"""Configuration-driven scheduler lifecycle and bounded execution policy."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from time import monotonic
from time import sleep as system_sleep
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.job import Job  # type: ignore[import-untyped]
from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from smc_ict.application.runtime import ProcessLock, RunReceipt
from smc_ict.configuration.models import ScheduleConfig, ScheduleJob


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    maximum_attempts: int
    backoff_seconds: tuple[int, ...]

    def __post_init__(self) -> None:
        if type(self.maximum_attempts) is not int or not 1 <= self.maximum_attempts <= 10:
            raise ValueError("maximum attempts must be an integer from 1 to 10")
        if len(self.backoff_seconds) != self.maximum_attempts - 1:
            raise ValueError("one backoff is required for each retry")
        if any(type(value) is not int or not 0 <= value <= 3600 for value in self.backoff_seconds):
            raise ValueError("backoffs must be integer seconds from 0 to 3600")


def run_with_retries(
    operation: Callable[[], RunReceipt],
    policy: RetryPolicy,
    *,
    sleep: Callable[[float], object] = system_sleep,
) -> RunReceipt:
    """Retry failed receipts only; overlap skips and successes are terminal."""
    receipt = operation()
    for delay in policy.backoff_seconds:
        if receipt.status != "FAILED":
            return receipt
        sleep(delay)
        receipt = operation()
    return receipt


class RecoveryRepository(Protocol):
    def recover_running_runs(self, *, completed_at_ms: int, reason: str) -> tuple[str, ...]: ...


@dataclass(frozen=True, slots=True)
class SchedulerHealth:
    running: bool
    ready: bool
    configured_jobs: int
    recovered_run_ids: tuple[str, ...]


OperationFactory = Callable[[ScheduleJob], Callable[[], RunReceipt]]


class InternalScheduler:
    """APScheduler lifecycle with explicit UTC, skip-misfire, and one-instance policy."""

    def __init__(
        self,
        schedule: ScheduleConfig,
        *,
        operation_factory: OperationFactory,
        repository: RecoveryRepository,
        clock_ms: Callable[[], int],
        retry_policy: RetryPolicy,
        recovery_lock_path: str | Path | None = None,
    ) -> None:
        if schedule.timezone != "UTC":
            raise ValueError("scheduler requires the validated UTC schedule")
        self._schedule = schedule
        self._operation_factory = operation_factory
        self._repository = repository
        self._clock_ms = clock_ms
        self._retry_policy = retry_policy
        self._recovery_lock_path = None if recovery_lock_path is None else Path(recovery_lock_path)
        self._backend = BackgroundScheduler(timezone=ZoneInfo("UTC"))
        self._operations: list[Callable[[], RunReceipt]] = []
        self._recovered: tuple[str, ...] = ()
        self._ready = False

    def start(self, *, paused: bool = False) -> None:
        if self._backend.running:
            raise RuntimeError("scheduler is already running")
        if self._recovery_lock_path is None:
            self._recovered = self._repository.recover_running_runs(
                completed_at_ms=self._clock_ms(), reason="PROCESS_RESTART"
            )
        else:
            with ProcessLock(self._recovery_lock_path) as recovery_lock:
                if not recovery_lock.acquired:
                    raise RuntimeError("scheduler recovery blocked by an active engine run")
                self._recovered = self._repository.recover_running_runs(
                    completed_at_ms=self._clock_ms(), reason="PROCESS_RESTART"
                )
        if self._schedule.enabled:
            for job in self._schedule.jobs:
                operation = self._operation_factory(job)
                self._operations.append(operation)
                trigger = CronTrigger.from_crontab(job.cron, timezone=ZoneInfo("UTC"))
                now = datetime.now(ZoneInfo("UTC"))
                not_before = now + timedelta(seconds=job.startup_delay_seconds)
                next_fire = trigger.get_next_fire_time(None, now)
                while next_fire is not None and next_fire < not_before:
                    next_fire = trigger.get_next_fire_time(next_fire, next_fire)
                self._backend.add_job(
                    self._run_job,
                    trigger=trigger,
                    args=(operation,),
                    id=job.id,
                    coalesce=False,
                    max_instances=1,
                    misfire_grace_time=job.misfire_grace_seconds,
                    next_run_time=next_fire,
                    replace_existing=False,
                )
        self._backend.start(paused=paused)
        self._ready = True

    def _run_job(self, operation: Callable[[], RunReceipt]) -> RunReceipt:
        return run_with_retries(operation, self._retry_policy)

    def get_job(self, job_id: str) -> Job:
        job = self._backend.get_job(job_id)
        if job is None:
            raise KeyError(job_id)
        return job

    def health(self) -> SchedulerHealth:
        running = self._backend.running
        return SchedulerHealth(
            running, running and self._ready, len(self._schedule.jobs), self._recovered
        )

    def shutdown(self, *, wait: bool = True, timeout_seconds: float = 30.0) -> None:
        self._ready = False
        if self._backend.running:
            self._backend.pause()
            deadline = monotonic() + max(0.0, timeout_seconds)
            for operation in self._operations:
                shutdown = getattr(operation, "shutdown", None)
                if callable(shutdown):
                    shutdown(timeout_seconds=max(0.0, deadline - monotonic()))
            self._backend.shutdown(wait=wait)
