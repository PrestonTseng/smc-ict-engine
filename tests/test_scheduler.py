from __future__ import annotations

import json
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from logging import LogRecord
from pathlib import Path
from threading import Event, Thread

import pytest

from smc_ict.application.runtime import RunReceipt

_UNTRUSTED_CHILD_TEXT = "FICTIONAL_SECRET"


def _child_receipt_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "run_id": "a" * 64,
        "status": "SUCCEEDED",
        "trigger": "scheduled",
        "started_at_ms": 1_000,
        "completed_at_ms": 2_000,
        "instrument_count": 1,
        "decision_count": 1,
        "error": None,
    }
    payload.update(overrides)
    return payload


def _run_managed_receipt(
    *,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    returncode: int,
    stdout: str,
) -> tuple[RunReceipt, tuple[tuple[object, ...], ...], list[LogRecord]]:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository
    from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
    from smc_ict.composition.runtime_services import _ManagedSubprocessOperation
    from smc_ict.configuration.models import ScheduleConfig

    class Process:
        def __init__(self) -> None:
            self.returncode = returncode

        def communicate(self, timeout: float | None = None) -> tuple[str, str]:
            return stdout, f"stderr {_UNTRUSTED_CHILD_TEXT}"

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Process())
    database = tmp_path / "receipt.sqlite3"
    repository = SQLiteRepository(database)
    operation = _ManagedSubprocessOperation(
        ["child"],
        maximum_runtime_seconds=1,
        termination_grace_seconds=0.01,
        repository=repository,
        lock_path=tmp_path / "engine.lock",
    )
    times = iter((3_000, 4_000))
    service = InternalScheduler(
        ScheduleConfig(False, "UTC", ()),
        operation_factory=lambda _job: operation,
        repository=repository,
        clock_ms=lambda: next(times),
        retry_policy=RetryPolicy(1, ()),
    )

    with caplog.at_level("INFO", logger="smc_ict.application.scheduler"):
        receipt = service._run_job(operation, "fixture-job")

    with sqlite3.connect(database) as connection:
        rows = tuple(
            connection.execute(
                "SELECT status,started_at_ms,completed_at_ms,error FROM runs "
                "WHERE strategy_name='scheduler:fixture-job'"
            ).fetchall()
        )
    records = [record for record in caplog.records if record.msg == "scheduler_job_receipt"]
    return receipt, rows, records


@pytest.mark.parametrize(
    ("returncode", "stdout"),
    [
        pytest.param(
            1,
            json.dumps(_child_receipt_payload(status="SUCCEEDED_FORGED")),
            id="nonzero-forged-success",
        ),
        pytest.param(
            1,
            json.dumps(
                _child_receipt_payload(
                    status=f"FAILED secret={_UNTRUSTED_CHILD_TEXT}",
                    error=_UNTRUSTED_CHILD_TEXT,
                )
            ),
            id="secret-bearing-unknown-status",
        ),
        pytest.param(0, json.dumps(_child_receipt_payload(run_id=[])), id="container-run-id"),
        pytest.param(0, json.dumps(_child_receipt_payload(status=[])), id="container-status"),
        pytest.param(0, json.dumps(_child_receipt_payload(trigger=True)), id="boolean-trigger"),
        pytest.param(
            0, json.dumps(_child_receipt_payload(started_at_ms=True)), id="boolean-start-time"
        ),
        pytest.param(
            0, json.dumps(_child_receipt_payload(completed_at_ms={})), id="container-end-time"
        ),
        pytest.param(
            0, json.dumps(_child_receipt_payload(instrument_count=False)), id="boolean-count"
        ),
        pytest.param(
            0, json.dumps(_child_receipt_payload(decision_count=[])), id="container-count"
        ),
        pytest.param(1, json.dumps(_child_receipt_payload(error={})), id="container-error"),
        pytest.param(0, json.dumps(_child_receipt_payload(trigger="forged")), id="unknown-trigger"),
        pytest.param(
            0, json.dumps(_child_receipt_payload(started_at_ms=-1)), id="negative-start-time"
        ),
        pytest.param(
            0,
            json.dumps(_child_receipt_payload(started_at_ms=2_001)),
            id="reversed-timestamps",
        ),
        pytest.param(0, json.dumps(_child_receipt_payload(decision_count=-1)), id="negative-count"),
        pytest.param(
            0,
            json.dumps(_child_receipt_payload(error=_UNTRUSTED_CHILD_TEXT)),
            id="success-with-error",
        ),
        pytest.param(
            0,
            json.dumps(_child_receipt_payload(status="SUCCEEDED_WITH_WARNINGS")),
            id="warning-without-error",
        ),
        pytest.param(
            1,
            json.dumps(_child_receipt_payload(status="FAILED", error=None)),
            id="failure-without-error",
        ),
        pytest.param(
            75,
            json.dumps(
                _child_receipt_payload(
                    status="OVERLAP_SKIPPED",
                    run_id="a" * 64,
                    instrument_count=0,
                    decision_count=0,
                )
            ),
            id="overlap-with-run-id",
        ),
        pytest.param(0, "not-json", id="malformed-json"),
        pytest.param(
            0,
            json.dumps(_child_receipt_payload()) + "\nchild noise " + _UNTRUSTED_CHILD_TEXT,
            id="noisy-json",
        ),
        pytest.param(0, "[]", id="json-container-not-object"),
        pytest.param(
            0,
            json.dumps(
                {
                    key: value
                    for key, value in _child_receipt_payload().items()
                    if key != "decision_count"
                }
            ),
            id="missing-field",
        ),
    ],
)
def test_managed_scheduler_redacts_every_invalid_child_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    returncode: int,
    stdout: str,
) -> None:
    receipt, rows, records = _run_managed_receipt(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        caplog=caplog,
        returncode=returncode,
        stdout=stdout,
    )

    assert receipt.status == "FAILED"
    assert receipt.run_id is None
    assert receipt.error == "INVALID_CHILD_RECEIPT"
    assert rows == (("FAILED", 3_000, 4_000, "FAILED"),)
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].outcome == "FAILED"
    assert records[0].child_run_id is None
    assert _UNTRUSTED_CHILD_TEXT not in caplog.text
    assert all(row[0] != "RUNNING" for row in rows)


