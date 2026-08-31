"""Direct closed-bar translations of the active SMC registrations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from smc_ict.application.graph import RunContext
from smc_ict.domain import Observation

from .base import Candle, closed_candles, decimal_text, dependency_problem, observation, wilder_atr

_SMC_SOURCE = ("smc_luxalgo@7",)


@dataclass(frozen=True, slots=True)
class _StructureEvent:
    event_type: str
    direction: str
    level: Decimal
    event_index: int
    known_index: int
    state: str
    pivot_kind: str


@dataclass(slots=True)
class _Pivot:
    level: Decimal
    index: int
    crossed: bool = False


def _exact_int(parameters: Mapping[str, object], name: str, minimum: int) -> int:
    value = parameters.get(name)
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer; Boolean is not integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


def _exact_bool(parameters: Mapping[str, object], name: str) -> bool:
    value = parameters.get(name)
    if type(value) is not bool:
        raise TypeError(f"{name} must be Boolean")
    return value


def _swing_events(candles: tuple[Candle, ...], size: int) -> tuple[_StructureEvent, ...]:
    """Translate SMC source lines 337-361, 409-457 and 551-612."""

    leg = 0
    trend = 0
    high_pivot: _Pivot | None = None
    low_pivot: _Pivot | None = None
    previous_high: Decimal | None = None
    previous_low: Decimal | None = None
    events: list[_StructureEvent] = []

    for index, candle in enumerate(candles):
        if index >= size:
            candidate_index = index - size
            candidate_high = Decimal(candles[candidate_index].high)
            candidate_low = Decimal(candles[candidate_index].low)
            subsequent = candles[candidate_index + 1 : index + 1]
            new_high = candidate_high > max(Decimal(item.high) for item in subsequent)
            new_low = candidate_low < min(Decimal(item.low) for item in subsequent)
            next_leg = 0 if new_high else 1 if new_low else leg
            if next_leg != leg:
                if next_leg == 1:
                    classification = (
                        "LL" if previous_low is not None and candidate_low < previous_low else "HL"
                    )
                    low_pivot = _Pivot(candidate_low, candidate_index)
                    previous_low = candidate_low
                    events.append(
                        _StructureEvent(
                            classification,
                            "BULLISH",
                            candidate_low,
                            candidate_index,
                            index,
                            "CONFIRMED_PIVOT",
                            "LOW",
                        )
                    )
                else:
                    classification = (
                        "HH"
                        if previous_high is not None and candidate_high > previous_high
                        else "LH"
                    )
                    high_pivot = _Pivot(candidate_high, candidate_index)
                    previous_high = candidate_high
                    events.append(
                        _StructureEvent(
                            classification,
                            "BEARISH",
                            candidate_high,
                            candidate_index,
                            index,
                            "CONFIRMED_PIVOT",
                            "HIGH",
                        )
                    )
                leg = next_leg

        close = Decimal(candle.close)
        previous_close = Decimal(candles[index - 1].close) if index else close
        if (
            high_pivot is not None
            and not high_pivot.crossed
            and previous_close <= high_pivot.level < close
        ):
            tag = "CHoCH" if trend == -1 else "BOS"
            high_pivot.crossed = True
            trend = 1
            events.append(
                _StructureEvent(
                    tag,
                    "BULLISH",
                    high_pivot.level,
                    index,
                    index,
                    "BROKEN",
                    "HIGH",
                )
            )
        if (
            low_pivot is not None
            and not low_pivot.crossed
            and previous_close >= low_pivot.level > close
        ):
            tag = "CHoCH" if trend == 1 else "BOS"
            low_pivot.crossed = True
            trend = -1
            events.append(
                _StructureEvent(
                    tag,
                    "BEARISH",
                    low_pivot.level,
                    index,
                    index,
                    "BROKEN",
                    "LOW",
                )
            )
    return tuple(events)


class SwingStructurePlugin:
    plugin_id = "smc.swing_structure"
    role = "regime"
    timeframe = "4h"
    dependency_ids: tuple[str, ...] = ()

    def __init__(self, parameters: Mapping[str, object]) -> None:
        self._parameters = dict(parameters)
        self._size = _exact_int(parameters, "swing_length", 1)
        self._show_labels = _exact_bool(parameters, "show_labels")
        if set(parameters) != {"swing_length", "show_labels"}:
            raise ValueError("unexpected swing-structure parameters")

    def evaluate(self, context: RunContext, dependencies: Mapping[str, Observation]) -> Observation:
        if dependencies:
            raise ValueError("swing structure does not accept dependencies")
        candles = closed_candles(context, self.role, self.timeframe)
        if len(candles) <= self._size:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_SMC_SOURCE,
                status="UNAVAILABLE",
                reason="insufficient bars for first size-confirmed swing pivot",
            )
        events = _swing_events(candles, self._size)
        if not events:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_SMC_SOURCE,
                status="FAIL",
                reason="no confirmed swing pivot or un-crossed pivot close-crossing",
                state="NO_EVENT",
                payload={"confirmation_bars": self._size},
            )
        event = events[-1]
        return observation(
            plugin_id=self.plugin_id,
            role=self.role,
            timeframe=self.timeframe,
            parameters=self._parameters,
            context=context,
            dependency_ids=self.dependency_ids,
            source_manifest_ids=_SMC_SOURCE,
            status="PASS",
            reason="latest source-aligned swing event is confirmed",
            event_type=event.event_type,
            direction=event.direction,
            event_time_ms=candles[event.event_index].close_time_ms,
            known_time_ms=candles[event.known_index].close_time_ms,
            state=event.state,
            level=event.level,
            payload={
                "confirmation_bars": self._size,
                "labels_enabled": self._show_labels,
                "pivot_kind": event.pivot_kind,
            },
        )


def _strictly_equal(
    previous: Decimal, current: Decimal, atr: Decimal, threshold_fraction: Decimal
) -> bool:
    """SMC lines 419/440 use a strict, not inclusive, ATR-scaled comparison."""

    return abs(previous - current) < threshold_fraction * atr


@dataclass(frozen=True, slots=True)
class _EqualEvent:
    event_type: str
    direction: str
    level: Decimal
    previous_level: Decimal
    event_index: int
    known_index: int


def _equal_events(
    candles: tuple[Candle, ...], confirmation: int, threshold: Decimal
) -> tuple[_EqualEvent, ...]:
    atr_values = wilder_atr(candles, 200)
    leg = 0
    previous_high: Decimal | None = None
    previous_low: Decimal | None = None
    events: list[_EqualEvent] = []
    for index in range(confirmation, len(candles)):
        candidate_index = index - confirmation
        candidate_high = Decimal(candles[candidate_index].high)
        candidate_low = Decimal(candles[candidate_index].low)
        subsequent = candles[candidate_index + 1 : index + 1]
        new_high = candidate_high > max(Decimal(item.high) for item in subsequent)
        new_low = candidate_low < min(Decimal(item.low) for item in subsequent)
        next_leg = 0 if new_high else 1 if new_low else leg
        if next_leg == leg:
            continue
        atr = atr_values[index]
        if next_leg == 1:
            if (
                previous_low is not None
                and atr is not None
                and _strictly_equal(previous_low, candidate_low, atr, threshold)
            ):
                events.append(
                    _EqualEvent(
                        "EQL",
                        "BULLISH",
                        candidate_low,
                        previous_low,
                        candidate_index,
                        index,
                    )
                )
            previous_low = candidate_low
        else:
            if (
                previous_high is not None
                and atr is not None
                and _strictly_equal(previous_high, candidate_high, atr, threshold)
            ):
                events.append(
                    _EqualEvent(
                        "EQH",
                        "BEARISH",
                        candidate_high,
                        previous_high,
                        candidate_index,
                        index,
                    )
                )
            previous_high = candidate_high
        leg = next_leg
    return tuple(events)


class EqualHighLowPlugin:
    plugin_id = "smc.equal_high_low"
    role = "context"
    timeframe = "1h"
    dependency_ids = ("smc.swing_structure",)

    def __init__(self, parameters: Mapping[str, object]) -> None:
        self._parameters = dict(parameters)
        self._confirmation = _exact_int(parameters, "confirmation_bars", 1)
        threshold = parameters.get("threshold_atr_fraction")
        if type(threshold) is not str:
            raise TypeError("threshold_atr_fraction must be canonical decimal text")
        self._threshold = Decimal(threshold)
        if not Decimal(0) <= self._threshold <= Decimal("0.5"):
            raise ValueError("threshold_atr_fraction must be in 0..0.5")
        if set(parameters) != {"confirmation_bars", "threshold_atr_fraction"}:
            raise ValueError("unexpected equal-high-low parameters")

    def evaluate(self, context: RunContext, dependencies: Mapping[str, Observation]) -> Observation:
        problem = dependency_problem(dependencies, self.dependency_ids, context)
        candles = closed_candles(context, self.role, self.timeframe)
        if problem is not None:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_SMC_SOURCE,
                status=problem.status,
                reason=problem.reason,
                state=problem.state,
                event_time_ms=problem.event_time_ms,
                known_time_ms=problem.known_time_ms,
                payload=problem.payload,
            )
        if len(candles) < max(200, self._confirmation + 1):
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_SMC_SOURCE,
                status="UNAVAILABLE",
                reason="insufficient bars for ATR(200) and pivot confirmation",
            )
        events = _equal_events(candles, self._confirmation, self._threshold)
        if not events:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_SMC_SOURCE,
                status="FAIL",
                reason="no latest confirmed pivot pair is strictly within the ATR threshold",
                state="NO_EQUAL_LEVEL",
                payload={"confirmation_bars": self._confirmation},
            )
        event = events[-1]
        return observation(
            plugin_id=self.plugin_id,
            role=self.role,
            timeframe=self.timeframe,
            parameters=self._parameters,
            context=context,
            dependency_ids=self.dependency_ids,
            source_manifest_ids=_SMC_SOURCE,
            status="PASS",
            reason="confirmed equal high or low satisfies the strict source ATR threshold",
            event_type=event.event_type,
            direction=event.direction,
            event_time_ms=candles[event.event_index].close_time_ms,
            known_time_ms=candles[event.known_index].close_time_ms,
            state="CONFIRMED_EQUAL_LEVEL",
            level=event.level,
            payload={
                "confirmation_bars": self._confirmation,
                "previous_level_text": decimal_text(event.previous_level),
                "threshold_atr_fraction": decimal_text(self._threshold),
            },
        )


@dataclass(slots=True)
class _OrderBlock:
    upper: Decimal
    lower: Decimal
    direction: str
    break_index: int
    source_index: int


def _parsed_extremes(
    candles: tuple[Candle, ...], filter_name: str
) -> tuple[tuple[Decimal, Decimal], ...]:
    atr_values = wilder_atr(candles, 200)
    true_range_values: list[Decimal] = []
    result: list[tuple[Decimal, Decimal]] = []
    previous_close: Decimal | None = None
    for index, candle in enumerate(candles):
        high = Decimal(candle.high)
        low = Decimal(candle.low)
        true_range = high - low
        if previous_close is not None:
            true_range = max(true_range, abs(high - previous_close), abs(low - previous_close))
        true_range_values.append(true_range)
        if filter_name == "atr":
            measure = atr_values[index]
        else:
            measure = None if index == 0 else sum(true_range_values, Decimal(0)) / Decimal(index)
        volatile = measure is not None and high - low >= Decimal(2) * measure
        result.append((low, high) if volatile else (high, low))
        previous_close = Decimal(candle.close)
    return tuple(result)


def _active_order_blocks(
    candles: tuple[Candle, ...],
    *,
    size: int,
    filter_name: str,
    mitigation_source: str,
    maximum_blocks: int,
) -> tuple[_OrderBlock, ...]:
    """Translate SMC source lines 310-323, 478-525 and 551-612."""

    parsed = _parsed_extremes(candles, filter_name)
    leg = 0
    high_pivot: _Pivot | None = None
    low_pivot: _Pivot | None = None
    active: list[_OrderBlock] = []
    for index, candle in enumerate(candles):
        if index >= size:
            candidate_index = index - size
            candidate_high = Decimal(candles[candidate_index].high)
            candidate_low = Decimal(candles[candidate_index].low)
            subsequent = candles[candidate_index + 1 : index + 1]
            new_high = candidate_high > max(Decimal(item.high) for item in subsequent)
            new_low = candidate_low < min(Decimal(item.low) for item in subsequent)
            next_leg = 0 if new_high else 1 if new_low else leg
            if next_leg != leg:
                if next_leg == 1:
                    low_pivot = _Pivot(candidate_low, candidate_index)
                else:
                    high_pivot = _Pivot(candidate_high, candidate_index)
                leg = next_leg

        close = Decimal(candle.close)
        previous_close = Decimal(candles[index - 1].close) if index else close
        if (
            high_pivot is not None
            and not high_pivot.crossed
            and previous_close <= high_pivot.level < close
        ):
            high_pivot.crossed = True
            candidates = parsed[high_pivot.index : index]
            if candidates:
                source_offset = min(range(len(candidates)), key=lambda item: candidates[item][1])
                source_index = high_pivot.index + source_offset
                upper, lower = parsed[source_index]
                active.insert(0, _OrderBlock(upper, lower, "BULLISH", index, source_index))
        if (
            low_pivot is not None
            and not low_pivot.crossed
            and previous_close >= low_pivot.level > close
        ):
            low_pivot.crossed = True
            candidates = parsed[low_pivot.index : index]
            if candidates:
                source_offset = max(range(len(candidates)), key=lambda item: candidates[item][0])
                source_index = low_pivot.index + source_offset
                upper, lower = parsed[source_index]
                active.insert(0, _OrderBlock(upper, lower, "BEARISH", index, source_index))

        bearish_source = close if mitigation_source == "close" else Decimal(candle.high)
        bullish_source = close if mitigation_source == "close" else Decimal(candle.low)
        active = [
            block
            for block in active
            if not (
                (block.direction == "BEARISH" and bearish_source > block.upper)
                or (block.direction == "BULLISH" and bullish_source < block.lower)
            )
        ][:maximum_blocks]
    return tuple(active)


class OrderBlockPlugin:
    plugin_id = "smc.order_block"
    role = "context"
    timeframe = "1h"
    dependency_ids = ("smc.swing_structure",)

    def __init__(self, parameters: Mapping[str, object]) -> None:
        self._parameters = dict(parameters)
        expected = {"scope", "volatility_filter", "mitigation_source", "maximum_blocks"}
        if set(parameters) != expected:
            raise ValueError("unexpected order-block parameters")
        scope = parameters["scope"]
        filter_name = parameters["volatility_filter"]
        mitigation = parameters["mitigation_source"]
        if type(scope) is not str or scope not in {"swing", "internal"}:
            raise ValueError("scope must be swing or internal")
        if type(filter_name) is not str or filter_name not in {"atr", "cumulative_range"}:
            raise ValueError("unknown volatility filter")
        if type(mitigation) is not str or mitigation not in {"close", "high_low"}:
            raise ValueError("unknown mitigation source")
        self._scope = scope
        self._filter = filter_name
        self._mitigation = mitigation
        self._maximum = _exact_int(parameters, "maximum_blocks", 1)

    def evaluate(self, context: RunContext, dependencies: Mapping[str, Observation]) -> Observation:
        problem = dependency_problem(dependencies, self.dependency_ids, context)
        candles = closed_candles(context, self.role, self.timeframe)
        size = 50 if self._scope == "swing" else 5
        if problem is not None:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_SMC_SOURCE,
                status=problem.status,
                reason=problem.reason,
                state=problem.state,
                event_time_ms=problem.event_time_ms,
                known_time_ms=problem.known_time_ms,
                payload=problem.payload,
            )
        if len(candles) < max(200, size + 1):
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_SMC_SOURCE,
                status="UNAVAILABLE",
                reason="insufficient bars for source volatility and structure warmup",
            )
        blocks = _active_order_blocks(
            candles,
            size=size,
            filter_name=self._filter,
            mitigation_source=self._mitigation,
            maximum_blocks=self._maximum,
        )
        if not blocks:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_SMC_SOURCE,
                status="FAIL",
                reason="no unmitigated source-selected order block is active",
                state="NO_ACTIVE_BLOCK",
                payload={"active_block_count": 0},
            )
        block = blocks[0]
        return observation(
            plugin_id=self.plugin_id,
            role=self.role,
            timeframe=self.timeframe,
            parameters=self._parameters,
            context=context,
            dependency_ids=self.dependency_ids,
            source_manifest_ids=_SMC_SOURCE,
            status="PASS",
            reason="latest bounded source-selected order block remains unmitigated",
            event_type="ORDER_BLOCK",
            direction=block.direction,
            event_time_ms=candles[block.break_index].close_time_ms,
            known_time_ms=candles[block.break_index].close_time_ms,
            state="ACTIVE",
            lower=block.lower,
            upper=block.upper,
            payload={
                "active_block_count": len(blocks),
                "source_candle_time_ms": candles[block.source_index].close_time_ms,
                "scope": self._scope,
            },
        )
