from __future__ import annotations

from collections.abc import Mapping
from multiprocessing import Event, Process
from pathlib import Path
from time import sleep
from typing import Any, cast

import pytest


def test_run_identity_changes_with_code_hash() -> None:
    from trading_research.application.runtime import EngineRunner

    first = EngineRunner._run_id("a" * 64, "b" * 64, 59_999, "c" * 64, "d" * 64)
    second = EngineRunner._run_id("a" * 64, "b" * 64, 59_999, "c" * 64, "e" * 64)

    assert first != second


def test_runtime_paths_are_fixed_derivations_of_the_required_data_folder(tmp_path: Path) -> None:
    from trading_research.composition.runtime_services import RuntimePaths

    paths = RuntimePaths.from_environ({"DATA_FOLDER": str(tmp_path)})

    assert paths.database == tmp_path / "trading_research.db"
    assert paths.lock == tmp_path / "engine.lock"
    assert paths.health == tmp_path / "scheduler.ready"
    assert paths.backtests == tmp_path / "backtests"
    with pytest.raises(ValueError, match="DATA_FOLDER"):
        RuntimePaths.from_environ({})
    with pytest.raises(ValueError, match="absolute normalized"):
        RuntimePaths.from_environ({"DATA_FOLDER": "relative/data"})