def test_scheduler_and_sqlite_fail_closed_when_an_operation_bypasses_child_validation(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository
    from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
    from smc_ict.configuration.models import ScheduleConfig

    database = tmp_path / "bypass.sqlite3"
    repository = SQLiteRepository(database)
    times = iter((1_000, 2_000))
    service = InternalScheduler(
        ScheduleConfig(False, "UTC", ()),
        operation_factory=lambda _job: pytest.fail("not used"),
        repository=repository,
        clock_ms=lambda: next(times),
        retry_policy=RetryPolicy(1, ()),
    )

    with caplog.at_level("INFO", logger="smc_ict.application.scheduler"):
        service._run_job(
            lambda: RunReceipt(
                _UNTRUSTED_CHILD_TEXT,
                "SUCCEEDED_FORGED",
                "scheduled",
                1_000,
                2_000,
                0,
                0,
                _UNTRUSTED_CHILD_TEXT,
            ),
            "fixture-job",
        )

    with sqlite3.connect(database) as connection:
        rows = connection.execute(
            "SELECT status,error FROM runs WHERE strategy_name='scheduler:fixture-job'"
        ).fetchall()
    records = [record for record in caplog.records if record.msg == "scheduler_job_receipt"]
    assert rows == [("FAILED", "FAILED")]
    assert len(records) == 1
    assert records[0].levelname == "WARNING"
    assert records[0].outcome == "FAILED"
    assert records[0].child_run_id is None
    assert "SUCCEEDED_FORGED" not in caplog.text
    assert _UNTRUSTED_CHILD_TEXT not in caplog.text


@pytest.mark.parametrize(
    ("returncode", "payload", "durable_row", "levelname"),
    [
        pytest.param(
            0,
            _child_receipt_payload(),
            ("SUCCEEDED", None),
            "INFO",
            id="success",
        ),
        pytest.param(
            0,
            _child_receipt_payload(
                status="SUCCEEDED_WITH_WARNINGS", error="NOTIFICATION_WARNING: delivery"
            ),
            ("SUCCEEDED", None),
            "INFO",
            id="success-with-warnings",
        ),
        pytest.param(
            1,
            _child_receipt_payload(
                run_id=None,
                status="FAILED",
                instrument_count=0,
                decision_count=0,
                error=_UNTRUSTED_CHILD_TEXT,
            ),
            ("FAILED", "FAILED"),
            "WARNING",
            id="failure",
        ),
        pytest.param(
            75,
            _child_receipt_payload(
                run_id=None,
                status="OVERLAP_SKIPPED",
                instrument_count=0,
                decision_count=0,
            ),
            ("FAILED", "OVERLAP_SKIPPED"),
            "WARNING",
            id="overlap",
        ),
    ],
)
def test_managed_scheduler_preserves_exact_valid_child_receipts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    returncode: int,
    payload: dict[str, object],
    durable_row: tuple[str, str | None],
    levelname: str,
) -> None:
    receipt, rows, records = _run_managed_receipt(
        tmp_path=tmp_path,
        monkeypatch=monkeypatch,
        caplog=caplog,
        returncode=returncode,
        stdout=json.dumps(payload),
    )

    expected_receipt = dict(payload)
    if payload["status"] == "FAILED":
        expected_receipt["error"] = "CHILD_FAILED"
    assert receipt.canonical_dict() == expected_receipt
    assert tuple((row[0], row[3]) for row in rows) == (durable_row,)
    assert len(records) == 1
    assert records[0].levelname == levelname
    assert records[0].outcome == receipt.status
    assert records[0].child_run_id == receipt.run_id
    assert _UNTRUSTED_CHILD_TEXT not in caplog.text


