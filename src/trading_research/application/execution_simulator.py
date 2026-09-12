"""Pure deterministic execution of replayed READY decisions against one-minute candles."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from trading_research.application.decimal_context import deterministic_decimal_context
from trading_research.configuration.models import (
    BacktestCostConfig,
    BacktestEntryConfig,
    BacktestExecutionConfig,
)
from trading_research.domain import ClosedCandle, DecimalText
from trading_research.domain.backtesting import ReplayEvaluation


@dataclass(frozen=True, slots=True)
class PendingTrade:
    evaluation: ReplayEvaluation
    expires_before_ms: int


@dataclass(frozen=True, slots=True)
class OpenTrade:
    evaluation: ReplayEvaluation
    entry_time_ms: int
    executed_entry: Decimal


@dataclass(frozen=True, slots=True)
class TradeRecord:
    decision_hash: str
    instrument_id: str
    direction: str
    status: str
    signal_time_ms: int
    entry_time_ms: int | None
    exit_time_ms: int | None
    requested_entry: str
    stop: str
    target: str
    executed_entry: str | None
    requested_exit: str | None
    executed_exit: str | None
    exit_reason: str
    initial_risk: str | None = None
    gross_price_return: str | None = None
    entry_fee: str | None = None
    exit_fee: str | None = None
    slippage_cost: str | None = None
    net_price_return: str | None = None
    gross_r: str | None = None
    net_r: str | None = None
    duration_minutes: int | None = None

    def canonical_dict(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class SimulationResult:
    trades: tuple[TradeRecord, ...]


class ExecutionSimulator:
    """Apply conservative V1 fill and exit rules without I/O or mutable outputs."""

    def __init__(
        self,
        *,
        entry: BacktestEntryConfig,
        execution: BacktestExecutionConfig,
        costs: BacktestCostConfig,
        execution_bar_minutes: int,
    ) -> None:
        self._entry = entry
        self._execution = execution
        self._costs = costs
        self._execution_bar_minutes = execution_bar_minutes
        with deterministic_decimal_context():
            self._fee_rate = Decimal(costs.taker_fee_bps) / Decimal("10000")
            self._slippage_rate = Decimal(costs.adverse_slippage_bps) / Decimal("10000")

    def run(
        self,
        evaluations: tuple[ReplayEvaluation, ...],
        candles: tuple[ClosedCandle, ...],
    ) -> SimulationResult:
        with deterministic_decimal_context():
            return self._run(evaluations, candles)

    def _run(
        self,
        evaluations: tuple[ReplayEvaluation, ...],
        candles: tuple[ClosedCandle, ...],
    ) -> SimulationResult:
        instruments = sorted(
            {item.instrument_id for item in evaluations} | {item.instrument_id for item in candles}
        )
        trades = tuple(
            trade
            for instrument in instruments
            for trade in self._run_instrument(
                tuple(item for item in evaluations if item.instrument_id == instrument),
                tuple(item for item in candles if item.instrument_id == instrument),
            )
        )
        return SimulationResult(
            tuple(
                sorted(
                    trades,
                    key=lambda item: (
                        item.signal_time_ms,
                        item.instrument_id,
                        item.decision_hash,
                    ),
                )
            )
        )

    def _run_instrument(
        self,
        evaluations: tuple[ReplayEvaluation, ...],
        candles: tuple[ClosedCandle, ...],
    ) -> tuple[TradeRecord, ...]:
        pending: PendingTrade | None = None
        opened: OpenTrade | None = None
        trades: list[TradeRecord] = []
        seen_decisions: set[str] = set()
        evaluations_by_time: dict[int, list[ReplayEvaluation]] = {}
        for evaluation in sorted(
            evaluations, key=lambda item: (item.evaluation_time_ms, item.decision_hash)
        ):
            evaluations_by_time.setdefault(evaluation.evaluation_time_ms, []).append(evaluation)
        ordered_candles = tuple(sorted(candles, key=lambda item: item.open_time_ms))
        for candle in ordered_candles:
            if pending is not None and candle.open_time_ms >= pending.expires_before_ms:
                trades.append(self._expired_record(pending))
                pending = None
            if pending is not None and self._touches_entry(pending, candle):
                entry_price = Decimal(pending.evaluation.decision.entry_text or "0")
                direction = pending.evaluation.decision.direction
                multiplier = Decimal(1) + (
                    self._slippage_rate if direction == "LONG" else -self._slippage_rate
                )
                opened = OpenTrade(
                    pending.evaluation, candle.open_time_ms, entry_price * multiplier
                )
                pending = None
            if opened is not None:
                stop = Decimal(opened.evaluation.decision.stop_text or "0")
                target = Decimal(opened.evaluation.decision.target_text or "0")
                if self._stop_touched(opened, candle, stop):
                    fill = self._stop_fill(opened, candle, stop)
                    trades.append(self._closed_record(opened, candle, stop, fill, "STOP"))
                    opened = None
                elif candle.open_time_ms > opened.entry_time_ms and self._target_touched(
                    opened, candle, target
                ):
                    trades.append(self._closed_record(opened, candle, target, target, "TARGET"))
                    opened = None
                elif (
                    self._duration_minutes(opened, candle)
                    >= self._execution.maximum_holding_minutes
                ):
                    closing = Decimal(candle.close)
                    trades.append(self._closed_record(opened, candle, closing, closing, "TIMEOUT"))
                    opened = None
            for evaluation in evaluations_by_time.get(candle.close_time_ms, []):
                if (
                    evaluation.decision.status != "READY"
                    or evaluation.decision_hash in seen_decisions
                ):
                    continue
                seen_decisions.add(evaluation.decision_hash)
                if pending is not None or opened is not None:
                    continue
                expiry_minutes = self._entry.expiry_execution_bars * self._execution_bar_minutes
                pending = PendingTrade(
                    evaluation,
                    evaluation.evaluation_time_ms + 1 + expiry_minutes * 60_000,
                )
        if pending is not None:
            trades.append(self._expired_record(pending))
        if opened is not None and ordered_candles:
            last = ordered_candles[-1]
            closing = Decimal(last.close)
            trades.append(self._closed_record(opened, last, closing, closing, "END_OF_DATA"))
        return tuple(trades)

    @staticmethod
    def _touches_entry(pending: PendingTrade, candle: ClosedCandle) -> bool:
        entry = Decimal(pending.evaluation.decision.entry_text or "0")
        touched = (
            Decimal(candle.low) <= entry
            if pending.evaluation.decision.direction == "LONG"
            else Decimal(candle.high) >= entry
        )
        return candle.open_time_ms < pending.expires_before_ms and touched

    @staticmethod
    def _target_touched(opened: OpenTrade, candle: ClosedCandle, target: Decimal) -> bool:
        if opened.evaluation.decision.direction == "LONG":
            return Decimal(candle.high) >= target
        return Decimal(candle.low) <= target

    @staticmethod
    def _stop_touched(opened: OpenTrade, candle: ClosedCandle, stop: Decimal) -> bool:
        if opened.evaluation.decision.direction == "LONG":
            return Decimal(candle.low) <= stop
        return Decimal(candle.high) >= stop

    @staticmethod
    def _stop_fill(opened: OpenTrade, candle: ClosedCandle, stop: Decimal) -> Decimal:
        opening = Decimal(candle.open)
        if opened.evaluation.decision.direction == "LONG":
            return min(stop, opening)
        return max(stop, opening)

    @staticmethod
    def _duration_minutes(opened: OpenTrade, candle: ClosedCandle) -> int:
        return (candle.open_time_ms - opened.entry_time_ms) // 60_000 + 1

    @staticmethod
    def _expired_record(pending: PendingTrade) -> TradeRecord:
        evaluation = pending.evaluation
        decision = evaluation.decision
        return TradeRecord(
            evaluation.decision_hash,
            decision.instrument_id,
            decision.direction or "",
            "EXPIRED",
            evaluation.evaluation_time_ms,
            None,
            None,
            decision.entry_text or "",
            decision.stop_text or "",
            decision.target_text or "",
            None,
            None,
            None,
            "ENTRY_EXPIRED",
        )

    def _closed_record(
        self,
        opened: OpenTrade,
        candle: ClosedCandle,
        requested_exit: Decimal,
        base_exit: Decimal,
        reason: str,
    ) -> TradeRecord:
        decision = opened.evaluation.decision
        requested_entry = Decimal(decision.entry_text or "0")
        stop = Decimal(decision.stop_text or "0")
        direction = Decimal(1) if decision.direction == "LONG" else Decimal(-1)
        executed_exit = base_exit * (Decimal(1) - direction * self._slippage_rate)
        initial_risk = abs(requested_entry - stop)
        gross_pnl = direction * (base_exit - requested_entry)
        entry_fee = opened.executed_entry * self._fee_rate
        exit_fee = executed_exit * self._fee_rate
        net_pnl = direction * (executed_exit - opened.executed_entry) - entry_fee - exit_fee
        slippage_cost = direction * (opened.executed_entry - requested_entry)
        slippage_cost += direction * (base_exit - executed_exit)
        return TradeRecord(
            opened.evaluation.decision_hash,
            decision.instrument_id,
            decision.direction or "",
            "CLOSED",
            opened.evaluation.evaluation_time_ms,
            opened.entry_time_ms,
            candle.open_time_ms,
            str(DecimalText(decision.entry_text or "0")),
            str(DecimalText(decision.stop_text or "0")),
            str(DecimalText(decision.target_text or "0")),
            _decimal_text(opened.executed_entry),
            _decimal_text(requested_exit),
            _decimal_text(executed_exit),
            reason,
            _decimal_text(initial_risk),
            _decimal_text(gross_pnl / requested_entry),
            _decimal_text(entry_fee),
            _decimal_text(exit_fee),
            _decimal_text(slippage_cost),
            _decimal_text(net_pnl / requested_entry),
            _decimal_text(gross_pnl / initial_risk),
            _decimal_text(net_pnl / initial_risk),
            duration_minutes=ExecutionSimulator._duration_minutes(opened, candle),
        )


def _decimal_text(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in {"", "-0"} else text
