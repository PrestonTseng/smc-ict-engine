from __future__ import annotations

from dataclasses import replace

from trading_research.application.execution_simulator import TradeRecord
from trading_research.domain import Decision
from trading_research.domain.backtesting import PipelineTrace, ReplayEvaluation


def trade(
    decision_hash: str,
    *,
    instrument: str,
    direction: str,
    net_r: str,
    exit_reason: str,
    entry_fee: str = "0.1",
    exit_fee: str = "0.1",
    slippage_cost: str = "0.05",
) -> TradeRecord:
    return TradeRecord(
        decision_hash=decision_hash,
        instrument_id=instrument,
        direction=direction,
        status="CLOSED",
        signal_time_ms=int(decision_hash[0]),
        entry_time_ms=60_000,
        exit_time_ms=120_000,
        requested_entry="100",
        stop="99",
        target="102",
        executed_entry="100",
        requested_exit="102",
        executed_exit="102",
        exit_reason=exit_reason,
        initial_risk="1",
        gross_price_return="0.02",
        entry_fee=entry_fee,
        exit_fee=exit_fee,
        slippage_cost=slippage_cost,
        net_price_return=net_r,
        gross_r=net_r,
        net_r=net_r,
        duration_minutes=2,
    )


def evaluation(status: str, decision_hash: str, failed: str | None) -> ReplayEvaluation:
    is_ready = status == "READY"
    decision = Decision(
        "BTC-USDT-PERP",
        status,
        "LONG" if is_ready else None,
        "100" if is_ready else None,
        "99" if is_ready else None,
        "102" if is_ready else None,
        failed,
        {},
    )
    return ReplayEvaluation(
        "BTC-USDT-PERP",
        59_999,
        decision,
        decision_hash,
        PipelineTrace("BTC-USDT-PERP", 59_999, (), failed, decision_hash),
    )


def test_metrics_are_normalized_and_grouped_in_deterministic_order() -> None:
    from trading_research.application.metrics import summarize_trades

    trades = (
        trade(
            "3" * 64,
            instrument="ETH-USDT-PERP",
            direction="SHORT",
            net_r="-2",
            exit_reason="STOP",
        ),
        trade(
            "1" * 64,
            instrument="BTC-USDT-PERP",
            direction="LONG",
            net_r="2",
            exit_reason="TARGET",
        ),
        trade(
            "4" * 64,
            instrument="ETH-USDT-PERP",
            direction="SHORT",
            net_r="-1",
            exit_reason="STOP",
        ),
        trade(
            "2" * 64,
            instrument="BTC-USDT-PERP",
            direction="LONG",
            net_r="1",
            exit_reason="TARGET",
        ),
    )

    report = summarize_trades(trades)

    assert report.overall.trade_count == 4
    assert report.overall.win_rate == "0.5"
    assert report.overall.mean_net_r == "0"
    assert report.overall.median_net_r == "0"
    assert report.overall.gross_profit_r == "3"
    assert report.overall.gross_loss_r == "3"
    assert report.overall.profit_factor == "1"
    assert report.overall.cumulative_net_r == "0"
    assert report.overall.maximum_drawdown_r == "3"
    assert report.overall.total_cost == "1"
    assert report.overall.exit_reason_counts == (("STOP", 2), ("TARGET", 2))
    assert tuple(key for key, _ in report.by_instrument) == (
        "BTC-USDT-PERP",
        "ETH-USDT-PERP",
    )
    assert tuple(key for key, _ in report.by_direction) == ("LONG", "SHORT")


def test_zero_loss_and_empty_denominators_are_explicit_and_dispositions_are_counted() -> None:
    from trading_research.application.metrics import summarize_backtest

    winner = trade(
        "1" * 64,
        instrument="BTC-USDT-PERP",
        direction="LONG",
        net_r="1",
        exit_reason="TARGET",
    )
    expired = replace(
        trade(
            "2" * 64,
            instrument="BTC-USDT-PERP",
            direction="SHORT",
            net_r="0",
            exit_reason="ENTRY_EXPIRED",
        ),
        status="EXPIRED",
        net_r=None,
        entry_fee=None,
        exit_fee=None,
        slippage_cost=None,
    )
    evaluations = (
        evaluation("READY", "1" * 64, None),
        evaluation("NO_TRADE", "2" * 64, "trend"),
        evaluation("UNAVAILABLE", "3" * 64, "levels"),
    )

    report = summarize_backtest(evaluations, (expired, winner))

    assert report.overall.trade_count == 1
    assert report.overall.profit_factor is None
    assert report.overall.status_counts == (("CLOSED", 1), ("EXPIRED", 1))
    assert report.decision_status_counts == (("NO_TRADE", 1), ("READY", 1), ("UNAVAILABLE", 1))
    assert report.unavailable_reason_counts == (("levels", 1),)
    empty = summarize_backtest((), ())
    assert empty.overall.win_rate is None
    assert empty.overall.mean_net_r is None
    assert empty.overall.median_net_r is None
    assert empty.overall.profit_factor is None
