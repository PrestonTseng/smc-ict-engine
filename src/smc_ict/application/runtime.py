"""Single deterministic application path for manual and scheduled analysis runs."""

from __future__ import annotations

import fcntl
import json
import logging
from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from pathlib import Path
from types import MappingProxyType
from typing import TextIO

from smc_ict.application.decision_policy import OrderedDecisionPlugin, configured_decision_signals
from smc_ict.application.evidence import decision_record, observation_record
from smc_ict.application.graph import IndicatorFactory, IndicatorGraph, RunContext, configured_nodes
from smc_ict.application.market_sync import MarketSyncService
from smc_ict.application.ports import (
    InstrumentMapping,
    KlineProvider,
    NotificationEvent,
    Repository,
    RunRecord,
)
from smc_ict.application.resampling import resample_roles
from smc_ict.configuration import (
    MarketDataConfig,
    NotificationConfig,
    StrategyConfig,
    hash_market_data,
    hash_strategy,
    load_market_data,
    load_notifications,
    load_strategy,
)
from smc_ict.domain import ClosedCandle, Decision, Observation, hash_candles, hash_decision

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class RunRequest:
    strategy_path: str | Path
    market_data_path: str | Path
    notifications_path: str | Path | None
    trigger: str

    def __post_init__(self) -> None:
        if self.trigger not in {"manual", "scheduled"}:
            raise ValueError("trigger must be manual or scheduled")


@dataclass(frozen=True, slots=True)
class RuntimeConfiguration:
    strategy: StrategyConfig
    market_data: MarketDataConfig
    notifications: NotificationConfig


