"""Configuration-driven scheduler lifecycle and bounded execution policy."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from threading import Lock
from time import monotonic
from time import sleep as system_sleep
from typing import Protocol
from zoneinfo import ZoneInfo

from apscheduler.events import (  # type: ignore[import-untyped]
    EVENT_JOB_MAX_INSTANCES,
    JobSubmissionEvent,
)
from apscheduler.job import Job  # type: ignore[import-untyped]
from apscheduler.schedulers.background import BackgroundScheduler  # type: ignore[import-untyped]
from apscheduler.triggers.cron import CronTrigger  # type: ignore[import-untyped]

from smc_ict.application.ports.repository import RunRecord
from smc_ict.application.receipt_contract import (
    RUN_RECEIPT_STATUSES,
    RUN_RECEIPT_SUCCESS_STATUSES,
    SCHEDULER_FAILURE_OUTCOMES,
    is_canonical_run_id,
)
from smc_ict.application.runtime import ProcessLock, RunReceipt
from smc_ict.configuration.models import ScheduleConfig, ScheduleJob

LOGGER = logging.getLogger(__name__)


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
        if receipt.status in RUN_RECEIPT_SUCCESS_STATUSES or receipt.status == "OVERLAP_SKIPPED":
            return receipt
        sleep(delay)
        receipt = operation()
    return receipt


class RecoveryRepository(Protocol):
    def recover_running_runs(
        self,
        *,
        completed_at_ms: int,
        reason: str,
        include_scheduler_attempts: bool = True,
    ) -> tuple[str, ...]: ...

    def start_scheduler_attempt(self, job_id: str, *, started_at_ms: int, sequence: int) -> str: ...

    def finish_scheduler_attempt(
        self, attempt_id: str, outcome: str, *, completed_at_ms: int
    ) -> RunRecord: ...


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
        self._backend.add_listener(self._handle_scheduler_event, EVENT_JOB_MAX_INSTANCES)
        self._operations: list[Callable[[], RunReceipt]] = []
        self._recovered: tuple[str, ...] = ()
        self._ready = False
        self._attempt_sequence = 0
        self._attempt_lock = Lock()

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
                    args=(operation, job.id),
                    id=job.id,
                    coalesce=False,
                    max_instances=1,
                    misfire_grace_time=job.misfire_grace_seconds,
                    next_run_time=next_fire,
                    replace_existing=False,
                )
        self._backend.start(paused=paused)
        self._ready = True

    def _run_job(self, operation: Callable[[], RunReceipt], job_id: str) -> RunReceipt:
        started = self._clock_ms()
        sequence = self._next_attempt_sequence()
        attempt_id = self._repository.start_scheduler_attempt(
            job_id, started_at_ms=started, sequence=sequence
        )
        try:
            receipt = run_with_retries(operation, self._retry_policy)
        except Exception as exc:
            completed = self._clock_ms()
            receipt = RunReceipt(
                None, "FAILED", "scheduled", started, completed, 0, 0, type(exc).__name__
            )
        completed = max(started, self._clock_ms())
        durable_outcome = self._durable_outcome(receipt)
        self._repository.finish_scheduler_attempt(
            attempt_id, durable_outcome, completed_at_ms=completed
        )
        log = LOGGER.info if durable_outcome == "SUCCEEDED" else LOGGER.warning
        child_run_id = (
            receipt.run_id
            if receipt.status in RUN_RECEIPT_STATUSES and is_canonical_run_id(receipt.run_id)
            else None
        )
        log(
            "scheduler_job_receipt",
            extra={
                "attempt_id": attempt_id,
                "job_id": job_id,
                "outcome": durable_outcome,
                "child_run_id": child_run_id,
            },
        )
        return receipt

    @staticmethod
    def _durable_outcome(receipt: RunReceipt) -> str:
        if receipt.status in RUN_RECEIPT_SUCCESS_STATUSES or receipt.status == "OVERLAP_SKIPPED":
            return receipt.status
        if receipt.status == "FAILED" and receipt.error in SCHEDULER_FAILURE_OUTCOMES:
            return receipt.error
        return "FAILED"

    def _handle_scheduler_event(self, event: JobSubmissionEvent) -> None:
        for scheduled_run_time in event.scheduled_run_times:
            self._record_overlap(
                event.job_id, scheduled_at_ms=int(scheduled_run_time.timestamp() * 1000)
            )

    def _record_overlap(self, job_id: str, *, scheduled_at_ms: int) -> None:
        attempt_id = self._repository.start_scheduler_attempt(
            job_id,
            started_at_ms=scheduled_at_ms,
            sequence=self._next_attempt_sequence(),
        )
        completed = max(scheduled_at_ms, self._clock_ms())
        self._repository.finish_scheduler_attempt(
            attempt_id, "OVERLAP_SKIPPED", completed_at_ms=completed
        )
        LOGGER.warning(
            "scheduler_job_overlap_skipped",
            extra={
                "attempt_id": attempt_id,
                "job_id": job_id,
                "outcome": "OVERLAP_SKIPPED",
            },
        )

    def _next_attempt_sequence(self) -> int:
        with self._attempt_lock:
            sequence = self._attempt_sequence
            self._attempt_sequence += 1
            return sequence

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
