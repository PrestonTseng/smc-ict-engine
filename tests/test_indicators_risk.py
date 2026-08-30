from __future__ import annotations

import pytest

from smc_ict.application.graph import RunContext
from smc_ict.application.resampling import DerivedCandle
from smc_ict.domain import Observation


def candles() -> tuple[DerivedCandle, ...]:
    duration = 300_000
    return tuple(
        DerivedCandle(
            instrument_id="BTC-USDT-PERP",
            interval="5m",
            open_time_ms=index * duration,
            close_time_ms=(index + 1) * duration - 1,
            open="100",
            high="101",
            low="99",
            close="100",
            base_volume="1",
            quote_volume="1",
        )
        for index in range(14)
    )


def observed(
    signal_id: str,
    *,
    direction: str,
    level: str,
    lower: str,
    upper: str,
    payload: dict[str, object],
    status: str = "PASS",
    event_time_ms: int = 1,
    known_time_ms: int | None = None,
    dependency_ids: tuple[str, ...] | None = None,
) -> Observation:
    if known_time_ms is None:
        known_time_ms = 2 if signal_id == "ict.clustered_liquidity" else 3
    if dependency_ids is None:
        dependency_ids = (
            ("smc.equal_high_low",)
            if signal_id == "ict.clustered_liquidity"
            else ("ict.market_structure",)
        )
    retained_payload = dict(payload)
    if signal_id == "ict.fair_value_gap" and status == "PASS":
        retained_payload.setdefault("market_structure_direction", direction)
        retained_payload.setdefault("market_structure_known_time_ms", 1)
    return Observation.available(
        signal_id=signal_id,
        instrument_id="BTC-USDT-PERP",
        timeframe="5m",
        status=status,
        event_type="FIXTURE" if status == "PASS" else None,
        direction=direction if status == "PASS" else None,
        event_time_ms=event_time_ms if status == "PASS" else None,
        known_time_ms=known_time_ms if status == "PASS" else None,
        state="FULL" if status == "PASS" else "UNAVAILABLE",
        dependency_ids=dependency_ids,
        parameter_hash="a" * 64,
        source_manifest_ids=("fixture",),
        payload_schema_version=1,
        bounded_reason="fixture",
        payload=retained_payload,
        level_text=level if status == "PASS" else None,
        lower_text=lower if status == "PASS" else None,
        upper_text=upper if status == "PASS" else None,
    )


def test_risk_levels_composes_exact_long_levels_and_accepts_reward_risk_boundary() -> None:
    from smc_ict.indicators.risk import RiskLevelsPlugin

    series = candles()
    context = RunContext("BTC-USDT-PERP", series[-1].close_time_ms, {"execution": series})
    liquidity = observed(
        "ict.clustered_liquidity",
        direction="LONG",
        level="99",
        lower="98.5",
        upper="99",
        payload={"swept_extreme_text": "99", "opposing_level_text": "102.4"},
    )
    fvg = observed(
        "ict.fair_value_gap",
        direction="BULLISH",
        level="100",
        lower="99.5",
        upper="100.5",
        payload={"midpoint_text": "100"},
    )

    observation = RiskLevelsPlugin({"minimum_reward_risk": "2"}).evaluate(
        context,
        {"ict.clustered_liquidity": liquidity, "ict.fair_value_gap": fvg},
    )

    assert (observation.status, observation.direction, observation.level_text) == (
        "PASS",
        "LONG",
        "100",
    )
    assert observation.payload == {
        "direction": "LONG",
        "entry_text": "100",
        "stop_text": "98.8",
        "target_text": "102.4",
        "reward_risk_text": "2",
    }


def test_risk_levels_rejects_contradictory_direction_and_unavailable_dependency() -> None:
    from dataclasses import replace

    from smc_ict.indicators.risk import RiskLevelsPlugin

    series = candles()
    context = RunContext("BTC-USDT-PERP", series[-1].close_time_ms, {"execution": series})
    liquidity = observed(
        "ict.clustered_liquidity",
        direction="SHORT",
        level="99",
        lower="98",
        upper="99",
        payload={"swept_extreme_text": "99", "opposing_level_text": "102"},
    )
    fvg = observed(
        "ict.fair_value_gap",
        direction="BULLISH",
        level="100",
        lower="99.5",
        upper="100.5",
        payload={"midpoint_text": "100"},
    )
    plugin = RiskLevelsPlugin({"minimum_reward_risk": "2"})

    contradictory = plugin.evaluate(
        context,
        {"ict.clustered_liquidity": liquidity, "ict.fair_value_gap": fvg},
    )
    assert (contradictory.status, contradictory.state) == ("FAIL", "CONTRADICTORY_EVIDENCE")

    failed_but_populated = plugin.evaluate(
        context,
        {
            "ict.clustered_liquidity": replace(liquidity, status="FAIL", direction="LONG"),
            "ict.fair_value_gap": fvg,
        },
    )
    assert (failed_but_populated.status, failed_but_populated.state) == (
        "FAIL",
        "FAILED_DEPENDENCY",
    )

    unavailable = observed(
        "ict.clustered_liquidity",
        direction="LONG",
        level="99",
        lower="98",
        upper="99",
        payload={},
        status="UNAVAILABLE",
    )
    missing = plugin.evaluate(
        context,
        {"ict.clustered_liquidity": unavailable, "ict.fair_value_gap": fvg},
    )
    assert missing.status == "UNAVAILABLE"


