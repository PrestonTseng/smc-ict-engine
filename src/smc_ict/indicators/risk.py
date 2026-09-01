"""Strategy-owned deterministic risk-level composition."""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation

from smc_ict.application.graph import RunContext
from smc_ict.domain import Observation

from .base import (
    closed_candles,
    configured_timeframe,
    decimal_text,
    dependency_problem,
    observation,
    wilder_atr,
)

_PROJECT_SOURCE = ("project_strategy@1",)


def _payload_decimal(observation_value: Observation, field: str) -> Decimal | None:
    value = observation_value.payload.get(field)
    if type(value) is not str:
        return None
    try:
        number = Decimal(value)
    except InvalidOperation:
        return None
    return number if number.is_finite() and number >= 0 else None


class RiskLevelsPlugin:
    plugin_id = "project.risk_levels"
    role = "execution"
    timeframe: str
    dependency_ids = ("ict.clustered_liquidity", "ict.fair_value_gap")

    def __init__(self, parameters: Mapping[str, object]) -> None:
        self._parameters = dict(parameters)
        if set(parameters) != {"minimum_reward_risk"}:
            raise ValueError("unexpected risk-level parameters")
        minimum = parameters.get("minimum_reward_risk")
        if type(minimum) is not str:
            raise TypeError("minimum_reward_risk must be canonical decimal text")
        self._minimum = Decimal(minimum)
        if self._minimum <= 0:
            raise ValueError("minimum_reward_risk must be positive")

    def evaluate(self, context: RunContext, dependencies: Mapping[str, Observation]) -> Observation:
        self.timeframe = configured_timeframe(context, self.role)
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
                source_manifest_ids=_PROJECT_SOURCE,
                status=problem.status,
                reason=problem.reason,
                state=problem.state,
                event_time_ms=problem.event_time_ms,
                known_time_ms=problem.known_time_ms,
                payload=problem.payload,
            )
        if len(candles) < 14:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_PROJECT_SOURCE,
                status="UNAVAILABLE",
                reason="insufficient execution bars for ATR(14)",
            )

        liquidity = dependencies["ict.clustered_liquidity"]
        gap = dependencies["ict.fair_value_gap"]
        contradiction = self._validated_inputs(liquidity, gap)
        if contradiction is not None:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_PROJECT_SOURCE,
                status="FAIL",
                reason=contradiction,
                state="CONTRADICTORY_EVIDENCE",
            )

        direction = liquidity.direction
        assert direction in {"LONG", "SHORT"}
        assert gap.level_text is not None
        entry = Decimal(gap.level_text)
        swept = _payload_decimal(liquidity, "swept_extreme_text")
        target = _payload_decimal(liquidity, "opposing_level_text")
        atr = wilder_atr(candles, 14)[-1]
        assert swept is not None and target is not None and atr is not None
        stop_offset = atr * Decimal("0.1")
        stop = swept - stop_offset if direction == "LONG" else swept + stop_offset
        risk = entry - stop if direction == "LONG" else stop - entry
        reward = target - entry if direction == "LONG" else entry - target
        if risk <= 0 or reward <= 0:
            return observation(
                plugin_id=self.plugin_id,
                role=self.role,
                timeframe=self.timeframe,
                parameters=self._parameters,
                context=context,
                dependency_ids=self.dependency_ids,
                source_manifest_ids=_PROJECT_SOURCE,
                status="FAIL",
                reason="composed stop or opposing target is not directional",
                state="INVALID_LEVEL_ORDER",
            )
        reward_risk = reward / risk
        status = "PASS" if reward_risk >= self._minimum else "FAIL"
        state = "LEVELS_CONFIRMED" if status == "PASS" else "BELOW_MINIMUM_REWARD_RISK"
        event_time = max(
            value for value in (liquidity.event_time_ms, gap.event_time_ms) if value is not None
        )
        known_time = max(
            value for value in (liquidity.known_time_ms, gap.known_time_ms) if value is not None
        )
        payload = {
            "direction": direction,
            "entry_text": decimal_text(entry),
            "stop_text": decimal_text(stop),
            "target_text": decimal_text(target),
            "reward_risk_text": decimal_text(reward_risk),
        }
        return observation(
            plugin_id=self.plugin_id,
            role=self.role,
            timeframe=self.timeframe,
            parameters=self._parameters,
            context=context,
            dependency_ids=self.dependency_ids,
            source_manifest_ids=_PROJECT_SOURCE,
            status=status,
            reason=(
                "exact reward/risk satisfies the configured minimum"
                if status == "PASS"
                else "exact reward/risk is below the configured minimum"
            ),
            event_type="RISK_LEVELS",
            direction=direction,
            event_time_ms=event_time,
            known_time_ms=known_time,
            state=state,
            level=entry,
            lower=min(stop, target),
            upper=max(stop, target),
            payload=payload,
        )

    @staticmethod
    def _validated_inputs(liquidity: Observation, gap: Observation) -> str | None:
        if liquidity.status != "PASS" or gap.status != "PASS":
            return "execution chain dependencies are not satisfied"
        if (
            liquidity.event_time_ms is None
            or liquidity.known_time_ms is None
            or gap.event_time_ms is None
            or gap.known_time_ms is None
        ):
            return "execution chain dependencies omit event or knowledge time"
        if liquidity.known_time_ms >= gap.known_time_ms:
            return "liquidity sweep is not known strictly before the FVG"
        if gap.dependency_ids != ("ict.market_structure",):
            return "FVG evidence does not retain its market-structure dependency"
        expected_gap_direction = {"LONG": "BULLISH", "SHORT": "BEARISH"}.get(
            liquidity.direction or ""
        )
        if expected_gap_direction is None or gap.direction != expected_gap_direction:
            return "liquidity and FVG directions contradict the execution chain"
        if gap.payload.get("market_structure_direction") != gap.direction:
            return "FVG evidence contradicts its retained market-structure direction"
        market_structure_known_time = gap.payload.get("market_structure_known_time_ms")
        if (
            type(market_structure_known_time) is not int
            or market_structure_known_time > gap.known_time_ms
        ):
            return "FVG evidence omits compatible market-structure knowledge time"
        if liquidity.state != "FULL":
            return "liquidity evidence does not contain a completed sweep"
        if gap.level_text is None or gap.lower_text is None or gap.upper_text is None:
            return "FVG evidence omits canonical levels"
        entry = Decimal(gap.level_text)
        if entry != (Decimal(gap.lower_text) + Decimal(gap.upper_text)) / Decimal(2):
            return "FVG entry is not its exact midpoint"
        swept = _payload_decimal(liquidity, "swept_extreme_text")
        target = _payload_decimal(liquidity, "opposing_level_text")
        if swept is None or target is None:
            return "liquidity evidence omits swept or frozen opposing level"
        return None
