"""Direct closed-bar translations of the active ICT registrations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from smc_ict.application.graph import RunContext
from smc_ict.domain import Observation

from .base import (
    Candle,
    closed_candles,
    decimal_text,
    dependency_problem,
    observation,
    simple_moving_average,
    wilder_atr,
)

_ICT_SOURCE = ("ict_luxalgo@1",)


def _exact_int(
    parameters: Mapping[str, object], name: str, minimum: int, maximum: int | None = None
) -> int:
    value = parameters.get(name)
    if type(value) is not int:
        raise TypeError(f"{name} must be an integer; Boolean is not integer")
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    if maximum is not None and value > maximum:
        raise ValueError(f"{name} must be at most {maximum}")
    return value


def _exact_bool(parameters: Mapping[str, object], name: str) -> bool:
    value = parameters.get(name)
    if type(value) is not bool:
        raise TypeError(f"{name} must be Boolean")
    return value


@dataclass(slots=True)
class _ZigzagPoint:
    direction: int
    index: int
    level: Decimal


def _pivot_candidates(candles: tuple[Candle, ...], left: int) -> tuple[tuple[int, int], ...]:
    """Return (confirmation index, pivot direction) using one-right-bar confirmation."""

    result: list[tuple[int, int]] = []
    for index in range(left + 1, len(candles)):
        candidate = index - 1
        high = Decimal(candles[candidate].high)
        low = Decimal(candles[candidate].low)
        prior = candles[candidate - left : candidate]
        if high > max(Decimal(item.high) for item in prior) and high > Decimal(candles[index].high):
            result.append((index, 1))
        if low < min(Decimal(item.low) for item in prior) and low < Decimal(candles[index].low):
            result.append((index, -1))
    return tuple(result)


def _update_zigzag(points: list[_ZigzagPoint], point: _ZigzagPoint) -> bool:
    if not points or points[0].direction != point.direction:
        points.insert(0, point)
        return True
    current = points[0]
    if (point.direction == 1 and point.level > current.level) or (
        point.direction == -1 and point.level < current.level
    ):
        points[0] = point
        return True
    return False


@dataclass(slots=True)
class _LiquidityZone:
    side: str
    lower: Decimal
    upper: Decimal
    level: Decimal
    event_index: int
    known_index: int
    anchor_index: int
    state: str = "ACTIVE"
    opposing_level: Decimal | None = None
    swept_extreme: Decimal | None = None


def _matching_liquidity_points(
    points: list[_ZigzagPoint], direction: int, level: Decimal, margin: Decimal
) -> tuple[_ZigzagPoint, ...]:
    """Apply the source's newest-to-oldest scan and asymmetric early break."""

    matching: list[_ZigzagPoint] = []
    for point in points[:50]:
        if point.direction != direction:
            continue
        if (direction == 1 and point.level > level + margin) or (
            direction == -1 and point.level < level - margin
        ):
            break
        if level - margin < point.level < level + margin:
            matching.append(point)
    return tuple(matching)


