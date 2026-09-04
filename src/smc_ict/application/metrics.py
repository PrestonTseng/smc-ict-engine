"""Deterministic normalized summaries for research-only simulated trades."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from decimal import Decimal, localcontext
from statistics import median

from smc_ict.application.execution_simulator import TradeRecord, _decimal_text
from smc_ict.domain.backtesting import ReplayEvaluation


@dataclass(frozen=True, slots=True)
class TradeMetrics:
    trade_count: int
    win_rate: str | None
    mean_net_r: str | None
    median_net_r: str | None
    gross_profit_r: str
    gross_loss_r: str
    profit_factor: str | None
    cumulative_net_r: str
    maximum_drawdown_r: str
    total_cost: str
    status_counts: tuple[tuple[str, int], ...]
    exit_reason_counts: tuple[tuple[str, int], ...]

    def canonical_dict(self) -> dict[str, object]:
        return {
            field: [list(item) for item in value]
            if field in {"status_counts", "exit_reason_counts"}
            else value
            for field, value in ((name, getattr(self, name)) for name in self.__dataclass_fields__)
        }


@dataclass(frozen=True, slots=True)
class MetricsReport:
    overall: TradeMetrics
    by_instrument: tuple[tuple[str, TradeMetrics], ...]
    by_direction: tuple[tuple[str, TradeMetrics], ...]
    decision_status_counts: tuple[tuple[str, int], ...] = ()
    unavailable_reason_counts: tuple[tuple[str, int], ...] = ()

    def canonical_dict(self) -> dict[str, object]:
        return {
            "overall": self.overall.canonical_dict(),
            "by_instrument": [
                [name, metrics.canonical_dict()] for name, metrics in self.by_instrument
            ],
            "by_direction": [
                [name, metrics.canonical_dict()] for name, metrics in self.by_direction
            ],
            "decision_status_counts": [list(item) for item in self.decision_status_counts],
            "unavailable_reason_counts": [list(item) for item in self.unavailable_reason_counts],
        }


def summarize_trades(trades: tuple[TradeRecord, ...]) -> MetricsReport:
    ordered = tuple(
        sorted(
            trades,
            key=lambda item: (item.signal_time_ms, item.instrument_id, item.decision_hash),
        )
    )
    instruments = sorted({trade.instrument_id for trade in ordered})
    directions = sorted({trade.direction for trade in ordered})
    return MetricsReport(
        _summarize(ordered),
        tuple(
            (key, _summarize(tuple(trade for trade in ordered if trade.instrument_id == key)))
            for key in instruments
        ),
        tuple(
            (key, _summarize(tuple(trade for trade in ordered if trade.direction == key)))
            for key in directions
        ),
    )


def summarize_backtest(
    evaluations: tuple[ReplayEvaluation, ...],
    trades: tuple[TradeRecord, ...],
) -> MetricsReport:
    report = summarize_trades(trades)
    decision_status_counts = Counter(evaluation.decision.status for evaluation in evaluations)
    unavailable_reason_counts = Counter(
        evaluation.decision.first_failed_signal
        for evaluation in evaluations
        if evaluation.decision.status == "UNAVAILABLE"
        and evaluation.decision.first_failed_signal is not None
    )
    return MetricsReport(
        report.overall,
        report.by_instrument,
        report.by_direction,
        tuple(sorted(decision_status_counts.items())),
        tuple(sorted(unavailable_reason_counts.items())),
    )


def _summarize(trades: tuple[TradeRecord, ...]) -> TradeMetrics:
    closed = tuple(trade for trade in trades if trade.status == "CLOSED")
    values = tuple(Decimal(trade.net_r or "0") for trade in closed)
    profits = sum((value for value in values if value > 0), Decimal(0))
    losses = -sum((value for value in values if value < 0), Decimal(0))
    total_cost = sum(
        (
            Decimal(trade.entry_fee or "0")
            + Decimal(trade.exit_fee or "0")
            + Decimal(trade.slippage_cost or "0")
            for trade in closed
        ),
        Decimal(0),
    )
    count = len(closed)
    with localcontext() as context:
        context.prec = 28
        win_rate = (
            None
            if count == 0
            else _decimal_text(Decimal(sum(value > 0 for value in values)) / count)
        )
        mean = None if count == 0 else _decimal_text(sum(values, Decimal(0)) / count)
        factor = None if losses == 0 else _decimal_text(profits / losses)
    median_value = None if count == 0 else _decimal_text(median(values))
    cumulative = sum(values, Decimal(0))
    return TradeMetrics(
        count,
        win_rate,
        mean,
        median_value,
        _decimal_text(profits),
        _decimal_text(losses),
        factor,
        _decimal_text(cumulative),
        _decimal_text(_maximum_drawdown(values)),
        _decimal_text(total_cost),
        tuple(sorted(Counter(trade.status for trade in trades).items())),
        tuple(sorted(Counter(trade.exit_reason for trade in trades).items())),
    )


def _maximum_drawdown(values: tuple[Decimal, ...]) -> Decimal:
    cumulative = Decimal(0)
    peak = Decimal(0)
    drawdown = Decimal(0)
    for value in values:
        cumulative += value
        peak = max(peak, cumulative)
        drawdown = max(drawdown, peak - cumulative)
    return drawdown