@pytest.mark.parametrize(
    ("config_root", "container_path", "expected"),
    [
        (Path("/"), "/config/market-data.yaml", Path("/config/market-data.yaml")),
        (Path("/"), "/strategies/research.yaml", Path("/strategies/research.yaml")),
        (Path("/config"), "/config/market-data.yaml", Path("/config/market-data.yaml")),
        (Path("/config"), "/strategies/research.yaml", Path("/strategies/research.yaml")),
        (
            Path("/srv/smc/config"),
            "/config/market-data.yaml",
            Path("/srv/smc/config/market-data.yaml"),
        ),
        (
            Path("/srv/smc/config"),
            "/strategies/research.yaml",
            Path("/srv/smc/strategies/research.yaml"),
        ),
    ],
)
def test_scheduler_translates_both_read_only_roots_for_every_config_root_mode(
    config_root: Path, container_path: str, expected: Path
) -> None:
    from smc_ict.composition.runtime_services import _host_config_path

    assert _host_config_path(container_path, config_root) == expected


def test_active_schedule_fires_one_minute_after_each_completed_15m_boundary() -> None:
    from apscheduler.triggers.cron import CronTrigger

    from smc_ict.configuration import load_schedule

    schedule = load_schedule(Path(__file__).parents[1] / "config/schedule.yaml")
    job = schedule.jobs[0]

    assert (schedule.timezone, job.cron) == ("UTC", "1,16,31,46 * * * *")
    assert (job.misfire_policy, job.misfire_grace_seconds) == ("skip", 120)
    assert job.overlap_policy == "skip"

    trigger = CronTrigger.from_crontab(job.cron, timezone=UTC)
    previous = None
    now = datetime(2026, 1, 1, tzinfo=UTC)
    fires = []
    for _ in range(5):
        fire = trigger.get_next_fire_time(previous, now)
        assert fire is not None
        fires.append(fire)
        previous = now = fire

    assert fires == [
        datetime(2026, 1, 1, 0, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 16, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 31, tzinfo=UTC),
        datetime(2026, 1, 1, 0, 46, tzinfo=UTC),
        datetime(2026, 1, 1, 1, 1, tzinfo=UTC),
    ]


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


def test_scheduler_retries_a_non_allowlisted_receipt_status() -> None:
    from smc_ict.application.scheduler import RetryPolicy, run_with_retries

    attempts = 0

    def operation() -> RunReceipt:
        nonlocal attempts
        attempts += 1
        return RunReceipt(
            "a" * 64,
            "SUCCEEDED_FORGED" if attempts == 1 else "SUCCEEDED",
            "scheduled",
            0,
            attempts,
            1,
            1,
            None,
        )

    receipt = run_with_retries(operation, RetryPolicy(2, (0,)), sleep=lambda _delay: None)

    assert receipt.status == "SUCCEEDED"
    assert attempts == 2


def test_scheduler_persists_and_logs_attempt_before_a_pre_run_failure(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository
    from smc_ict.application.runtime import RunReceipt
    from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
    from smc_ict.configuration.models import ScheduleConfig

    database = tmp_path / "attempts.sqlite3"
    repository = SQLiteRepository(database)
    times = iter((1_000, 2_000))

    def operation() -> RunReceipt:
        with sqlite3.connect(database) as connection:
            assert connection.execute(
                "SELECT status FROM runs WHERE strategy_name='scheduler:fixture-job'"
            ).fetchone() == ("RUNNING",)
        return RunReceipt(None, "FAILED", "scheduled", 1_000, 2_000, 0, 0, "remote-secret")

    service = InternalScheduler(
        ScheduleConfig(False, "UTC", ()),
        operation_factory=lambda _job: operation,
        repository=repository,
        clock_ms=lambda: next(times),
        retry_policy=RetryPolicy(1, ()),
    )

    with caplog.at_level("INFO", logger="smc_ict.application.scheduler"):
        receipt = service._run_job(operation, "fixture-job")

    assert receipt.status == "FAILED"
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status,started_at_ms,completed_at_ms,error FROM runs "
            "WHERE strategy_name='scheduler:fixture-job'"
        ).fetchone()
    assert row == ("FAILED", 1_000, 2_000, "FAILED")
    assert "scheduler_job_receipt" in caplog.text
    assert "remote-secret" not in caplog.text