def _liquidity_zones(
    candles: tuple[Candle, ...], left: int, minimum: int, fraction: Decimal
) -> tuple[_LiquidityZone, ...]:
    """Translate ICT source lines 344-463 and 821-856 without visual objects."""

    atr_values = wilder_atr(candles, 10)
    candidate_map: dict[int, list[int]] = {}
    for known_index, direction in _pivot_candidates(candles, left):
        candidate_map.setdefault(known_index, []).append(direction)
    points: list[_ZigzagPoint] = []
    zones: list[_LiquidityZone] = []
    seen_clusters: set[tuple[str, tuple[int, ...]]] = set()
    for index, candle in enumerate(candles):
        for direction in candidate_map.get(index, []):
            pivot_index = index - 1
            level = Decimal(
                candles[pivot_index].high if direction == 1 else candles[pivot_index].low
            )
            if not _update_zigzag(points, _ZigzagPoint(direction, pivot_index, level)):
                continue
            atr = atr_values[index]
            if atr is None:
                continue
            margin = atr * fraction
            matching = _matching_liquidity_points(points, direction, level, margin)
            if len(matching) < minimum:
                continue
            side = "BUYSIDE" if direction == 1 else "SELLSIDE"
            anchor_index = matching[-1].index
            cluster_key = (
                side,
                tuple(point.index for point in matching),
            )
            if cluster_key in seen_clusters:
                continue
            seen_clusters.add(cluster_key)
            minimum_level = min(point.level for point in matching)
            maximum_level = max(point.level for point in matching)
            center = (minimum_level + maximum_level) / Decimal(2)
            latest_same_side = next((zone for zone in zones if zone.side == side), None)
            if (
                latest_same_side is not None
                and latest_same_side.state != "FULL"
                and latest_same_side.anchor_index == anchor_index
            ):
                latest_same_side.lower = center - margin
                latest_same_side.upper = center + margin
                latest_same_side.event_index = pivot_index
                latest_same_side.known_index = index
                continue
            zones.insert(
                0,
                _LiquidityZone(
                    cluster_key[0],
                    center - margin,
                    center + margin,
                    matching[-1].level,
                    pivot_index,
                    index,
                    anchor_index,
                ),
            )

        close = Decimal(candle.close)
        for zone in zones:
            if zone.state == "FULL":
                continue
            if zone.side == "BUYSIDE":
                partial = close > zone.lower
                full = close > zone.upper
            else:
                partial = close < zone.upper
                full = close < zone.lower
            if full:
                zone.state = "FULL"
                zone.known_index = index
                zone.swept_extreme = Decimal(candle.high if zone.side == "BUYSIDE" else candle.low)
                opposing = next((item for item in zones if item.side != zone.side), None)
                zone.opposing_level = None if opposing is None else opposing.level
            elif partial and zone.state == "ACTIVE":
                zone.state = "PARTIAL"
                zone.known_index = index
    return tuple(sorted(zones, key=lambda zone: (zone.known_index, zone.event_index), reverse=True))


class ClusteredLiquidityPlugin:
    plugin_id = "ict.clustered_liquidity"
    role = "execution"
    timeframe = "5m"
    dependency_ids = ("smc.equal_high_low",)

    def __init__(self, parameters: Mapping[str, object]) -> None:
        self._parameters = dict(parameters)
        if set(parameters) != {"pivot_width", "minimum_pivots", "margin_atr_fraction"}:
            raise ValueError("unexpected clustered-liquidity parameters")
        self._left = _exact_int(parameters, "pivot_width", 3, 10)
        self._minimum = _exact_int(parameters, "minimum_pivots", 3)
        fraction = parameters.get("margin_atr_fraction")
        if type(fraction) is not str:
            raise TypeError("margin_atr_fraction must be canonical decimal text")
        self._fraction = Decimal(fraction)
        if self._fraction not in {Decimal(value) / Decimal(10) for value in range(2, 8)}:
            raise ValueError("margin_atr_fraction must be a source-representable tenth in 0.2..0.7")

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
                source_manifest_ids=_ICT_SOURCE,
                status=problem.status,
                reason=problem.reason,
                state=problem.state,
                event_time_ms=problem.event_time_ms,
                known_time_ms=problem.known_time_ms,
                payload=problem.payload,
            )
        if len(candles) < max(10, self._left + 2):
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_ICT_SOURCE,
                status="UNAVAILABLE",
                reason="insufficient bars for ATR(10) and one-right-bar pivots",
            )
        zones = _liquidity_zones(candles, self._left, self._minimum, self._fraction)
        if not zones:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_ICT_SOURCE,
                status="FAIL",
                reason="fewer than the configured same-side pivots are strictly clustered",
                state="NO_CLUSTER",
                payload={"cluster_count": 0},
            )
        zone = zones[0]
        opposing = next((item for item in zones if item.side != zone.side), None)
        direction = "SHORT" if zone.side == "BUYSIDE" else "LONG"
        payload: dict[str, object] = {
            "cluster_count": len(zones),
            "liquidity_side": zone.side,
        }
        if zone.swept_extreme is not None:
            payload["swept_extreme_text"] = decimal_text(zone.swept_extreme)
        opposing_level = zone.opposing_level
        if opposing_level is None and opposing is not None and zone.state != "FULL":
            opposing_level = opposing.level
        if opposing_level is not None:
            payload["opposing_level_text"] = decimal_text(opposing_level)
        return observation(
            plugin_id=self.plugin_id,
            role=self.role,
            timeframe=self.timeframe,
            parameters=self._parameters,
            context=context,
            dependency_ids=self.dependency_ids,
            source_manifest_ids=_ICT_SOURCE,
            status="PASS",
            reason="latest same-side pivot cluster has deterministic traversal state",
            event_type="CLUSTERED_LIQUIDITY",
            direction=direction,
            event_time_ms=candles[zone.event_index].close_time_ms,
            known_time_ms=candles[zone.known_index].close_time_ms,
            state=zone.state,
            level=zone.level,
            lower=zone.lower,
            upper=zone.upper,
            payload=payload,
        )


