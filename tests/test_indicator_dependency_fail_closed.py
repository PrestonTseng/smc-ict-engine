from __future__ import annotations

from collections.abc import Mapping

import pytest

from smc_ict.application.graph import RunContext
from smc_ict.application.resampling import DerivedCandle
from smc_ict.domain import Observation


def candle(interval: str) -> DerivedCandle:
    duration = {"1h": 3_600_000, "5m": 300_000}[interval]
    return DerivedCandle(
        instrument_id="BTC-USDT-PERP",
        interval=interval,
        open_time_ms=0,
        close_time_ms=duration - 1,
        open="100",
        high="101",
        low="99",
        close="100",
        base_volume="1",
        quote_volume="1",
    )


def dependency(signal_id: str, *, status: str) -> Observation:
    timeframe = (
        "4h"
        if signal_id == "smc.swing_structure"
        else "1h"
        if signal_id.startswith("smc.")
        else "5m"
    )
    return Observation.available(
        signal_id=signal_id,
        instrument_id="BTC-USDT-PERP",
        timeframe=timeframe,
        status=status,
        event_type=None,
        direction=None,
        event_time_ms=0,
        known_time_ms=0,
        state="REJECTED" if status == "FAIL" else "CONFIRMED",
        dependency_ids=(),
        parameter_hash="a" * 64,
        source_manifest_ids=("fixture",),
        payload_schema_version=1,
        bounded_reason="fixture prerequisite failed" if status == "FAIL" else "fixture passed",
        payload={},
    )


def context(role: str, timeframe: str) -> RunContext:
    series = (candle(timeframe),)
    return RunContext("BTC-USDT-PERP", series[-1].close_time_ms, {role: series})


@pytest.mark.parametrize(
    ("factory", "parameters", "role", "timeframe", "dependency_ids"),
    [
        pytest.param(
            "smc.equal_high_low",
            {"confirmation_bars": 3, "threshold_atr_fraction": "0.1"},
            "context",
            "1h",
            ("smc.swing_structure",),
            id="equal-high-low",
        ),
        pytest.param(
            "smc.order_block",
            {
                "scope": "swing",
                "volatility_filter": "atr",
                "mitigation_source": "close",
                "maximum_blocks": 5,
            },
            "context",
            "1h",
            ("smc.swing_structure",),
            id="order-block",
        ),
        pytest.param(
            "ict.clustered_liquidity",
            {"pivot_width": 5, "minimum_pivots": 3, "margin_atr_fraction": "0.4"},
            "execution",
            "5m",
            ("smc.equal_high_low",),
            id="clustered-liquidity",
        ),
        pytest.param(
            "ict.market_structure",
            {"pivot_width": 5, "emit_mss": True, "emit_bos": True},
            "execution",
            "5m",
            ("ict.clustered_liquidity",),
            id="market-structure",
        ),
        pytest.param(
            "ict.fair_value_gap",
            {
                "kind": "ordinary",
                "require_displacement": True,
                "displacement_length": 20,
                "mitigation": "full_traversal",
            },
            "execution",
            "5m",
            ("ict.market_structure",),
            id="fair-value-gap",
        ),
        pytest.param(
            "project.risk_levels",
            {"minimum_reward_risk": "2"},
            "execution",
            "5m",
            ("ict.clustered_liquidity", "ict.fair_value_gap"),
            id="risk-levels",
        ),
    ],
)
def test_every_dependent_plugin_propagates_evaluable_failed_prerequisite(
    factory: str,
    parameters: Mapping[str, object],
    role: str,
    timeframe: str,
    dependency_ids: tuple[str, ...],
) -> None:
    from smc_ict.composition.registries import indicator_composition_root

    dependencies = {
        dependency_id: dependency(dependency_id, status="FAIL" if index == 0 else "PASS")
        for index, dependency_id in enumerate(dependency_ids)
    }

    result = (
        indicator_composition_root()
        .plugins.resolve(factory)(parameters)
        .evaluate(context(role, timeframe), dependencies)
    )

    assert result.status == "FAIL"
    assert result.state == "FAILED_DEPENDENCY"
    assert result.dependency_ids == dependency_ids
    assert result.event_time_ms == 0
    assert result.known_time_ms == 0
    assert result.bounded_reason == f"dependency {dependency_ids[0]} failed"
    assert result.payload == {
        "failed_dependency_id": dependency_ids[0],
        "dependency_reason": "fixture prerequisite failed",
    }