def test_scheduler_persists_and_logs_an_overlap_skip(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository
    from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
    from smc_ict.configuration.models import ScheduleConfig

    database = tmp_path / "overlap.sqlite3"
    service = InternalScheduler(
        ScheduleConfig(False, "UTC", ()),
        operation_factory=lambda _job: pytest.fail("overlap must not invoke the operation"),
        repository=SQLiteRepository(database),
        clock_ms=lambda: 61_000,
        retry_policy=RetryPolicy(1, ()),
    )

    with caplog.at_level("INFO", logger="smc_ict.application.scheduler"):
        service._record_overlap("fixture-job", scheduled_at_ms=60_000)

    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT status,started_at_ms,completed_at_ms,error FROM runs "
            "WHERE strategy_name='scheduler:fixture-job'"
        ).fetchone()
    assert row == ("FAILED", 60_000, 61_000, "OVERLAP_SKIPPED")
    assert "scheduler_job_overlap_skipped" in caplog.text


def test_scheduler_persists_timeout_category_without_child_error_text(tmp_path: Path) -> None:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository
    from smc_ict.application.runtime import RunReceipt
    from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
    from smc_ict.configuration.models import ScheduleConfig

    database = tmp_path / "timeout.sqlite3"
    times = iter((1_000, 2_000))
    service = InternalScheduler(
        ScheduleConfig(False, "UTC", ()),
        operation_factory=lambda _job: pytest.fail("not used"),
        repository=SQLiteRepository(database),
        clock_ms=lambda: next(times),
        retry_policy=RetryPolicy(1, ()),
    )

    service._run_job(
        lambda: RunReceipt(None, "FAILED", "scheduled", 1_000, 2_000, 0, 0, "MAXIMUM_RUNTIME"),
        "fixture-job",
    )

    with sqlite3.connect(database) as connection:
        error = connection.execute(
            "SELECT error FROM runs WHERE strategy_name='scheduler:fixture-job'"
        ).fetchone()
    assert error == ("MAXIMUM_RUNTIME",)


def test_timeout_reconciliation_does_not_finish_the_parent_attempt_early(tmp_path: Path) -> None:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository
    from smc_ict.application.ports import RunRecord
    from smc_ict.application.runtime import RunReceipt
    from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
    from smc_ict.configuration.models import ScheduleConfig

    repository = SQLiteRepository(tmp_path / "timeout-ownership.sqlite3")
    times = iter((1_000, 2_000, 2_000))

    def timed_out_child() -> RunReceipt:
        repository.store_run(
            RunRecord(
                "child-run",
                "RUNNING",
                1_100,
                None,
                "fixture",
                "1",
                "a" * 64,
                "okx_swap",
                "LINEAR_PERPETUAL",
                "b" * 64,
                "c" * 40,
                0,
                59_999,
                "d" * 64,
                None,
            )
        )
        recovered = repository.recover_running_runs(
            completed_at_ms=1_500,
            reason="MAXIMUM_RUNTIME",
            include_scheduler_attempts=False,
        )
        assert recovered == ("child-run",)
        return RunReceipt("child-run", "FAILED", "scheduled", 1_000, 1_500, 0, 0, "MAXIMUM_RUNTIME")

    service = InternalScheduler(
        ScheduleConfig(False, "UTC", ()),
        operation_factory=lambda _job: timed_out_child,
        repository=repository,
        clock_ms=lambda: next(times),
        retry_policy=RetryPolicy(1, ()),
    )

    receipt = service._run_job(timed_out_child, "fixture-job")

    assert receipt.error == "MAXIMUM_RUNTIME"
    assert repository.load_run("child-run").error == "MAXIMUM_RUNTIME"
    with sqlite3.connect(repository.path) as connection:
        attempt = connection.execute(
            "SELECT completed_at_ms,error FROM runs WHERE provider_id='scheduler'"
        ).fetchone()
    assert attempt == (2_000, "MAXIMUM_RUNTIME")


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

        def start_scheduler_attempt(self, job_id: str, *, started_at_ms: int, sequence: int) -> str:
            assert (job_id, started_at_ms, sequence) == ("owned-child", 0, 0)
            return "attempt"

        def finish_scheduler_attempt(
            self, attempt_id: str, outcome: str, *, completed_at_ms: int
        ) -> object:
            assert (attempt_id, outcome, completed_at_ms) == ("attempt", "FAILED", 0)
            return object()

    service = InternalScheduler(
        schedule,
        operation_factory=lambda _job: operation,
        repository=Repository(),
        clock_ms=lambda: 0,
        retry_policy=RetryPolicy(1, ()),
    )
    service.start(paused=True)
    worker = Thread(target=service._run_job, args=(operation, "owned-child"))
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

        def recover_running_runs(
            self,
            *,
            completed_at_ms: int,
            reason: str,
            include_scheduler_attempts: bool = True,
        ) -> tuple[str, ...]:
            assert include_scheduler_attempts is False
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
