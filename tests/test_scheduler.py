from __future__ import annotations

import subprocess
from datetime import UTC, datetime, timedelta
from threading import Event, Thread

import pytest


def test_complete_scheduler_job_is_never_retried() -> None:
    from smc_ict.application.runtime import RunReceipt
    from smc_ict.application.scheduler import RetryPolicy, run_with_retries

    attempts = 0
    delays: list[float] = []

    def operation() -> RunReceipt:
        nonlocal attempts
        attempts += 1
        status = "SUCCEEDED" if attempts == 3 else "FAILED"
        return RunReceipt(
            "run", status, "scheduled", 0, attempts, 1, int(status == "SUCCEEDED"), None
        )

    receipt = run_with_retries(
        operation,
        RetryPolicy(maximum_attempts=1, backoff_seconds=()),
        sleep=delays.append,
    )

    assert receipt.status == "FAILED"
    assert attempts == 1
    assert delays == []


def test_scheduler_applies_utc_misfire_overlap_coalescing_recovery_and_shutdown() -> None:
    from smc_ict.application.runtime import RunReceipt
    from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
    from smc_ict.configuration.models import ScheduleConfig, ScheduleJob

    schedule = ScheduleConfig(
        enabled=True,
        timezone="UTC",
        jobs=(
            ScheduleJob(
                id="fixture-job",
                cron="*/5 * * * *",
                strategy="/config/strategy.yaml",
                market_data="/config/market.yaml",
                notifications="/config/notifications.yaml",
                misfire_policy="skip",
                misfire_grace_seconds=17,
                overlap_policy="skip",
                maximum_runtime_seconds=60,
                startup_delay_seconds=0,
            ),
        ),
    )

    class Repository:
        def recover_running_runs(self, *, completed_at_ms: int, reason: str) -> tuple[str, ...]:
            assert (completed_at_ms, reason) == (123, "PROCESS_RESTART")
            return ("stale-run",)

    service = InternalScheduler(
        schedule,
        operation_factory=lambda _job: lambda: RunReceipt(
            "run", "SUCCEEDED", "scheduled", 0, 1, 1, 1, None
        ),
        repository=Repository(),
        clock_ms=lambda: 123,
        retry_policy=RetryPolicy(1, ()),
    )

    service.start(paused=True)
    health = service.health()
    scheduled_job = service.get_job("fixture-job")
    assert health.running is True
    assert health.ready is True
    assert health.recovered_run_ids == ("stale-run",)
    assert str(scheduled_job.trigger.timezone) == "UTC"
    assert scheduled_job.coalesce is False
    assert scheduled_job.max_instances == 1
    assert scheduled_job.misfire_grace_time == 17

    service.shutdown(wait=True)
    assert service.health().running is False
    assert service.health().ready is False


def test_scheduler_honors_configured_startup_delay_for_first_fire() -> None:
    from smc_ict.application.runtime import RunReceipt
    from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
    from smc_ict.configuration.models import ScheduleConfig, ScheduleJob

    schedule = ScheduleConfig(
        enabled=True,
        timezone="UTC",
        jobs=(
            ScheduleJob(
                id="delayed-job",
                cron="* * * * *",
                strategy="/config/strategy.yaml",
                market_data="/config/market.yaml",
                notifications="/config/notifications.yaml",
                misfire_policy="skip",
                misfire_grace_seconds=10,
                overlap_policy="skip",
                maximum_runtime_seconds=60,
                startup_delay_seconds=300,
            ),
        ),
    )

    class Repository:
        def recover_running_runs(self, *, completed_at_ms: int, reason: str) -> tuple[str, ...]:
            return ()

    service = InternalScheduler(
        schedule,
        operation_factory=lambda _job: lambda: RunReceipt(
            "run", "SUCCEEDED", "scheduled", 0, 1, 1, 1, None
        ),
        repository=Repository(),
        clock_ms=lambda: 0,
        retry_policy=RetryPolicy(1, ()),
    )
    earliest = datetime.now(UTC) + timedelta(seconds=295)

    service.start(paused=True)
    try:
        assert service.get_job("delayed-job").next_run_time >= earliest
    finally:
        service.shutdown(wait=True)