@dataclass(frozen=True, slots=True)
class RunReceipt:
    run_id: str | None
    status: str
    trigger: str
    started_at_ms: int
    completed_at_ms: int
    instrument_count: int
    decision_count: int
    error: str | None

    def canonical_dict(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


RuntimeConfigLoader = Callable[[RunRequest], RuntimeConfiguration]
ProviderFactory = Callable[[MarketDataConfig], KlineProvider]
EventSink = Callable[[NotificationEvent], object]
EventBatchSink = Callable[[tuple[NotificationEvent, ...]], object]
Clock = Callable[[], int]


def _decision_notification_payload(
    decision: Decision, *, evaluation_time_ms: int
) -> dict[str, None | bool | int | str]:
    payload: dict[str, None | bool | int | str] = {
        "status": decision.status,
        "evaluation_time_ms": evaluation_time_ms,
        "closed_bar_time_ms": evaluation_time_ms,
        "decision_id": hash_decision(decision),
    }
    if decision.status != "READY":
        payload["first_failed_signal"] = decision.first_failed_signal
        return payload
    assert decision.entry_text is not None
    assert decision.stop_text is not None
    assert decision.target_text is not None
    entry = Decimal(decision.entry_text)
    risk = abs(entry - Decimal(decision.stop_text))
    reward = abs(Decimal(decision.target_text) - entry)
    ratio = "UNDEFINED" if risk == 0 else format((reward / risk).normalize(), "f")
    payload.update(
        {
            "direction": decision.direction,
            "entry": decision.entry_text,
            "stop": decision.stop_text,
            "target": decision.target_text,
            "reward_risk": ratio,
        }
    )
    return payload


def load_runtime_configuration(request: RunRequest) -> RuntimeConfiguration:
    notifications = (
        NotificationConfig(False, MappingProxyType({}))
        if request.notifications_path is None
        else load_notifications(request.notifications_path)
    )
    return RuntimeConfiguration(
        strategy=load_strategy(request.strategy_path, allow_deferred=True),
        market_data=load_market_data(request.market_data_path),
        notifications=notifications,
    )


class ProcessLock(AbstractContextManager["ProcessLock"]):
    """Linux advisory lock released by close or process death."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._handle: TextIO | None = None
        self.acquired = False

    def __enter__(self) -> ProcessLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            handle.close()
            return self
        handle.seek(0)
        handle.truncate()
        handle.write(str(__import__("os").getpid()))
        handle.flush()
        self._handle = handle
        self.acquired = True
        return self

    def __exit__(self, *exc_info: object) -> None:
        handle = self._handle
        if handle is not None:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
        self._handle = None
        self.acquired = False


class EngineRunner:
    """Own the complete run transaction; callers only choose the trigger."""

    def __init__(
        self,
        *,
        repository: Repository,
        provider_factory: ProviderFactory,
        plugin_factories: Mapping[str, IndicatorFactory],
        lock_path: str | Path,
        config_loader: RuntimeConfigLoader = load_runtime_configuration,
        clock_ms: Clock,
        git_commit: str,
        event_sink: EventSink | None = None,
        event_batch_sink: EventBatchSink | None = None,
    ) -> None:
        if len(git_commit) != 40 or any(char not in "0123456789abcdef" for char in git_commit):
            raise ValueError("git commit must be a lowercase 40-character hash")
        self.repository = repository
        self._provider_factory = provider_factory
        self._plugin_factories = MappingProxyType(dict(plugin_factories))
        self._lock_path = Path(lock_path)
        self._config_loader = config_loader
        self._clock_ms = clock_ms
        self._git_commit = git_commit
        self._event_sink = event_sink
        self._event_batch_sink = event_batch_sink

    def run(self, request: RunRequest) -> RunReceipt:
        started = self._clock_ms()
        with ProcessLock(self._lock_path) as guard:
            if not guard.acquired:
                return RunReceipt(
                    None,
                    "OVERLAP_SKIPPED",
                    request.trigger,
                    started,
                    self._clock_ms(),
                    0,
                    0,
                    None,
                )
            return self._run_locked(request, started)

    def _run_locked(self, request: RunRequest, started: int) -> RunReceipt:
        config = self._config_loader(request)
        strategy = config.strategy
        market = config.market_data
        if set(strategy.instruments) - market.instruments.keys():
            raise ValueError("strategy instruments must exist in market-data mappings")
        provider = self._provider_factory(market)
        latest_open = provider.latest_closed_open_time_ms()
        first_open = latest_open - (strategy.history_minutes - 1) * 60_000
        if first_open < 0:
            raise ValueError("configured history precedes the provider epoch")

        all_candles: list[ClosedCandle] = []
        by_instrument = {}
        sync = MarketSyncService(provider, self.repository)
        for instrument_id in strategy.instruments:
            candles = sync.sync_range(
                InstrumentMapping(instrument_id, market.instruments[instrument_id]),
                first_open,
                latest_open,
            )
            by_instrument[instrument_id] = candles
            all_candles.extend(candles)
        data_hash = hash_candles(all_candles)
        strategy_hash = hash_strategy(strategy)
        market_hash = hash_market_data(market)
        run_id = self._run_id(strategy_hash, market_hash, latest_open + 59_999, data_hash)
        running = RunRecord(
            run_id=run_id,
            status="RUNNING",
            started_at_ms=started,
            completed_at_ms=None,
            strategy_name=strategy.name,
            strategy_version=strategy.version,
            strategy_config_hash=strategy_hash,
            provider_id=market.provider,
            market_type=market.market_type,
            market_config_hash=market_hash,
            git_commit=self._git_commit,
            data_start_open_ms=first_open,
            data_end_close_ms=latest_open + 59_999,
            data_hash=data_hash,
            error=None,
        )
        self.repository.store_run(running)
        notification_warnings = list(
            self._emit_events(
                (
                    NotificationEvent(
                        "run_started",
                        run_id,
                        None,
                        strategy.name,
                        1,
                        {
                            "status": "RUNNING",
                            "instrument_count": len(strategy.instruments),
                            "evaluation_time_ms": latest_open + 59_999,
                        },
                    ),
                )
            )
        )

        try:
            graph = IndicatorGraph(
                nodes=configured_nodes(strategy), factories=self._plugin_factories
            )
            policy = OrderedDecisionPlugin(configured_decision_signals(strategy))
            observations: list[Observation] = []
            decisions: list[Decision] = []
            for instrument_id in strategy.instruments:
                context = RunContext(
                    instrument_id,
                    latest_open + 59_999,
                    resample_roles(
                        by_instrument[instrument_id],
                        strategy.roles,
                        evaluation_time_ms=latest_open + 59_999,
                    ),
                    strategy.roles,
                )
                instrument_observations = graph.execute(context)
                observations.extend(instrument_observations.values())
                decisions.append(policy.decide(context, instrument_observations))
            completed = self._clock_ms()
            self.repository.commit_run(
                run_id,
                tuple(observation_record(run_id, item) for item in observations),
                tuple(decision_record(run_id, item) for item in decisions),
                completed_at_ms=completed,
            )
        except Exception as exc:
            completed = self._clock_ms()
            error = self._bounded_error(exc)
            self.repository.finish_run(run_id, "FAILED", completed_at_ms=completed, error=error)
            self._emit_terminal(
                config,
                run_id,
                (),
                False,
                error,
                evaluation_time_ms=latest_open + 59_999,
            )
            return RunReceipt(
                run_id,
                "FAILED",
                request.trigger,
                started,
                completed,
                len(strategy.instruments),
                0,
                error,
            )

        notification_warnings.extend(
            self._emit_terminal(
                config,
                run_id,
                tuple(decisions),
                True,
                None,
                evaluation_time_ms=latest_open + 59_999,
            )
        )
        warning = (
            None
            if not notification_warnings
            else "NOTIFICATION_WARNING: " + ", ".join(notification_warnings)
        )
        return RunReceipt(
            run_id,
            "SUCCEEDED" if warning is None else "SUCCEEDED_WITH_WARNINGS",
            request.trigger,
            started,
            completed,
            len(strategy.instruments),
            len(decisions),
            warning,
        )

    def _emit_terminal(
        self,
        config: RuntimeConfiguration,
        run_id: str,
        decisions: tuple[Decision, ...],
        succeeded: bool,
        error: str | None,
        *,
        evaluation_time_ms: int,
    ) -> tuple[str, ...]:
        if self._event_sink is None:
            return ()
        events: list[NotificationEvent] = []
        for decision in decisions:
            event_type = "decision_found" if decision.status == "READY" else "no_decision"
            events.append(
                NotificationEvent(
                    event_type,
                    run_id,
                    decision.instrument_id,
                    config.strategy.name,
                    1,
                    _decision_notification_payload(decision, evaluation_time_ms=evaluation_time_ms),
                )
            )
        lifecycle_payload: dict[str, None | bool | int | str] = {
            "status": "SUCCEEDED" if succeeded else "FAILED",
            "instrument_count": len(config.strategy.instruments),
            "decision_count": len(decisions),
        }
        if error is not None:
            lifecycle_payload["error_category"] = error.partition(":")[0][:64]
        events.append(
            NotificationEvent(
                "run_succeeded" if succeeded else "run_failed",
                run_id,
                None,
                config.strategy.name,
                1,
                lifecycle_payload,
            )
        )
        return self._emit_events(tuple(events))

    def _emit_events(self, events: tuple[NotificationEvent, ...]) -> tuple[str, ...]:
        if self._event_sink is None:
            return ()
        if self._event_batch_sink is not None and len(events) > 1:
            try:
                receipt = self._event_batch_sink(events)
                if getattr(receipt, "outcome", None) in {"PARTIAL_FAILURE", "ALL_FAILURE"}:
                    return tuple(event.event_type for event in events)
                return ()
            except Exception:
                LOGGER.warning(
                    "notification_batch_failed",
                    extra={"run_id": events[0].run_id},
                )
                return tuple(event.event_type for event in events)
        failed: list[str] = []
        for event in events:
            try:
                receipt = self._event_sink(event)
                if getattr(receipt, "outcome", None) in {"PARTIAL_FAILURE", "ALL_FAILURE"}:
                    failed.append(event.event_type)
            except Exception:
                LOGGER.warning(
                    "notification_event_failed",
                    extra={"event_type": event.event_type, "run_id": event.run_id},
                )
                failed.append(event.event_type)
        return tuple(failed)

    @staticmethod
    def _run_id(
        strategy_hash: str, market_hash: str, evaluation_time_ms: int, data_hash: str
    ) -> str:
        payload = json.dumps(
            [strategy_hash, market_hash, evaluation_time_ms, data_hash], separators=(",", ":")
        ).encode()
        return sha256(b"engine-run-v1\0" + payload).hexdigest()

    @staticmethod
    def _bounded_error(exc: Exception) -> str:
        text = f"{type(exc).__name__}: {exc}".replace("\n", " ").replace("\r", " ")
        return text[:4000] or type(exc).__name__
