from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from trading_research.application.graph import ConfiguredNode, RunContext
from trading_research.configuration.models import (
    BacktestPeriod,
    SignalConfig,
    StrategyConfig,
    frozen_mapping,
)
from trading_research.domain import ClosedCandle, Observation


def candle(minute: int) -> ClosedCandle:
    price = str(100 + minute)
    return ClosedCandle(
        provider_id="okx_swap",
        market_type="LINEAR_PERPETUAL",
        instrument_id="BTC-USDT-PERP",
        provider_symbol="BTC-USDT-SWAP",
        interval="1m",
        open_time_ms=minute * 60_000,
        close_time_ms=minute * 60_000 + 59_999,
        open=price,
        high=price,
        low=price,
        close=price,
        base_volume="1",
        quote_volume=price,
        source_fields={"contract_volume": "1"},
    )


@dataclass
class CandleSource:
    candles: tuple[ClosedCandle, ...]

    def load_candles(
        self,
        provider_id: str,
        market_type: str,
        instrument_id: str,
        start_open_ms: int,
        end_open_ms: int,
    ) -> tuple[ClosedCandle, ...]:
        return tuple(
            item
            for item in self.candles
            if item.provider_id == provider_id
            and item.market_type == market_type
            and item.instrument_id == instrument_id
            and start_open_ms <= item.open_time_ms <= end_open_ms
        )


class RecordingPlugin:
    plugin_id = "levels"

    def __init__(
        self,
        parameters: Mapping[str, object],
        seen: list[tuple[int, tuple[int, ...]]],
        *,
        status: str = "PASS",
        signal_id: str = "levels",
    ) -> None:
        self._parameter_hash = ConfiguredNode(
            signal_id, signal_id, "execution", (), parameters, 1, "5m"
        ).parameter_hash
        self._seen = seen
        self._status = status
        self._signal_id = signal_id

    def evaluate(self, context: RunContext, dependencies: Mapping[str, Observation]) -> Observation:
        del dependencies
        bars = context.candles_by_role["execution"]
        self._seen.append((context.evaluation_time_ms, tuple(bar.close_time_ms for bar in bars)))
        payload: dict[str, object] = {}
        if self._signal_id == "levels" and self._status == "PASS":
            payload = {
                "direction": "LONG",
                "entry_text": "100",
                "stop_text": "99",
                "target_text": "102",
            }
        return Observation.available(
            signal_id=self._signal_id,
            instrument_id=context.instrument_id,
            timeframe="5m",
            status=self._status,
            event_type=None,
            direction=None,
            event_time_ms=context.evaluation_time_ms,
            known_time_ms=context.evaluation_time_ms,
            state="fixture",
            dependency_ids=(),
            parameter_hash=self._parameter_hash,
            source_manifest_ids=(),
            payload_schema_version=1,
            bounded_reason=f"FIXTURE_{self._status}",
            payload=payload,
        )


def strategy(signals: tuple[SignalConfig, ...] | None = None) -> StrategyConfig:
    configured = signals or (
        SignalConfig.model_construct(
            id="levels",
            role="execution",
            depends_on=(),
            parameters=frozen_mapping({}),
            required=True,
            effect="LEVELS",
            order=1,
        ),
    )
    return StrategyConfig.model_construct(
        name="fixture",
        version="1",
        instruments=("BTC-USDT-PERP",),
        history_minutes=5,
        roles=frozen_mapping({"execution": "5m"}),
        signals=configured,
    )


def test_replay_uses_exact_boundaries_and_never_exposes_future_bars() -> None:
    from trading_research.application.backtesting import PointInTimeReplay

    seen: list[tuple[int, tuple[int, ...]]] = []
    replay = PointInTimeReplay(
        strategy=strategy(),
        provider_id="okx_swap",
        market_type="LINEAR_PERPETUAL",
        candle_source=CandleSource(tuple(candle(minute) for minute in range(15))),
        plugin_factories={"levels": lambda parameters: RecordingPlugin(parameters, seen)},
    )

    result = replay.run(BacktestPeriod(5 * 60_000, 14 * 60_000))

    assert tuple(item.evaluation_time_ms for item in result.evaluations) == (599_999, 899_999)
    assert all(max(close_times) <= evaluation for evaluation, close_times in seen)
    assert seen == [(599_999, (599_999,)), (899_999, (899_999,))]
    assert all(item.decision.status == "READY" for item in result.evaluations)


def test_appending_future_candles_cannot_change_earlier_replay() -> None:
    from trading_research.application.backtesting import PointInTimeReplay

    period = BacktestPeriod(5 * 60_000, 14 * 60_000)

    def execute(rows: tuple[ClosedCandle, ...]) -> object:
        return PointInTimeReplay(
            strategy=strategy(),
            provider_id="okx_swap",
            market_type="LINEAR_PERPETUAL",
            candle_source=CandleSource(rows),
            plugin_factories={"levels": lambda parameters: RecordingPlugin(parameters, [])},
        ).run(period)

    prefix = tuple(candle(minute) for minute in range(15))

    assert execute(prefix) == execute(prefix + tuple(candle(minute) for minute in range(15, 25)))


