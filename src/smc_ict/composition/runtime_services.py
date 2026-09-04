"""Concrete runtime wiring kept outside CLI handlers and application core."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from threading import Lock
from time import time_ns
from typing import Protocol

from smc_ict.adapters.persistence.sqlite import SQLiteRepository
from smc_ict.application.historical_sync import HistoricalRangeSyncService, HistoricalSyncReceipt
from smc_ict.application.notifications import NotificationRouter
from smc_ict.application.ports import InstrumentMapping, Notifier
from smc_ict.application.receipt_contract import (
    RUN_RECEIPT_STATUSES,
    RUN_RECEIPT_SUCCESS_STATUSES,
    RUN_RECEIPT_TRIGGERS,
    is_canonical_run_id,
)
from smc_ict.application.runtime import (
    EngineRunner,
    ProcessLock,
    RunReceipt,
    RunRequest,
    RuntimeConfiguration,
)
from smc_ict.application.scheduler import InternalScheduler, RetryPolicy
from smc_ict.composition.registries import (
    CompositionRoot,
    build_market_provider,
    indicator_composition_root,
    market_data_composition_root,
    notification_composition_root,
)
from smc_ict.configuration import (
    DEFERRED_PLUGIN_IDS,
    load_market_data,
    load_notifications,
    load_schedule,
    load_strategy,
)
from smc_ict.configuration.models import NotificationDestination, ScheduleJob


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    data_folder: Path
    database: Path
    lock: Path
    health: Path
    backtests: Path

    @classmethod
    def from_environ(cls, environ: Mapping[str, str] | None = None) -> RuntimePaths:
        source = os.environ if environ is None else environ
        text = source.get("DATA_FOLDER")
        if text is None or not text:
            raise ValueError("DATA_FOLDER is required")
        root = Path(text)
        if not root.is_absolute() or str(root) != text:
            raise ValueError("DATA_FOLDER must be an absolute normalized path")
        return cls(
            root,
            root / "smc_ict.db",
            root / "engine.lock",
            root / "scheduler.ready",
            root / "backtests",
        )


def required_runtime_folder(variable: str, environ: Mapping[str, str] | None = None) -> Path:
    source = os.environ if environ is None else environ
    text = source.get(variable)
    if text is None or not text:
        raise ValueError(f"{variable} is required")
    path = Path(text)
    if not path.is_absolute() or str(path) != text:
        raise ValueError(f"{variable} must be an absolute normalized path")
    return path


def _utc_minute_ms(text: str) -> int:
    try:
        parsed = datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError("range timestamps must be canonical UTC minute strings") from exc
    if parsed.strftime("%Y-%m-%dT%H:%M:%SZ") != text or parsed.second != 0:
        raise ValueError("range timestamps must be canonical UTC minute strings")
    milliseconds = int(parsed.timestamp()) * 1000
    if milliseconds < 0:
        raise ValueError("range timestamps must not precede the Unix epoch")
    return milliseconds


def sync_historical_range(*, start: str, end: str) -> HistoricalSyncReceipt:
    paths = RuntimePaths.from_environ()
    market = load_market_data(required_runtime_folder("CONFIG_FOLDER") / "market-data.yaml")
    start_ms = _utc_minute_ms(start)
    end_ms = _utc_minute_ms(end)
    if start_ms > end_ms:
        raise ValueError("historical range start must not follow end")
    provider = build_market_provider(market, market_data_composition_root())
    mappings = tuple(
        InstrumentMapping(instrument_id, provider_symbol)
        for instrument_id, provider_symbol in market.instruments.items()
    )
    with ProcessLock(paths.lock) as lock:
        if not lock.acquired:
            raise RuntimeError("historical sync cannot overlap the active writer")
        return HistoricalRangeSyncService(provider, SQLiteRepository(paths.database)).sync_all(
            mappings,
            start_ms,
            end_ms,
        )


def current_time_ms() -> int:
    return time_ns() // 1_000_000


def current_git_commit() -> str:
    configured = os.environ.get("SMC_ICT_GIT_COMMIT")
    if configured is not None:
        return configured
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        text=True,
        capture_output=True,
        timeout=5,
        check=True,
    )
    return result.stdout.strip()


def build_engine_runner(database: str | Path, lock_path: str | Path) -> EngineRunner:
    root = notification_composition_root()
    plugin_factories = {
        plugin_id: root.plugins.resolve(plugin_id) for plugin_id in DEFERRED_PLUGIN_IDS
    }
    return EngineRunner(
        repository=SQLiteRepository(database),
        provider_factory=lambda config: build_market_provider(config, root),
        plugin_factories=plugin_factories,  # type: ignore[arg-type]
        lock_path=lock_path,
        clock_ms=current_time_ms,
        git_commit=current_git_commit(),
    )


def _build_notification_adapter(
    root: CompositionRoot,
    destination_id: str,
    destination: NotificationDestination,
) -> Notifier:
    candidate = root.notifiers.resolve(destination.adapter)(
        destination_id,
        destination,
        clock_seconds=lambda: current_time_ms() // 1000,
    )
    if not isinstance(candidate, Notifier):
        raise TypeError("registered notifier does not implement Notifier")
    return candidate


def run_once(
    *,
    strategy: str | Path,
    market_data: str | Path,
    notifications: str | Path | None,
    database: str | Path,
    lock_path: str | Path,
    trigger: str,
) -> RunReceipt:
    started = current_time_ms()
    try:
        notification_config = (
            load_notifications(notifications) if notifications is not None else None
        )
        root = notification_composition_root()
        router = (
            None
            if notification_config is None
            else NotificationRouter(
                notification_config,
                adapter_factory=lambda destination_id, destination: _build_notification_adapter(
                    root, destination_id, destination
                ),
                clock_seconds=lambda: current_time_ms() // 1000,
                deduplication_store=SQLiteRepository(database),
            )
        )
        runner = build_engine_runner(database, lock_path)
        if notification_config is not None:
            assert router is not None
            runner._config_loader = lambda request: RuntimeConfiguration(
                load_strategy(request.strategy_path, allow_deferred=True),
                load_market_data(request.market_data_path),
                notification_config,
            )

            def event_sink(event: object) -> object:
                return router.deliver(event)  # type: ignore[arg-type]

            runner._event_sink = event_sink
            runner._event_batch_sink = router.deliver_all
        receipt = runner.run(RunRequest(strategy, market_data, notifications, trigger))
        if router is None:
            return receipt
        try:
            final_failed = router.close().outcome in {"PARTIAL_FAILURE", "ALL_FAILURE"}
        except Exception:
            final_failed = True
        if not final_failed:
            return receipt
        if receipt.status not in {"SUCCEEDED", "SUCCEEDED_WITH_WARNINGS"}:
            return receipt
        warning = "NOTIFICATION_WARNING: final_flush"
        if receipt.error is not None:
            warning = f"{receipt.error}, final_flush"
        return replace(receipt, status="SUCCEEDED_WITH_WARNINGS", error=warning[:4000])
    except Exception as exc:
        completed = current_time_ms()
        error = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")[:4000]
        return RunReceipt(None, "FAILED", trigger, started, completed, 0, 0, error)


def _host_config_path(container_path: str, config_root: str | Path) -> Path:
    if Path(config_root) == Path("/"):
        return Path(container_path)
    path = PurePosixPath(container_path)
    config = Path(config_root)
    if path.is_relative_to("/config"):
        relative = path.relative_to("/config")
        return config.joinpath(*relative.parts)
    relative = path.relative_to("/strategies")
    return config.parent.joinpath("strategies", *relative.parts)


class RunningRunRecovery(Protocol):
    def recover_running_runs(
        self,
        *,
        completed_at_ms: int,
        reason: str,
        include_scheduler_attempts: bool = True,
    ) -> tuple[str, ...]: ...


class _ManagedSubprocessOperation:
    """Own one scheduled child and reconcile its durable run after interruption."""

    def __init__(
        self,
        command: list[str],
        *,
        maximum_runtime_seconds: int,
        termination_grace_seconds: float,
        repository: RunningRunRecovery,
        lock_path: str | Path,
    ) -> None:
        self._command = command
        self._maximum_runtime_seconds = maximum_runtime_seconds
        self._termination_grace_seconds = termination_grace_seconds
        self._repository = repository
        self._lock_path = Path(lock_path)
        self._state_lock = Lock()
        self._stop_lock = Lock()
        self._process: subprocess.Popen[str] | None = None
        self._interruption: tuple[str | None, str] | None = None

    def __call__(self) -> RunReceipt:
        started = current_time_ms()
        process = subprocess.Popen(
            self._command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        with self._state_lock:
            self._process = process
            self._interruption = None
        try:
            try:
                stdout, stderr = process.communicate(timeout=self._maximum_runtime_seconds)
            except subprocess.TimeoutExpired:
                self._interrupt(process, "MAXIMUM_RUNTIME", self._termination_grace_seconds)
                stdout, stderr = "", ""
            with self._state_lock:
                interruption = self._interruption
            if interruption is not None:
                run_id, reason = interruption
                completed = current_time_ms()
                return RunReceipt(run_id, "FAILED", "scheduled", started, completed, 0, 0, reason)
            return _receipt_from_child(process.returncode, stdout, stderr)
        finally:
            with self._state_lock:
                if self._process is process:
                    self._process = None

    def shutdown(self, *, timeout_seconds: float) -> None:
        with self._state_lock:
            process = self._process
        if process is not None:
            self._interrupt(process, "SCHEDULER_SHUTDOWN", timeout_seconds)

    def _interrupt(self, process: subprocess.Popen[str], reason: str, grace: float) -> None:
        with self._stop_lock:
            with self._state_lock:
                if self._interruption is not None:
                    return
            process.terminate()
            try:
                process.communicate(timeout=max(0.0, grace))
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
            recovered: tuple[str, ...] = ()
            with ProcessLock(self._lock_path) as recovery_lock:
                if recovery_lock.acquired:
                    recovered = self._repository.recover_running_runs(
                        completed_at_ms=current_time_ms(),
                        reason=reason,
                        include_scheduler_attempts=False,
                    )
            run_id = recovered[0] if len(recovered) == 1 else None
            with self._state_lock:
                self._interruption = (run_id, reason)


_CHILD_RECEIPT_FIELDS = frozenset(RunReceipt.__dataclass_fields__)
_CHILD_RECEIPT_RETURN_CODES = {
    "SUCCEEDED": 0,
    "SUCCEEDED_WITH_WARNINGS": 0,
    "FAILED": 1,
    "OVERLAP_SKIPPED": 75,
}


def _invalid_child_receipt() -> RunReceipt:
    now = current_time_ms()
    return RunReceipt(None, "FAILED", "scheduled", now, now, 0, 0, "INVALID_CHILD_RECEIPT")


def _receipt_from_child(returncode: int, stdout: str, stderr: str) -> RunReceipt:
    del stderr
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return _invalid_child_receipt()
    if type(payload) is not dict or frozenset(payload) != _CHILD_RECEIPT_FIELDS:
        return _invalid_child_receipt()

    run_id = payload["run_id"]
    status = payload["status"]
    trigger = payload["trigger"]
    started_at_ms = payload["started_at_ms"]
    completed_at_ms = payload["completed_at_ms"]
    instrument_count = payload["instrument_count"]
    decision_count = payload["decision_count"]
    error = payload["error"]
    if (
        type(returncode) is not int
        or type(status) is not str
        or status not in RUN_RECEIPT_STATUSES
        or _CHILD_RECEIPT_RETURN_CODES[status] != returncode
        or type(trigger) is not str
        or trigger not in RUN_RECEIPT_TRIGGERS
        or type(started_at_ms) is not int
        or started_at_ms < 0
        or type(completed_at_ms) is not int
        or completed_at_ms < started_at_ms
        or type(instrument_count) is not int
        or instrument_count < 0
        or type(decision_count) is not int
        or decision_count < 0
        or (run_id is not None and not is_canonical_run_id(run_id))
        or (error is not None and (type(error) is not str or not 1 <= len(error) <= 4000))
        or (status in RUN_RECEIPT_SUCCESS_STATUSES and run_id is None)
        or (status == "SUCCEEDED" and error is not None)
        or (status == "SUCCEEDED_WITH_WARNINGS" and error is None)
        or (status == "FAILED" and error is None)
        or (
            status == "OVERLAP_SKIPPED"
            and (
                run_id is not None
                or error is not None
                or instrument_count != 0
                or decision_count != 0
            )
        )
    ):
        return _invalid_child_receipt()
    return RunReceipt(
        run_id,
        status,
        trigger,
        started_at_ms,
        completed_at_ms,
        instrument_count,
        decision_count,
        "CHILD_FAILED" if status == "FAILED" else error,
    )


def _subprocess_operation(
    job: ScheduleJob,
    *,
    database: str | Path,
    lock_path: str | Path,
    config_root: str | Path,
    repository: RunningRunRecovery,
    termination_grace_seconds: float = 5.0,
) -> Callable[[], RunReceipt]:
    command = [
        sys.executable,
        "-m",
        "smc_ict.cli",
        "run",
        "--strategy",
        str(_host_config_path(job.strategy, config_root)),
        "--notifications",
        str(_host_config_path(job.notifications, config_root)),
        "--trigger",
        "scheduled",
    ]

    return _ManagedSubprocessOperation(
        command,
        maximum_runtime_seconds=job.maximum_runtime_seconds,
        termination_grace_seconds=termination_grace_seconds,
        repository=repository,
        lock_path=lock_path,
    )


def build_scheduler(
    *,
    schedule_path: str | Path,
    database: str | Path,
    lock_path: str | Path,
    config_root: str | Path,
) -> InternalScheduler:
    schedule = load_schedule(schedule_path)
    market = load_market_data(Path(config_root) / "market-data.yaml") if schedule.enabled else None
    for job in schedule.jobs if schedule.enabled else ():
        strategy = load_strategy(_host_config_path(job.strategy, config_root))
        load_notifications(_host_config_path(job.notifications, config_root))
        assert market is not None
        if set(strategy.instruments) - market.instruments.keys():
            raise ValueError("strategy instruments are missing from market-data configuration")
    repository = SQLiteRepository(database)
    return InternalScheduler(
        schedule,
        operation_factory=lambda job: _subprocess_operation(
            job,
            database=database,
            lock_path=lock_path,
            config_root=config_root,
            repository=repository,
        ),
        repository=repository,
        clock_ms=current_time_ms,
        retry_policy=RetryPolicy(1, ()),
        recovery_lock_path=lock_path,
    )