@dataclass(frozen=True, slots=True)
class _MarketEvent:
    event_type: str
    direction: str
    level: Decimal
    index: int


def _selected_structure_points(
    points: list[_ZigzagPoint],
) -> tuple[_ZigzagPoint | None, _ZigzagPoint | None]:
    """Translate ICT source lines 468-469, which deliberately exclude point zero."""

    if len(points) < 3:
        return None, None
    high_index = 2 if points[2].direction == 1 else 1
    low_index = 2 if points[2].direction == -1 else 1
    high = points[high_index]
    low = points[low_index]
    return (high if high.direction == 1 else None, low if low.direction == -1 else None)


def _market_events(candles: tuple[Candle, ...], left: int) -> tuple[_MarketEvent, ...]:
    """Translate ICT source lines 344-364 and 465-517 with closed-bar crossings."""

    candidate_map: dict[int, list[int]] = {}
    for known_index, direction in _pivot_candidates(candles, left):
        candidate_map.setdefault(known_index, []).append(direction)
    points: list[_ZigzagPoint] = []
    trend = 0
    latest_mss: dict[int, Decimal] = {}
    latest_bos: dict[int, Decimal] = {}
    events: list[_MarketEvent] = []
    for index, candle in enumerate(candles):
        for direction in candidate_map.get(index, []):
            pivot_index = index - 1
            level = Decimal(
                candles[pivot_index].high if direction == 1 else candles[pivot_index].low
            )
            _update_zigzag(points, _ZigzagPoint(direction, pivot_index, level))
        high, low = _selected_structure_points(points)
        close = Decimal(candle.close)
        if high is not None and close > high.level and trend < 1:
            trend = 1
            latest_mss[1] = high.level
            events.append(_MarketEvent("MSS", "BULLISH", high.level, index))
        elif low is not None and close < low.level and trend > -1:
            trend = -1
            latest_mss[-1] = low.level
            events.append(_MarketEvent("MSS", "BEARISH", low.level, index))
        elif (
            trend == 1
            and high is not None
            and close > high.level
            and high.level != latest_mss.get(1)
            and high.level != latest_bos.get(1)
        ):
            latest_bos[1] = high.level
            events.append(_MarketEvent("BOS", "BULLISH", high.level, index))
        elif (
            trend == -1
            and low is not None
            and close < low.level
            and low.level != latest_mss.get(-1)
            and low.level != latest_bos.get(-1)
        ):
            latest_bos[-1] = low.level
            events.append(_MarketEvent("BOS", "BEARISH", low.level, index))
    return tuple(events)


