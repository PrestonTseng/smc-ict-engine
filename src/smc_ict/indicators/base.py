"""Shared closed-candle and observation mechanics for indicator plugins."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, cast

from smc_ict.application.graph import ConfiguredNode, RunContext
from smc_ict.domain import Observation


class Candle(Protocol):
    instrument_id: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: str
    high: str
    low: str
    close: str


def configured_timeframe(context: RunContext, role: str) -> str:
    """Resolve a role's timeframe from its configured candle series."""

    configured = context.timeframes_by_role.get(role)
    if configured is not None:
        return configured
    raw = context.candles_by_role.get(role)
    if raw is None:
        raise ValueError(f"missing candle role {role!r}")
    intervals = {cast(Candle, value).interval for value in raw}
    if len(intervals) != 1:
        raise ValueError("configured role must contain one non-empty timeframe")
    return intervals.pop()


def closed_candles(context: RunContext, role: str, timeframe: str) -> tuple[Candle, ...]:
    """Return one validated, ordered, completed candle series for a configured role."""

    raw = context.candles_by_role.get(role)
    if raw is None:
        raise ValueError(f"missing candle role {role!r}")
    candles = tuple(cast(Candle, value) for value in raw)
    previous_close: int | None = None
    for candle in candles:
        for field in ("open_time_ms", "close_time_ms"):
            if type(getattr(candle, field, None)) is not int:
                raise TypeError(f"candle {field} must be an integer; Boolean is not integer")
        if candle.instrument_id != context.instrument_id or candle.interval != timeframe:
            raise ValueError("candle identity does not match plugin role")
        if candle.close_time_ms > context.evaluation_time_ms:
            raise ValueError("developing or future candle is not allowed")
        if previous_close is not None and candle.open_time_ms != previous_close + 1:
            raise ValueError("indicator candles must be monotonic and contiguous")
        for field in ("open", "high", "low", "close"):
            value = getattr(candle, field, None)
            if type(value) is not str:
                raise TypeError(f"candle {field} must be canonical decimal text")
            Decimal(value)
        previous_close = candle.close_time_ms
    return candles


def decimal_text(value: Decimal) -> str:
    normalized = format(value, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def parameter_hash(plugin_id: str, role: str, parameters: Mapping[str, object]) -> str:
    return ConfiguredNode(plugin_id, plugin_id, role, (), parameters, 0).parameter_hash


@dataclass(frozen=True, slots=True)
class DependencyProblem:
    status: str
    state: str
    reason: str
    event_time_ms: int | None = None
    known_time_ms: int | None = None
    payload: Mapping[str, object] | None = None


_DEPENDENCY_TIMEFRAMES = {
    "smc.swing_structure": "4h",
    "smc.equal_high_low": "1h",
}

_EXECUTION_DEPENDENCIES = frozenset(
    {"ict.clustered_liquidity", "ict.market_structure", "ict.fair_value_gap"}
)


def dependency_problem(
    dependencies: Mapping[str, Observation],
    expected_ids: tuple[str, ...],
    context: RunContext,
) -> DependencyProblem | None:
    if tuple(dependencies) != expected_ids:
        raise ValueError("plugin dependencies do not match configured IDs")
    failed_dependency: str | None = None
    unavailable_dependency: str | None = None
    for dependency_id, observation in dependencies.items():
        if observation.signal_id != dependency_id:
            raise ValueError("dependency observation signal ID mismatch")
        if observation.instrument_id != context.instrument_id:
            raise ValueError("dependency observation instrument mismatch")
        expected_timeframe = (
            configured_timeframe(context, "execution")
            if dependency_id in _EXECUTION_DEPENDENCIES
            else _DEPENDENCY_TIMEFRAMES[dependency_id]
        )
        if observation.timeframe != expected_timeframe:
            raise ValueError("dependency observation timeframe mismatch")
        if (
            observation.known_time_ms is not None
            and observation.known_time_ms > context.evaluation_time_ms
        ):
            return DependencyProblem(
                "FAIL",
                "INVALID_DEPENDENCY_EVIDENCE",
                "dependency evidence is from the future",
            )
        if observation.status == "FAIL" and failed_dependency is None:
            failed_dependency = dependency_id
        elif observation.status == "UNAVAILABLE" and unavailable_dependency is None:
            unavailable_dependency = dependency_id
    if failed_dependency is not None:
        failed_observation = dependencies[failed_dependency]
        return DependencyProblem(
            "FAIL",
            "FAILED_DEPENDENCY",
            f"dependency {failed_dependency} failed",
            failed_observation.event_time_ms,
            failed_observation.known_time_ms,
            {
                "failed_dependency_id": failed_dependency,
                "dependency_reason": failed_observation.bounded_reason,
            },
        )
    if unavailable_dependency is not None:
        return DependencyProblem(
            "UNAVAILABLE",
            "UNAVAILABLE",
            f"dependency {unavailable_dependency} is unavailable",
        )
    return None


def observation(
    *,
    plugin_id: str,
    role: str,
    timeframe: str,
    parameters: Mapping[str, object],
    context: RunContext,
    dependency_ids: tuple[str, ...],
    source_manifest_ids: tuple[str, ...],
    status: str,
    reason: str,
    event_type: str | None = None,
    direction: str | None = None,
    event_time_ms: int | None = None,
    known_time_ms: int | None = None,
    state: str = "UNAVAILABLE",
    payload: Mapping[str, object] | None = None,
    level: Decimal | None = None,
    lower: Decimal | None = None,
    upper: Decimal | None = None,
) -> Observation:
    return Observation.available(
        signal_id=plugin_id,
        instrument_id=context.instrument_id,
        timeframe=timeframe,
        status=status,
        event_type=event_type,
        direction=direction,
        event_time_ms=event_time_ms,
        known_time_ms=known_time_ms,
        state=state,
        dependency_ids=dependency_ids,
        parameter_hash=parameter_hash(plugin_id, role, parameters),
        source_manifest_ids=source_manifest_ids,
        payload_schema_version=1,
        bounded_reason=reason,
        payload={} if payload is None else payload,
        level_text=None if level is None else decimal_text(level),
        lower_text=None if lower is None else decimal_text(lower),
        upper_text=None if upper is None else decimal_text(upper),
    )


def true_ranges(candles: Sequence[Candle]) -> tuple[Decimal, ...]:
    ranges: list[Decimal] = []
    previous_close: Decimal | None = None
    for candle in candles:
        high = Decimal(candle.high)
        low = Decimal(candle.low)
        value = high - low
        if previous_close is not None:
            value = max(value, abs(high - previous_close), abs(low - previous_close))
        ranges.append(value)
        previous_close = Decimal(candle.close)
    return tuple(ranges)


def wilder_atr(candles: Sequence[Candle], length: int) -> tuple[Decimal | None, ...]:
    ranges = true_ranges(candles)
    result: list[Decimal | None] = [None] * len(ranges)
    if len(ranges) < length:
        return tuple(result)
    current = sum(ranges[:length], Decimal(0)) / Decimal(length)
    result[length - 1] = current
    for index in range(length, len(ranges)):
        current = (current * Decimal(length - 1) + ranges[index]) / Decimal(length)
        result[index] = current
    return tuple(result)


def simple_moving_average(values: Sequence[Decimal], length: int) -> tuple[Decimal | None, ...]:
    result: list[Decimal | None] = [None] * len(values)
    running = Decimal(0)
    for index, value in enumerate(values):
        running += value
        if index >= length:
            running -= values[index - length]
        if index >= length - 1:
            result[index] = running / Decimal(length)
    return tuple(result)
