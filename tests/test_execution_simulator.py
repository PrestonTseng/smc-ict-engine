from __future__ import annotations

from trading_research.configuration.models import (
    BacktestCostConfig,
    BacktestEntryConfig,
    BacktestExecutionConfig,
)
from trading_research.domain import ClosedCandle, Decision
from trading_research.domain.backtesting import PipelineTrace, ReplayEvaluation


def candle(
    minute: int,
    *,
    opening: str,
    high: str,
    low: str,
    close: str,
    instrument: str = "BTC-USDT-PERP",
) -> ClosedCandle:
    return ClosedCandle(
        provider_id="okx_swap",
        market_type="LINEAR_PERPETUAL",
        instrument_id=instrument,
        provider_symbol=instrument.replace("-PERP", "-SWAP"),
        interval="1m",
        open_time_ms=minute * 60_000,
        close_time_ms=minute * 60_000 + 59_999,
        open=opening,
        high=high,
        low=low,
        close=close,
        base_volume="1",
        quote_volume="100",
        source_fields={"contract_volume": "1"},
    )


def ready(
    minute: int = 0,
    *,
    direction: str = "LONG",
    entry: str = "100",
    stop: str = "99",
    target: str = "102",
    decision_hash: str = "a" * 64,
    instrument: str = "BTC-USDT-PERP",
) -> ReplayEvaluation:
    decision = Decision(
        instrument,
        "READY",
        direction,
        entry,
        stop,
        target,
        None,
        {},
    )
    evaluation_time_ms = minute * 60_000 + 59_999
    return ReplayEvaluation(
        instrument,
        evaluation_time_ms,
        decision,
        decision_hash,
        PipelineTrace(instrument, evaluation_time_ms, (), None, decision_hash),
    )


def simulator(
    *,
    expiry_bars: int = 6,
    maximum_holding_minutes: int = 360,
    fee_bps: str = "0",
    slippage_bps: str = "0",
):
    from trading_research.application.execution_simulator import ExecutionSimulator

    return ExecutionSimulator(
        entry=BacktestEntryConfig("touch_limit", expiry_bars),
        execution=BacktestExecutionConfig(maximum_holding_minutes, "stop_first", False),
        costs=BacktestCostConfig(fee_bps, slippage_bps),
        execution_bar_minutes=5,
    )


def test_long_limit_is_eligible_on_next_minute_then_exits_at_target() -> None:
    result = simulator().run(
        (ready(),),
        (
            candle(0, opening="100", high="102", low="99", close="101"),
            candle(1, opening="101", high="101", low="100", close="100.5"),
            candle(2, opening="101", high="102", low="100.5", close="102"),
        ),
    )

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.status == "CLOSED"
    assert trade.entry_time_ms == 60_000
    assert trade.exit_time_ms == 120_000
    assert trade.exit_reason == "TARGET"
    assert trade.executed_entry == "100"
    assert trade.executed_exit == "102"


def test_pending_entry_expires_after_six_execution_bars() -> None:
    rows = (
        *(
            candle(minute, opening="101", high="101", low="100.5", close="101")
            for minute in range(31)
        ),
        candle(31, opening="100", high="101", low="99", close="100"),
    )

    result = simulator(expiry_bars=6).run((ready(),), rows)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.status == "EXPIRED"
    assert trade.exit_reason == "ENTRY_EXPIRED"
    assert trade.entry_time_ms is None


def test_short_favorable_entry_gap_fills_at_limit_then_exits_at_target() -> None:
    result = simulator().run(
        (ready(direction="SHORT", stop="101", target="98"),),
        (
            candle(0, opening="100", high="100", low="100", close="100"),
            candle(1, opening="100.5", high="100.5", low="99", close="99"),
            candle(2, opening="99", high="100", low="98.5", close="99"),
            candle(3, opening="99", high="99", low="98", close="98"),
        ),
    )

    trade = result.trades[0]
    assert trade.direction == "SHORT"
    assert trade.entry_time_ms == 60_000
    assert trade.exit_time_ms == 180_000
    assert trade.exit_reason == "TARGET"
    assert trade.executed_entry == "100"
    assert trade.executed_exit == "98"