class MarketStructurePlugin:
    plugin_id = "ict.market_structure"
    role = "execution"
    timeframe = "5m"
    dependency_ids = ("ict.clustered_liquidity",)

    def __init__(self, parameters: Mapping[str, object]) -> None:
        self._parameters = dict(parameters)
        if set(parameters) != {"pivot_width", "emit_mss", "emit_bos"}:
            raise ValueError("unexpected market-structure parameters")
        self._left = _exact_int(parameters, "pivot_width", 3, 10)
        self._emit_mss = _exact_bool(parameters, "emit_mss")
        self._emit_bos = _exact_bool(parameters, "emit_bos")
        if not self._emit_mss and not self._emit_bos:
            raise ValueError("at least one structure event must be enabled")

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
                source_manifest_ids=_ICT_SOURCE,
                status=problem.status,
                reason=problem.reason,
                state=problem.state,
                event_time_ms=problem.event_time_ms,
                known_time_ms=problem.known_time_ms,
                payload=problem.payload,
            )
        if len(candles) < self._left + 2:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_ICT_SOURCE,
                status="UNAVAILABLE",
                reason="insufficient bars for one-right-bar zigzag confirmation",
            )
        events = tuple(
            event
            for event in _market_events(candles, self._left)
            if (event.event_type == "MSS" and self._emit_mss)
            or (event.event_type == "BOS" and self._emit_bos)
        )
        if not events:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_ICT_SOURCE,
                status="FAIL",
                reason="no enabled close-confirmed zigzag structure break",
                state="NO_BREAK",
            )
        event = events[-1]
        return observation(
            plugin_id=self.plugin_id,
            role=self.role,
            timeframe=self.timeframe,
            parameters=self._parameters,
            context=context,
            dependency_ids=self.dependency_ids,
            source_manifest_ids=_ICT_SOURCE,
            status="PASS",
            reason="latest unique zigzag level has an enabled closed-bar structure break",
            event_type=event.event_type,
            direction=event.direction,
            event_time_ms=candles[event.index].close_time_ms,
            known_time_ms=candles[event.index].close_time_ms,
            state="CONFIRMED_BREAK",
            level=event.level,
            payload={"pivot_confirmation_bars": 1},
        )


@dataclass(slots=True)
class _Gap:
    direction: str
    lower: Decimal
    upper: Decimal
    event_index: int
    known_index: int
    state: str = "ACTIVE"


def _active_gaps(
    candles: tuple[Candle, ...], displacement_length: int, require_displacement: bool
) -> tuple[_Gap, ...]:
    """Translate ICT source lines 547-566, 597-644 and 699-724 (ordinary FVG only)."""

    bodies = tuple(abs(Decimal(item.close) - Decimal(item.open)) for item in candles)
    means = simple_moving_average(bodies, displacement_length)
    gaps: list[_Gap] = []
    previous_bullish = False
    previous_bearish = False
    for index, candle in enumerate(candles):
        if index >= 2:
            middle = candles[index - 1]
            old = candles[index - 2]
            middle_body = bodies[index - 1]
            wick_limit = middle_body * Decimal("0.36")
            middle_high_wick = Decimal(middle.high) - max(
                Decimal(middle.close), Decimal(middle.open)
            )
            middle_low_wick = min(Decimal(middle.close), Decimal(middle.open)) - Decimal(middle.low)
            mean_body = means[index - 1]
            large_body = (
                mean_body is not None
                and middle_body > mean_body
                and middle_high_wick < wick_limit
                and middle_low_wick < wick_limit
            )
            bullish_displacement = large_body and Decimal(middle.close) > Decimal(middle.open)
            bearish_displacement = large_body and Decimal(middle.close) < Decimal(middle.open)
            bullish = Decimal(candle.low) > Decimal(old.high) and (
                bullish_displacement or not require_displacement
            )
            bearish = Decimal(candle.high) < Decimal(old.low) and (
                bearish_displacement or not require_displacement
            )
            if bullish:
                existing = next((gap for gap in gaps if gap.direction == "BULLISH"), None)
                if previous_bullish and existing is not None:
                    existing.lower = Decimal(old.high)
                    existing.upper = Decimal(candle.low)
                    existing.event_index = index
                    existing.known_index = index
                else:
                    gaps.insert(
                        0,
                        _Gap("BULLISH", Decimal(old.high), Decimal(candle.low), index, index),
                    )
            if bearish:
                existing = next((gap for gap in gaps if gap.direction == "BEARISH"), None)
                if previous_bearish and existing is not None:
                    existing.lower = Decimal(candle.high)
                    existing.upper = Decimal(old.low)
                    existing.event_index = index
                    existing.known_index = index
                else:
                    gaps.insert(
                        0,
                        _Gap("BEARISH", Decimal(candle.high), Decimal(old.low), index, index),
                    )
            previous_bullish = bullish
            previous_bearish = bearish

        low = Decimal(candle.low)
        high = Decimal(candle.high)
        for gap in gaps:
            if gap.state == "FULL":
                continue
            if gap.direction == "BULLISH":
                if low < gap.lower:
                    gap.state = "FULL"
                    gap.known_index = index
                elif low < gap.upper and gap.state == "ACTIVE":
                    gap.state = "PARTIAL"
                    gap.known_index = index
            else:
                if high > gap.upper:
                    gap.state = "FULL"
                    gap.known_index = index
                elif high > gap.lower and gap.state == "ACTIVE":
                    gap.state = "PARTIAL"
                    gap.known_index = index
    return tuple(
        sorted(
            (gap for gap in gaps if gap.state != "FULL"),
            key=lambda gap: (gap.known_index, gap.event_index),
            reverse=True,
        )
    )