def test_trace_covers_every_indicator_and_gate_and_preserves_first_rejection() -> None:
    from trading_research.application.backtesting import PointInTimeReplay

    signals = tuple(
        SignalConfig.model_construct(
            id=signal_id,
            role="execution",
            depends_on=(),
            parameters=frozen_mapping({}),
            required=True,
            effect="REJECT",
            order=order,
        )
        for order, signal_id in enumerate(("a", "b", "c"), start=1)
    )
    statuses = {"a": "PASS", "b": "FAIL", "c": "PASS"}
    factories = {
        signal_id: (
            lambda parameters, signal_id=signal_id: RecordingPlugin(
                parameters, [], status=statuses[signal_id], signal_id=signal_id
            )
        )
        for signal_id in statuses
    }
    replay = PointInTimeReplay(
        strategy=strategy(signals),
        provider_id="okx_swap",
        market_type="LINEAR_PERPETUAL",
        candle_source=CandleSource(tuple(candle(minute) for minute in range(10))),
        plugin_factories=factories,
    )

    evaluation = replay.run(BacktestPeriod(5 * 60_000, 9 * 60_000)).evaluations[0]

    assert evaluation.decision.status == "NO_TRADE"
    assert evaluation.trace.first_rejection == "b"
    assert tuple((step.kind, step.step_id, step.state) for step in evaluation.trace.steps) == (
        ("INDICATOR", "a", "PASS"),
        ("INDICATOR", "b", "FAIL"),
        ("INDICATOR", "c", "PASS"),
        ("DECISION_GATE", "a", "PASS"),
        ("DECISION_GATE", "b", "REJECT"),
        ("DECISION_GATE", "c", "SKIPPED_AFTER_REJECTION"),
    )
    assert all(step.reason and step.output_hash for step in evaluation.trace.steps[:5])
    assert evaluation.trace.steps[3].dependency_hashes == (
        ("a", evaluation.trace.steps[0].output_hash),
    )
    assert evaluation.trace.steps[-1].output_hash is None


def test_backtest_identity_hashes_every_immutable_input() -> None:
    from trading_research.application.backtesting import BacktestIdentity, RequiredRange

    values = dict(
        scenario_hash="a" * 64,
        strategy_hash="b" * 64,
        market_data_hash="c" * 64,
        candle_data_hash="d" * 64,
        git_commit="e" * 40,
        period=BacktestPeriod(300_000, 840_000),
        required_range=RequiredRange(0, 840_000),
    )

    first = BacktestIdentity.create(**values)
    second = BacktestIdentity.create(**values)
    changed = BacktestIdentity.create(**(values | {"candle_data_hash": "f" * 64}))

    assert first == second
    assert len(first.backtest_id) == 64
    assert first.backtest_id != changed.backtest_id


def test_trace_uses_the_ordered_policy_unavailable_result_for_missing_levels() -> None:
    from trading_research.application.backtesting import PointInTimeReplay

    signals = (
        SignalConfig.model_construct(
            id="a",
            role="execution",
            depends_on=(),
            parameters=frozen_mapping({}),
            required=True,
            effect="LEVELS",
            order=1,
        ),
    )
    replay = PointInTimeReplay(
        strategy=strategy(signals),
        provider_id="okx_swap",
        market_type="LINEAR_PERPETUAL",
        candle_source=CandleSource(tuple(candle(minute) for minute in range(10))),
        plugin_factories={"a": lambda parameters: RecordingPlugin(parameters, [], signal_id="a")},
    )

    evaluation = replay.run(BacktestPeriod(5 * 60_000, 9 * 60_000)).evaluations[0]

    assert evaluation.decision.status == "UNAVAILABLE"
    assert evaluation.trace.steps[-1].state == "UNAVAILABLE"
    assert evaluation.trace.steps[-1].reason == "REQUIRED_SIGNAL_UNAVAILABLE"


def test_replay_evaluation_has_a_canonical_ordered_record() -> None:
    from trading_research.application.backtesting import PointInTimeReplay

    evaluation = (
        PointInTimeReplay(
            strategy=strategy(),
            provider_id="okx_swap",
            market_type="LINEAR_PERPETUAL",
            candle_source=CandleSource(tuple(candle(minute) for minute in range(10))),
            plugin_factories={"levels": lambda parameters: RecordingPlugin(parameters, [])},
        )
        .run(BacktestPeriod(5 * 60_000, 9 * 60_000))
        .evaluations[0]
    )

    record = evaluation.canonical_dict()

    assert tuple(record) == (
        "instrument_id",
        "evaluation_time_ms",
        "decision",
        "decision_hash",
        "trace",
    )
    assert record["decision_hash"] == evaluation.decision_hash
    assert len(record["trace"]["steps"]) == 2