def test_risk_levels_composes_exact_short_levels_symmetrically() -> None:
    from smc_ict.indicators.risk import RiskLevelsPlugin

    series = candles()
    context = RunContext("BTC-USDT-PERP", series[-1].close_time_ms, {"execution": series})
    liquidity = observed(
        "ict.clustered_liquidity",
        direction="SHORT",
        level="101",
        lower="101",
        upper="101.5",
        payload={"swept_extreme_text": "101", "opposing_level_text": "97.6"},
    )
    fvg = observed(
        "ict.fair_value_gap",
        direction="BEARISH",
        level="100",
        lower="99.5",
        upper="100.5",
        payload={"midpoint_text": "100"},
    )

    result = RiskLevelsPlugin({"minimum_reward_risk": "2"}).evaluate(
        context,
        {"ict.clustered_liquidity": liquidity, "ict.fair_value_gap": fvg},
    )

    assert result.status == "PASS"
    assert result.payload == {
        "direction": "SHORT",
        "entry_text": "100",
        "stop_text": "101.2",
        "target_text": "97.6",
        "reward_risk_text": "2",
    }


@pytest.mark.parametrize(
    ("liquidity_known", "fvg_known", "expected_status"),
    [
        pytest.param(2, 3, "PASS", id="strictly-forward"),
        pytest.param(3, 2, "FAIL", id="reversed"),
        pytest.param(2, 2, "FAIL", id="equal-time"),
    ],
)
def test_risk_levels_requires_liquidity_sweep_known_strictly_before_fvg(
    liquidity_known: int, fvg_known: int, expected_status: str
) -> None:
    from smc_ict.indicators.risk import RiskLevelsPlugin

    series = candles()
    context = RunContext("BTC-USDT-PERP", series[-1].close_time_ms, {"execution": series})
    liquidity = observed(
        "ict.clustered_liquidity",
        direction="LONG",
        level="99",
        lower="98.5",
        upper="99",
        payload={"swept_extreme_text": "99", "opposing_level_text": "102.4"},
        known_time_ms=liquidity_known,
    )
    fvg = observed(
        "ict.fair_value_gap",
        direction="BULLISH",
        level="100",
        lower="99.5",
        upper="100.5",
        payload={"midpoint_text": "100"},
        known_time_ms=fvg_known,
    )

    result = RiskLevelsPlugin({"minimum_reward_risk": "2"}).evaluate(
        context,
        {"ict.clustered_liquidity": liquidity, "ict.fair_value_gap": fvg},
    )

    assert result.status == expected_status
    if expected_status == "FAIL":
        assert result.state == "CONTRADICTORY_EVIDENCE"
        assert result.bounded_reason == "liquidity sweep is not known strictly before the FVG"


@pytest.mark.parametrize("dependency_ids", [(), ("ict.clustered_liquidity",)])
def test_risk_levels_rejects_missing_or_contradictory_fvg_market_structure_chain(
    dependency_ids: tuple[str, ...],
) -> None:
    from smc_ict.indicators.risk import RiskLevelsPlugin

    series = candles()
    context = RunContext("BTC-USDT-PERP", series[-1].close_time_ms, {"execution": series})
    liquidity = observed(
        "ict.clustered_liquidity",
        direction="LONG",
        level="99",
        lower="98.5",
        upper="99",
        payload={"swept_extreme_text": "99", "opposing_level_text": "102.4"},
    )
    fvg = observed(
        "ict.fair_value_gap",
        direction="BULLISH",
        level="100",
        lower="99.5",
        upper="100.5",
        payload={"midpoint_text": "100"},
        dependency_ids=dependency_ids,
    )

    result = RiskLevelsPlugin({"minimum_reward_risk": "2"}).evaluate(
        context,
        {"ict.clustered_liquidity": liquidity, "ict.fair_value_gap": fvg},
    )

    assert (result.status, result.state) == ("FAIL", "CONTRADICTORY_EVIDENCE")
    assert result.bounded_reason == "FVG evidence does not retain its market-structure dependency"