def test_scheduler_build_validates_every_referenced_job_authority_before_readiness(
    tmp_path,
) -> None:
    from smc_ict.composition.runtime_services import build_scheduler

    schedule = tmp_path / "schedule.yaml"
    schedule.write_text(
        """schedule:
  enabled: true
  timezone: UTC
  jobs:
    - id: broken
      cron: "* * * * *"
      strategy: /config/missing-strategy.yaml
      market_data: /config/missing-market.yaml
      notifications: /config/missing-notifications.yaml
      misfire_policy: skip
      misfire_grace_seconds: 10
      overlap_policy: skip
      maximum_runtime_seconds: 60
      startup_delay_seconds: 0
""",
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="missing-strategy"):
        build_scheduler(
            schedule_path=schedule,
            database=tmp_path / "runtime.sqlite3",
            lock_path=tmp_path / "engine.lock",
            config_root=tmp_path,
        )


def test_scheduler_shutdown_stops_and_drains_an_active_owned_operation() -> None:
    from smc_ict.application.runtime import RunReceipt
    from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
    from smc_ict.configuration.models import ScheduleConfig, ScheduleJob

    entered = Event()
    released = Event()

    class Operation:
        shutdown_timeout: float | None = None

        def __call__(self) -> RunReceipt:
            entered.set()
            assert released.wait(2)
            return RunReceipt(None, "FAILED", "scheduled", 0, 1, 0, 0, "SHUTDOWN")

        def shutdown(self, *, timeout_seconds: float) -> None:
            self.shutdown_timeout = timeout_seconds
            released.set()

    operation = Operation()
    schedule = ScheduleConfig(
        enabled=True,
        timezone="UTC",
        jobs=(
            ScheduleJob(
                id="owned-child",
                cron="* * * * *",
                strategy="/config/strategy.yaml",
                market_data="/config/market.yaml",
                notifications="/config/notifications.yaml",
                misfire_policy="skip",
                misfire_grace_seconds=10,
                overlap_policy="skip",
                maximum_runtime_seconds=60,
                startup_delay_seconds=300,
            ),
        ),
    )

    class Repository:
        def recover_running_runs(self, *, completed_at_ms: int, reason: str) -> tuple[str, ...]:
            return ()

    service = InternalScheduler(
        schedule,
        operation_factory=lambda _job: operation,
        repository=Repository(),
        clock_ms=lambda: 0,
        retry_policy=RetryPolicy(1, ()),
    )
    service.start(paused=True)
    worker = Thread(target=service._run_job, args=(operation,))
    worker.start()
    assert entered.wait(1)

    service.shutdown(wait=True, timeout_seconds=0.25)
    worker.join(timeout=1)

    assert operation.shutdown_timeout is not None
    assert 0 < operation.shutdown_timeout <= 0.25
    assert not worker.is_alive()


def test_timed_out_child_is_terminated_killed_drained_and_durably_reconciled(
    tmp_path, monkeypatch
) -> None:
    from smc_ict.composition.runtime_services import _subprocess_operation
    from smc_ict.configuration.models import ScheduleJob

    class Process:
        returncode = -9
        communicate_calls = 0
        terminated = False
        killed = False

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            self.communicate_calls += 1
            if self.communicate_calls <= 2:
                raise subprocess.TimeoutExpired(["child"], 0.0 if timeout is None else timeout)
            return "", ""

        def terminate(self) -> None:
            self.terminated = True

        def kill(self) -> None:
            self.killed = True

    process = Process()
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: process)

    class Repository:
        def __init__(self) -> None:
            self.recovered: list[tuple[int, str]] = []

        def recover_running_runs(self, *, completed_at_ms: int, reason: str) -> tuple[str, ...]:
            self.recovered.append((completed_at_ms, reason))
            return ("interrupted-run",)

    repository = Repository()
    job = ScheduleJob(
        id="timeout",
        cron="* * * * *",
        strategy="/config/strategy.yaml",
        market_data="/config/market.yaml",
        notifications="/config/notifications.yaml",
        misfire_policy="skip",
        misfire_grace_seconds=10,
        overlap_policy="skip",
        maximum_runtime_seconds=1,
        startup_delay_seconds=0,
    )
    operation = _subprocess_operation(
        job,
        database=tmp_path / "runtime.sqlite3",
        lock_path=tmp_path / "engine.lock",
        config_root=tmp_path,
        repository=repository,
        termination_grace_seconds=0.01,
    )

    receipt = operation()

    assert receipt.status == "FAILED"
    assert receipt.run_id == "interrupted-run"
    assert receipt.error == "MAXIMUM_RUNTIME"
    assert process.terminated is True
    assert process.killed is True
    assert process.communicate_calls == 3
    assert repository.recovered and repository.recovered[0][1] == "MAXIMUM_RUNTIME"