def test_same_minute_stop_and_target_collision_exits_at_stop() -> None:
    result = simulator().run(
        (ready(),),
        (
            candle(0, opening="100", high="100", low="100", close="100"),
            candle(1, opening="100", high="101", low="100", close="101"),
            candle(2, opening="101", high="102", low="99", close="101"),
        ),
    )

    trade = result.trades[0]
    assert trade.exit_reason == "STOP"
    assert trade.requested_exit == "99"
    assert trade.executed_exit == "99"


def test_long_stop_gap_uses_the_worse_opening_price() -> None:
    result = simulator().run(
        (ready(),),
        (
            candle(0, opening="100", high="100", low="100", close="100"),
            candle(1, opening="100", high="101", low="100", close="101"),
            candle(2, opening="97", high="98", low="96", close="97"),
        ),
    )

    trade = result.trades[0]
    assert trade.exit_reason == "STOP"
    assert trade.requested_exit == "99"
    assert trade.executed_exit == "97"


def test_open_trade_times_out_at_close_of_360th_permitted_minute() -> None:
    rows = (
        candle(0, opening="100", high="100", low="100", close="100"),
        *(
            candle(minute, opening="100.5", high="101", low="100", close="100.5")
            for minute in range(1, 362)
        ),
    )

    result = simulator(maximum_holding_minutes=360).run((ready(),), rows)

    trade = result.trades[0]
    assert trade.exit_reason == "TIMEOUT"
    assert trade.exit_time_ms == 360 * 60_000
    assert trade.requested_exit == "100.5"
    assert trade.executed_exit == "100.5"
    assert trade.duration_minutes == 360


def test_open_trade_at_dataset_end_exits_at_last_close() -> None:
    result = simulator().run(
        (ready(),),
        (
            candle(0, opening="100", high="100", low="100", close="100"),
            candle(1, opening="100", high="101", low="100", close="100.5"),
            candle(2, opening="100.5", high="101", low="100", close="100.75"),
        ),
    )

    trade = result.trades[0]
    assert trade.exit_reason == "END_OF_DATA"
    assert trade.requested_exit == "100.75"
    assert trade.executed_exit == "100.75"


def test_long_trade_applies_adverse_slippage_fees_and_normalized_accounting() -> None:
    result = simulator(fee_bps="5", slippage_bps="2").run(
        (ready(),),
        (
            candle(0, opening="100", high="100", low="100", close="100"),
            candle(1, opening="100", high="101", low="100", close="101"),
            candle(2, opening="101", high="102", low="100", close="102"),
        ),
    )

    trade = result.trades[0]
    assert trade.executed_entry == "100.02"
    assert trade.executed_exit == "101.9796"
    assert trade.initial_risk == "1"
    assert trade.gross_price_return == "0.02"
    assert trade.entry_fee == "0.05001"
    assert trade.exit_fee == "0.0509898"
    assert trade.slippage_cost == "0.0404"
    assert trade.net_price_return == "0.018586002"
    assert trade.gross_r == "2"
    assert trade.net_r == "1.8586002"


def test_each_instrument_has_independent_state_and_output_order_is_deterministic() -> None:
    eth = "ETH-USDT-PERP"
    evaluations = (
        ready(decision_hash="b" * 64, instrument=eth),
        ready(decision_hash="a" * 64),
    )
    rows = tuple(
        candle(
            minute,
            opening=opening,
            high=high,
            low=low,
            close=close,
            instrument=instrument,
        )
        for instrument in (eth, "BTC-USDT-PERP")
        for minute, opening, high, low, close in (
            (0, "100", "100", "100", "100"),
            (1, "100", "101", "100", "101"),
            (2, "101", "102", "100", "102"),
        )
    )

    result = simulator().run(evaluations, tuple(reversed(rows)))

    assert tuple(trade.instrument_id for trade in result.trades) == (
        "BTC-USDT-PERP",
        "ETH-USDT-PERP",
    )
    assert all(trade.exit_reason == "TARGET" for trade in result.trades)