class FairValueGapPlugin:
    plugin_id = "ict.fair_value_gap"
    role = "execution"
    timeframe = "5m"
    dependency_ids = ("ict.market_structure",)

    def __init__(self, parameters: Mapping[str, object]) -> None:
        self._parameters = dict(parameters)
        expected = {"kind", "require_displacement", "displacement_length", "mitigation"}
        if set(parameters) != expected:
            raise ValueError("unexpected fair-value-gap parameters")
        if parameters["kind"] != "ordinary" or parameters["mitigation"] != "full_traversal":
            raise ValueError("only ordinary full-traversal FVG is supported")
        self._require_displacement = _exact_bool(parameters, "require_displacement")
        self._length = _exact_int(parameters, "displacement_length", 1)

    def evaluate(self, context: RunContext, dependencies: Mapping[str, Observation]) -> Observation:
        problem = dependency_problem(dependencies, self.dependency_ids, context)
        candles = closed_candles(context, self.role, self.timeframe)
        warmup = self._length + 1 if self._require_displacement else 3
        if problem is not None:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_ICT_SOURCE,
                status=problem.status,
                reason=problem.reason,
                state=problem.state,
                event_time_ms=problem.event_time_ms,
                known_time_ms=problem.known_time_ms,
                payload=problem.payload,
            )
        if len(candles) < warmup:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_ICT_SOURCE,
                status="UNAVAILABLE",
                reason="insufficient bars for displacement and three-candle FVG",
            )
        gaps = _active_gaps(candles, self._length, self._require_displacement)
        if not gaps:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_ICT_SOURCE,
                status="FAIL",
                reason="no ordinary displacement FVG remains before full traversal",
                state="NO_ACTIVE_GAP",
                payload={"active_gap_count": 0},
            )
        gap = gaps[0]
        market_structure = dependencies["ict.market_structure"]
        if market_structure.direction != gap.direction or market_structure.known_time_ms is None:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_ICT_SOURCE,
                status="FAIL",
                reason="FVG direction contradicts market-structure dependency",
                state="CONTRADICTORY_EVIDENCE",
            )
        midpoint = (gap.lower + gap.upper) / Decimal(2)
        gap_known_time = candles[gap.known_index].close_time_ms
        known_time = max(gap_known_time, market_structure.known_time_ms)
        return observation(
            plugin_id=self.plugin_id,
            role=self.role,
            timeframe=self.timeframe,
            parameters=self._parameters,
            context=context,
            dependency_ids=self.dependency_ids,
            source_manifest_ids=_ICT_SOURCE,
            status="PASS",
            reason="latest ordinary displacement FVG remains before full traversal",
            event_type="FAIR_VALUE_GAP",
            direction=gap.direction,
            event_time_ms=candles[gap.event_index].close_time_ms,
            known_time_ms=known_time,
            state=gap.state,
            level=midpoint,
            lower=gap.lower,
            upper=gap.upper,
            payload={
                "active_gap_count": len(gaps),
                "midpoint_text": decimal_text(midpoint),
                "market_structure_direction": market_structure.direction,
                "market_structure_known_time_ms": market_structure.known_time_ms,
            },
        )