def test_run_readiness_uses_historical_sync_service_before_analysis(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research.application import runtime
    from trading_research.application.ports import InstrumentMapping
    from trading_research.configuration.models import (
        MarketDataConfig,
        NotificationConfig,
        StrategyConfig,
        frozen_mapping,
    )

    class ReadinessFailure(RuntimeError):
        pass

    calls: list[tuple[InstrumentMapping, int, int]] = []

    class HistoricalSync:
        def __init__(self, provider: object, repository: object) -> None:
            assert isinstance(provider, Provider)
            assert repository is not None

        def sync_range(
            self, mapping: InstrumentMapping, start_open_time_ms: int, end_open_time_ms: int
        ) -> object:
            calls.append((mapping, start_open_time_ms, end_open_time_ms))
            raise ReadinessFailure("readiness failed")

    class Provider:
        provider_id = "binance_usdm"

        def latest_closed_open_time_ms(self) -> int:
            return 120_000

        def fetch_page(self, _request: object) -> object:
            raise AssertionError("run bypassed the shared readiness service")

    strategy = StrategyConfig(
        "fixture-strategy",
        "1",
        ("BTC-USDT-PERP",),
        2,
        frozen_mapping({"execution": "5m"}),
        (),
    )
    market = MarketDataConfig(
        "binance_usdm",
        "LINEAR_PERPETUAL",
        frozen_mapping({"BTC-USDT-PERP": "BTCUSDT"}),
    )
    config = runtime.RuntimeConfiguration(
        strategy,
        market,
        NotificationConfig(False, frozen_mapping({})),
    )
    monkeypatch.setattr(runtime, "HistoricalRangeSyncService", HistoricalSync, raising=False)
    runner = runtime.EngineRunner(
        repository=cast(Any, object()),
        provider_factory=lambda _market: cast(Any, Provider()),
        plugin_factories={},
        lock_path=tmp_path / "engine.lock",
        config_loader=lambda _request: config,
        clock_ms=lambda: 120_000,
        code_hash="a" * 64,
    )

    with pytest.raises(ReadinessFailure, match="readiness failed"):
        runner._run_locked(runtime.RunRequest("strategy.yaml", "market.yaml", None, "manual"), 0)

    assert calls == [(InstrumentMapping("BTC-USDT-PERP", "BTCUSDT"), 60_000, 120_000)]


def _hold_process_lock(path: str, ready: Any) -> None:
    from trading_research.application.runtime import ProcessLock

    with ProcessLock(path) as lock:
        assert lock.acquired
        ready.set()
        sleep(30)


def test_run_once_wires_sqlite_dedup_and_reports_final_flush_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research.adapters.persistence.sqlite import SQLiteRepository
    from trading_research.application.notifications import RoutingReceipt
    from trading_research.application.runtime import RunReceipt
    from trading_research.composition import runtime_services

    class Router:
        def __init__(
            self,
            _config: object,
            *,
            adapter_factory: object,
            clock_seconds: object,
            deduplication_store: object,
        ) -> None:
            assert adapter_factory is not None
            assert clock_seconds is not None
            assert isinstance(deduplication_store, SQLiteRepository)

        def deliver(self, _event: object) -> RoutingReceipt:
            return RoutingReceipt("ALL_SUCCESS", ())

        def deliver_all(self, _events: object) -> RoutingReceipt:
            return RoutingReceipt("QUEUED", ())

        def close(self) -> RoutingReceipt:
            return RoutingReceipt("ALL_FAILURE", ())

    class Runner:
        _config_loader: object
        _event_sink: object
        _event_batch_sink: object

        def run(self, _request: object) -> RunReceipt:
            return RunReceipt("run", "SUCCEEDED", "manual", 1, 2, 1, 1, None)

    monkeypatch.setattr(runtime_services, "NotificationRouter", Router)
    monkeypatch.setattr(runtime_services, "load_notifications", lambda _path: object())
    monkeypatch.setattr(runtime_services, "build_engine_runner", lambda *_args: Runner())

    receipt = runtime_services.run_once(
        strategy="strategy.yaml",
        market_data="market.yaml",
        notifications="notifications.yaml",
        database=tmp_path / "runtime.sqlite3",
        lock_path=tmp_path / "engine.lock",
        trigger="manual",
    )

    assert receipt.status == "SUCCEEDED_WITH_WARNINGS"
    assert receipt.error == "NOTIFICATION_WARNING: final_flush"


def test_run_once_selects_notification_adapter_from_closed_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research.application.notifications import RoutingReceipt
    from trading_research.application.runtime import RunReceipt
    from trading_research.composition import runtime_services
    from trading_research.configuration.models import (
        BatchingConfig,
        DeduplicationConfig,
        NotificationConfig,
        NotificationDestination,
        RedactionConfig,
        RetryConfig,
        SecretRef,
        frozen_mapping,
    )

    destination = NotificationDestination(
        "discord_webhook",
        True,
        ("run_started",),
        SecretRef("env", "FICTIONAL_DISCORD_HOOK"),
        1,
        RetryConfig(1, ()),
        DeduplicationConfig(1, ("event_type", "run_id")),
        BatchingConfig(1, 1),
        RedactionConfig((), ()),
        "warning",
    )
    config = NotificationConfig(True, frozen_mapping({"discord_debug": destination}))
    built: list[type[object]] = []

    class Adapter:
        adapter_id = "discord_webhook"

        def __init__(self, *_args: object, **_kwargs: object) -> None:
            built.append(type(self))

        def deliver(self, _event: object) -> object:
            raise AssertionError("adapter construction is the behavior under test")

    class Registry:
        def resolve(self, adapter_id: str) -> type[Adapter]:
            assert adapter_id == "discord_webhook"
            return Adapter

    class Root:
        notifiers = Registry()

    class Router:
        def __init__(self, _config: object, *, adapter_factory: object, **_kwargs: object) -> None:
            factory = cast(Any, adapter_factory)
            factory("discord_debug", destination)

        def deliver(self, _event: object) -> RoutingReceipt:
            return RoutingReceipt("ALL_SUCCESS", ())

        def deliver_all(self, _events: object) -> RoutingReceipt:
            return RoutingReceipt("ALL_SUCCESS", ())

        def close(self) -> RoutingReceipt:
            return RoutingReceipt("ALL_SUCCESS", ())

    class Runner:
        _config_loader: object
        _event_sink: object
        _event_batch_sink: object

        def run(self, _request: object) -> RunReceipt:
            return RunReceipt("run", "SUCCEEDED", "manual", 1, 2, 1, 0, None)

    monkeypatch.setattr(runtime_services, "notification_composition_root", lambda: Root())
    monkeypatch.setattr(runtime_services, "NotificationRouter", Router)
    monkeypatch.setattr(runtime_services, "load_notifications", lambda _path: config)
    monkeypatch.setattr(runtime_services, "build_engine_runner", lambda *_args: Runner())

    receipt = runtime_services.run_once(
        strategy="strategy.yaml",
        market_data="market.yaml",
        notifications="notifications.yaml",
        database=tmp_path / "runtime.sqlite3",
        lock_path=tmp_path / "engine.lock",
        trigger="manual",
    )

    assert receipt.status == "SUCCEEDED"
    assert built == [Adapter]


def test_manual_and_scheduled_runs_share_one_deterministic_engine_path(tmp_path: Path) -> None:
    from trading_research.adapters.persistence.sqlite import SQLiteRepository
    from trading_research.application.graph import ConfiguredNode
    from trading_research.application.ports import KlinePage
    from trading_research.application.runtime import (
        EngineRunner,
        RunRequest,
        RuntimeConfiguration,
    )
    from trading_research.configuration.models import (
        MarketDataConfig,
        NotificationConfig,
        SignalConfig,
        StrategyConfig,
        frozen_mapping,
    )
    from trading_research.domain import ClosedCandle, Observation

    strategy = StrategyConfig(
        name="fixture-strategy",
        version="1",
        instruments=("BTC-USDT-PERP",),
        history_minutes=5,
        roles=frozen_mapping({"execution": "5m"}),
        signals=(
            SignalConfig(
                "smc.swing_structure",
                "execution",
                (),
                frozen_mapping({"swing_length": 10, "show_labels": False}),
                True,
                "REJECT",
                1,
            ),
        ),
    )
    market = MarketDataConfig(
        "binance_usdm",
        "LINEAR_PERPETUAL",
        frozen_mapping({"BTC-USDT-PERP": "BTCUSDT"}),
    )
    runtime_config = RuntimeConfiguration(
        strategy=strategy,
        market_data=market,
        notifications=NotificationConfig(False, frozen_mapping({})),
    )
    candles = tuple(
        ClosedCandle(
            provider_id="binance_usdm",
            market_type="LINEAR_PERPETUAL",
            instrument_id="BTC-USDT-PERP",
            provider_symbol="BTCUSDT",
            interval="1m",
            open_time_ms=minute * 60_000,
            close_time_ms=minute * 60_000 + 59_999,
            open="100",
            high="101",
            low="99",
            close="100",
            base_volume="1",
            quote_volume="100",
            source_fields={
                "trade_count": 1,
                "taker_buy_base_volume": "0.5",
                "taker_buy_quote_volume": "50",
            },
        )
        for minute in range(5)
    )

    class Provider:
        provider_id = "binance_usdm"

        def validate_instrument(self, mapping: object) -> None:
            return None

        def server_time_ms(self) -> int:
            return 300_000

        def latest_closed_open_time_ms(self) -> int:
            return 240_000

        def fetch_page(self, request: object) -> KlinePage:
            return KlinePage(candles, None, True)

    parameter_hash = ConfiguredNode(
        "smc.swing_structure",
        "smc.swing_structure",
        "execution",
        (),
        {"swing_length": 10, "show_labels": False},
        1,
    ).parameter_hash

    class Plugin:
        plugin_id = "smc.swing_structure"

        def evaluate(self, context: object, dependencies: Mapping[str, Observation]) -> Observation:
            from trading_research.application.graph import RunContext

            runtime_context = cast(RunContext, context)
            instrument_id = runtime_context.instrument_id
            evaluation_time_ms = runtime_context.evaluation_time_ms
            return Observation(
                signal_id=self.plugin_id,
                instrument_id=instrument_id,
                timeframe="5m",
                status="PASS",
                event_type="structure",
                direction=None,
                event_time_ms=evaluation_time_ms,
                known_time_ms=evaluation_time_ms,
                state="confirmed",
                dependency_ids=(),
                parameter_hash=parameter_hash,
                source_manifest_ids=("fixture",),
                payload_schema_version=1,
                bounded_reason="fixture pass",
                payload={},
            )

    def execute(trigger: str, root: Path, *, fail_events: bool = False):
        events = []

        def emit(event: object) -> None:
            if fail_events:
                raise RuntimeError("notification sink unavailable")
            events.append(event)

        runner = EngineRunner(
            repository=SQLiteRepository(root / "runtime.sqlite3"),
            provider_factory=lambda _config: Provider(),
            plugin_factories={"smc.swing_structure": lambda _parameters: Plugin()},
            lock_path=root / "engine.lock",
            config_loader=lambda _request: runtime_config,
            clock_ms=lambda: 300_000,
            code_hash="a" * 64,
            event_sink=emit,
        )
        receipt = runner.run(RunRequest("strategy.yaml", "market.yaml", None, trigger))
        return receipt, events, runner.repository

    manual, manual_events, manual_repository = execute("manual", tmp_path / "manual")
    scheduled, scheduled_events, scheduled_repository = execute("scheduled", tmp_path / "scheduled")

    assert manual.status == scheduled.status == "SUCCEEDED"
    assert manual.run_id == scheduled.run_id
    assert manual.decision_count == scheduled.decision_count == 1
    assert [event.event_type for event in manual_events] == [
        "run_started",
        "no_decision",
        "run_succeeded",
    ]
    assert [event.event_type for event in scheduled_events] == [
        "run_started",
        "no_decision",
        "run_succeeded",
    ]
    assert dict(manual_events[0].payload) == {
        "status": "RUNNING",
        "instrument_count": 1,
        "evaluation_time_ms": 299_999,
        "event_time_ms": 300_000,
    }
    assert dict(manual_events[1].payload) == {
        "status": "UNAVAILABLE",
        "evaluation_time_ms": 299_999,
        "closed_bar_time_ms": 299_999,
        "decision_id": dict(manual_repository.load_decisions(cast(str, manual.run_id))[0].payload)[
            "decision_hash"
        ],
        "first_failed_signal": "decision.levels",
    }
    assert dict(manual_events[2].payload) == {
        "status": "SUCCEEDED",
        "instrument_count": 1,
        "decision_count": 1,
        "event_time_ms": 300_000,
    }
    stored_run = manual_repository.load_run(manual.run_id)
    assert stored_run.status == "SUCCEEDED"
    assert stored_run.code_hash == "a" * 64
    assert stored_run.git_commit is None
    assert scheduled_repository.load_decisions(scheduled.run_id)[0].decision_status == "UNAVAILABLE"

    isolated, _, isolated_repository = execute(
        "manual", tmp_path / "isolated-notifier", fail_events=True
    )
    assert isolated.status == "SUCCEEDED_WITH_WARNINGS"
    assert isolated.error == "NOTIFICATION_WARNING: run_started, no_decision, run_succeeded"
    assert isolated_repository.load_run(isolated.run_id).status == "SUCCEEDED"


def test_process_lock_rejects_a_competing_process_and_releases_after_process_death(
    tmp_path: Path,
) -> None:
    from trading_research.application.runtime import ProcessLock

    lock_path = tmp_path / "engine.lock"
    ready = Event()
    holder = Process(target=_hold_process_lock, args=(str(lock_path), ready))
    holder.start()
    assert ready.wait(timeout=5)
    try:
        with ProcessLock(lock_path) as competing:
            assert competing.acquired is False
    finally:
        holder.terminate()
        holder.join(timeout=5)
    assert holder.is_alive() is False

    with ProcessLock(lock_path) as recovered:
        assert recovered.acquired is True


def test_ready_decision_notification_payload_contains_bounded_debug_evidence() -> None:
    from trading_research.application.runtime import _decision_notification_payload
    from trading_research.domain import Decision, hash_decision

    decision = Decision(
        instrument_id="BTC-USDT-PERP",
        status="READY",
        direction="LONG",
        entry_text="101.5",
        stop_text="99.25",
        target_text="106",
        first_failed_signal=None,
        payload={"observation_hashes": {"project.risk_levels": "a" * 64}},
    )

    payload = _decision_notification_payload(decision, evaluation_time_ms=900_000)

    assert payload == {
        "status": "READY",
        "direction": "LONG",
        "evaluation_time_ms": 900_000,
        "closed_bar_time_ms": 900_000,
        "entry": "101.5",
        "stop": "99.25",
        "target": "106",
        "reward_risk": "2",
        "decision_id": hash_decision(decision),
    }


@pytest.mark.parametrize(
    ("stop", "target", "expected_ratio"),
    [("100.5", "111.5", "10"), ("101.5", "106", "UNDEFINED")],
)
def test_ready_decision_notification_reward_risk_is_stable_and_non_crashing(
    stop: str, target: str, expected_ratio: str
) -> None:
    from trading_research.application.runtime import _decision_notification_payload
    from trading_research.domain import Decision

    decision = Decision(
        instrument_id="BTC-USDT-PERP",
        status="READY",
        direction="LONG",
        entry_text="101.5",
        stop_text=stop,
        target_text=target,
        first_failed_signal=None,
        payload={"observation_hashes": {}},
    )

    payload = _decision_notification_payload(decision, evaluation_time_ms=900_000)

    assert payload["reward_risk"] == expected_ratio


@pytest.mark.parametrize("status", ["NO_TRADE", "UNAVAILABLE"])
def test_no_decision_notification_payload_identifies_first_failed_signal(status: str) -> None:
    from trading_research.application.runtime import _decision_notification_payload
    from trading_research.domain import Decision, hash_decision

    decision = Decision(
        instrument_id="BTC-USDT-PERP",
        status=status,
        direction=None,
        entry_text=None,
        stop_text=None,
        target_text=None,
        first_failed_signal="ict.fair_value_gap",
        payload={"observation_hashes": {}},
    )

    assert _decision_notification_payload(decision, evaluation_time_ms=900_000) == {
        "status": status,
        "evaluation_time_ms": 900_000,
        "closed_bar_time_ms": 900_000,
        "decision_id": hash_decision(decision),
        "first_failed_signal": "ict.fair_value_gap",
    }


def test_restart_recovery_marks_interrupted_running_rows_failed(tmp_path: Path) -> None:
    from trading_research.adapters.persistence.sqlite import SQLiteRepository
    from trading_research.application.ports import RunRecord

    repository = SQLiteRepository(tmp_path / "runtime.sqlite3")
    repository.store_run(
        RunRecord(
            run_id="interrupted",
            status="RUNNING",
            started_at_ms=1,
            completed_at_ms=None,
            strategy_name="fixture",
            strategy_version="1",
            strategy_config_hash="a" * 64,
            provider_id="binance_usdm",
            market_type="LINEAR_PERPETUAL",
            market_config_hash="b" * 64,
            git_commit=None,
            code_hash="c" * 64,
            data_start_open_ms=0,
            data_end_close_ms=59_999,
            data_hash="d" * 64,
            error=None,
        )
    )

    recovered = repository.recover_running_runs(completed_at_ms=10, reason="PROCESS_RESTART")

    assert recovered == ("interrupted",)
    row = repository.load_run("interrupted")
    assert row is not None
    assert (row.status, row.completed_at_ms, row.error) == ("FAILED", 10, "PROCESS_RESTART")


def test_scheduler_recovery_cannot_fail_a_run_owned_by_an_active_process(tmp_path: Path) -> None:
    from trading_research.adapters.persistence.sqlite import SQLiteRepository
    from trading_research.application.ports import RunRecord
    from trading_research.application.runtime import ProcessLock, RunReceipt
    from trading_research.application.scheduler import InternalScheduler, RetryPolicy
    from trading_research.configuration.models import ScheduleConfig

    lock_path = tmp_path / "engine.lock"
    repository = SQLiteRepository(tmp_path / "runtime.sqlite3")
    repository.store_run(
        RunRecord(
            "active",
            "RUNNING",
            1,
            None,
            "fixture",
            "1",
            "a" * 64,
            "binance_usdm",
            "LINEAR_PERPETUAL",
            "b" * 64,
            None,
            "c" * 64,
            0,
            59_999,
            "d" * 64,
            None,
        )
    )
    service = InternalScheduler(
        ScheduleConfig(False, "UTC", ()),
        operation_factory=lambda _job: lambda: RunReceipt(
            None, "FAILED", "scheduled", 0, 0, 0, 0, None
        ),
        repository=repository,
        clock_ms=lambda: 10,
        retry_policy=RetryPolicy(1, ()),
        recovery_lock_path=lock_path,
    )

    with ProcessLock(lock_path) as active:
        assert active.acquired
        with pytest.raises(RuntimeError, match="active engine run"):
            service.start(paused=True)

    assert repository.load_run("active").status == "RUNNING"


def test_atomic_run_commit_rolls_back_evidence_when_a_decision_insert_fails(tmp_path: Path) -> None:
    from trading_research.adapters.persistence.sqlite import (
        PersistenceConflictError,
        SQLiteRepository,
    )
    from trading_research.application.ports import DecisionRecord, ObservationRecord, RunRecord

    repository = SQLiteRepository(tmp_path / "runtime.sqlite3")
    repository.store_run(
        RunRecord(
            "run",
            "RUNNING",
            1,
            None,
            "fixture",
            "1",
            "a" * 64,
            "binance_usdm",
            "LINEAR_PERPETUAL",
            "b" * 64,
            None,
            "c" * 64,
            0,
            59_999,
            "d" * 64,
            None,
        )
    )
    observation = ObservationRecord("run", "BTC-USDT-PERP", "signal", "PASS", 1, 1, "ok", {})
    invalid_decision = DecisionRecord(
        "missing-run",
        "BTC-USDT-PERP",
        "UNAVAILABLE",
        None,
        None,
        None,
        None,
        "signal",
        {},
    )

    with pytest.raises(PersistenceConflictError, match="PERSISTENCE_CONFLICT"):
        repository.commit_run("run", (observation,), (invalid_decision,), completed_at_ms=2)

    assert repository.load_observations("run") == ()
    assert repository.load_decisions("run") == ()
    run = repository.load_run("run")
    assert run is not None
    assert run.status == "RUNNING"
