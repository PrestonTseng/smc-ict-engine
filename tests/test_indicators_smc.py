from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

import pytest

from smc_ict.application.graph import ConfiguredNode, RunContext
from smc_ict.application.resampling import DerivedCandle
from smc_ict.domain import Observation


def candles(
    values: Iterable[tuple[str, str, str, str]], *, interval: str = "4h"
) -> tuple[DerivedCandle, ...]:
    duration = {"5m": 300_000, "1h": 3_600_000, "4h": 14_400_000}[interval]
    return tuple(
        DerivedCandle(
            instrument_id="BTC-USDT-PERP",
            interval=interval,
            open_time_ms=index * duration,
            close_time_ms=(index + 1) * duration - 1,
            open=opening,
            high=high,
            low=low,
            close=close,
            base_volume="1",
            quote_volume="1",
        )
        for index, (opening, high, low, close) in enumerate(values)
    )


def context(role: str, series: tuple[DerivedCandle, ...]) -> RunContext:
    evaluation_time = series[-1].close_time_ms if series else 0
    return RunContext("BTC-USDT-PERP", evaluation_time, {role: series})


def dependency(signal_id: str, *, status: str = "PASS") -> Observation:
    return Observation.available(
        signal_id=signal_id,
        instrument_id="BTC-USDT-PERP",
        timeframe="4h",
        status=status,
        event_type="BOS" if status == "PASS" else None,
        direction="BULLISH" if status == "PASS" else None,
        event_time_ms=0 if status == "PASS" else None,
        known_time_ms=0 if status == "PASS" else None,
        state="CONFIRMED" if status == "PASS" else "UNAVAILABLE",
        dependency_ids=(),
        parameter_hash="a" * 64,
        source_manifest_ids=("fixture",),
        payload_schema_version=1,
        bounded_reason="fixture dependency",
        payload={},
    )


def test_swing_structure_is_unavailable_until_first_size_confirmed_pivot() -> None:
    from smc_ict.indicators.smc import SwingStructurePlugin

    parameters = {"swing_length": 10, "show_labels": True}
    series = candles([("10", "11", "9", "10")] * 10)

    observation = SwingStructurePlugin(parameters).evaluate(context("regime", series), {})

    assert observation.status == "UNAVAILABLE"
    assert observation.event_time_ms is None
    assert (
        observation.parameter_hash
        == ConfiguredNode(
            "smc.swing_structure", "smc.swing_structure", "regime", (), parameters, 10
        ).parameter_hash
    )


def test_swing_structure_reports_pivot_at_source_bar_when_confirmation_bar_closes() -> None:
    from smc_ict.indicators.smc import SwingStructurePlugin

    parameters = {"swing_length": 10, "show_labels": True}
    values = [("10", "11", "8", "10")]
    values.extend(("10", "11", "9", "10") for _ in range(10))
    series = candles(values)

    observation = SwingStructurePlugin(parameters).evaluate(context("regime", series), {})

    assert observation.status == "PASS"
    assert observation.event_type == "HL"
    assert observation.direction == "BULLISH"
    assert observation.level_text == "8"
    assert observation.event_time_ms == series[0].close_time_ms
    assert observation.known_time_ms == series[10].close_time_ms
    assert observation.source_manifest_ids == ("smc_luxalgo@7",)


def test_swing_structure_emits_choch_then_bos_only_on_closed_close_crossings() -> None:
    from smc_ict.indicators.smc import SwingStructurePlugin

    parameters = {"swing_length": 10, "show_labels": True}
    values = [("10", "11", "8", "10")]
    values.extend(("10", "11", "9", "10") for _ in range(10))
    values.append(("10", "15", "9", "10"))
    values.extend(("13", "14", "13", "13") for _ in range(10))
    values.append(("14", "16", "13", "16"))
    first_cross = candles(values)

    bullish = SwingStructurePlugin(parameters).evaluate(context("regime", first_cross), {})
    assert (bullish.event_type, bullish.direction, bullish.level_text) == ("BOS", "BULLISH", "15")
    assert bullish.event_time_ms == bullish.known_time_ms == first_cross[-1].close_time_ms

    values.extend(
        [
            ("15", "16", "12", "15"),
            *(("15", "17", "13", "15") for _ in range(10)),
            ("13", "14", "7", "7"),
        ]
    )
    bearish_cross = candles(values)
    bearish = SwingStructurePlugin(parameters).evaluate(context("regime", bearish_cross), {})
    assert (bearish.event_type, bearish.direction, bearish.level_text) == (
        "CHoCH",
        "BEARISH",
        "12",
    )

    values.extend(
        [
            ("7", "18", "7", "8"),
            *(("8", "17", "7", "8") for _ in range(10)),
            ("8", "16", "6", "8"),
            *(("8", "17", "7", "8") for _ in range(10)),
            ("8", "9", "5", "5"),
        ]
    )
    continuation = candles(values)
    bearish_bos = SwingStructurePlugin(parameters).evaluate(context("regime", continuation), {})
    assert (bearish_bos.event_type, bearish_bos.direction, bearish_bos.level_text) == (
        "BOS",
        "BEARISH",
        "6",
    )


def test_equal_high_low_uses_strict_atr_threshold_boundaries() -> None:
    from smc_ict.indicators.smc import _strictly_equal

    assert _strictly_equal(Decimal("100"), Decimal("100.999"), Decimal("10"), Decimal("0.1"))
    assert not _strictly_equal(Decimal("100"), Decimal("101"), Decimal("10"), Decimal("0.1"))
    assert not _strictly_equal(Decimal("100"), Decimal("101.001"), Decimal("10"), Decimal("0.1"))


def test_equal_high_low_requires_atr_warmup_then_reports_source_and_confirmation_times() -> None:
    from smc_ict.indicators.smc import EqualHighLowPlugin

    parameters = {"confirmation_bars": 3, "threshold_atr_fraction": "0.5"}
    warmup = candles([("100", "110", "90", "100")] * 199, interval="1h")
    unavailable = EqualHighLowPlugin(parameters).evaluate(
        context("context", warmup), {"smc.swing_structure": dependency("smc.swing_structure")}
    )
    assert unavailable.status == "UNAVAILABLE"

    values = [("100", "110", "90", "100")] * 200
    values.extend(
        [
            ("100", "110", "80", "100"),
            *(("100", "110", "90", "100") for _ in range(3)),
            ("100", "120", "90", "100"),
            *(("100", "110", "90", "100") for _ in range(3)),
            ("100", "110", "80", "100"),
            *(("100", "110", "90", "100") for _ in range(3)),
            ("100", "121", "90", "100"),
            *(("100", "110", "90", "100") for _ in range(3)),
        ]
    )
    series = candles(values, interval="1h")
    observation = EqualHighLowPlugin(parameters).evaluate(
        context("context", series), {"smc.swing_structure": dependency("smc.swing_structure")}
    )

    assert (observation.status, observation.event_type, observation.direction) == (
        "PASS",
        "EQH",
        "BEARISH",
    )
    assert observation.level_text == "121"
    assert observation.event_time_ms == series[-4].close_time_ms
    assert observation.known_time_ms == series[-1].close_time_ms
    assert observation.payload["previous_level_text"] == "120"


def test_order_block_selects_source_extreme_and_uses_strict_close_mitigation() -> None:
    from smc_ict.indicators.smc import OrderBlockPlugin

    parameters = {
        "scope": "internal",
        "volatility_filter": "atr",
        "mitigation_source": "close",
        "maximum_blocks": 5,
    }
    values = [("100", "110", "90", "100")] * 200
    values.extend(
        [
            ("100", "110", "80", "100"),
            *(("100", "110", "90", "100") for _ in range(5)),
            ("100", "120", "90", "100"),
            *(("100", "110", "90", "100") for _ in range(5)),
            ("100", "110", "85", "100"),
            ("100", "121", "90", "121"),
        ]
    )
    active_series = candles(values, interval="1h")
    plugin = OrderBlockPlugin(parameters)
    active = plugin.evaluate(
        context("context", active_series),
        {"smc.swing_structure": dependency("smc.swing_structure")},
    )

    assert (active.status, active.event_type, active.direction, active.state) == (
        "PASS",
        "ORDER_BLOCK",
        "BULLISH",
        "ACTIVE",
    )
    assert (active.lower_text, active.upper_text) == ("85", "110")
    assert active.payload["active_block_count"] == 1

    values.append(("90", "100", "85", "85"))
    boundary_series = candles(values, interval="1h")
    boundary = plugin.evaluate(
        context("context", boundary_series),
        {"smc.swing_structure": dependency("smc.swing_structure")},
    )
    assert boundary.status == "PASS"

    values.append(("85", "90", "84", "84"))
    mitigated_series = candles(values, interval="1h")
    mitigated = plugin.evaluate(
        context("context", mitigated_series),
        {"smc.swing_structure": dependency("smc.swing_structure")},
    )
    assert (mitigated.status, mitigated.state) == ("FAIL", "NO_ACTIVE_BLOCK")


def test_swing_event_history_is_prefix_invariant_and_parameter_ints_reject_booleans() -> None:
    from smc_ict.indicators.smc import SwingStructurePlugin, _swing_events

    values = [("10", "11", "8", "10")]
    values.extend(("10", "11", "9", "10") for _ in range(10))
    values.append(("10", "15", "9", "10"))
    values.extend(("13", "14", "13", "13") for _ in range(10))
    values.append(("14", "16", "13", "16"))
    full = candles(values)
    all_events = _swing_events(full, 10)

    for length in range(1, len(full) + 1):
        prefix_events = _swing_events(full[:length], 10)
        expected = tuple(event for event in all_events if event.known_index < length)
        assert prefix_events == expected

    with pytest.raises(TypeError, match="Boolean is not integer"):
        SwingStructurePlugin({"swing_length": True, "show_labels": True})


def test_equal_high_low_fails_closed_for_unavailable_or_mismatched_dependencies() -> None:
    from smc_ict.indicators.smc import EqualHighLowPlugin

    parameters = {"confirmation_bars": 3, "threshold_atr_fraction": "0.1"}
    plugin = EqualHighLowPlugin(parameters)
    series = candles([("100", "110", "90", "100")] * 200, interval="1h")

    unavailable = plugin.evaluate(
        context("context", series),
        {"smc.swing_structure": dependency("smc.swing_structure", status="UNAVAILABLE")},
    )
    assert unavailable.status == "UNAVAILABLE"

    with pytest.raises(ValueError, match="dependencies"):
        plugin.evaluate(
            context("context", series),
            {"smc.wrong": dependency("smc.wrong")},
        )


def test_equal_high_low_rejects_future_dependency_evidence_as_a_failure() -> None:
    from dataclasses import replace

    from smc_ict.indicators.smc import EqualHighLowPlugin

    parameters = {"confirmation_bars": 3, "threshold_atr_fraction": "0.1"}
    series = candles([("100", "110", "90", "100")] * 200, interval="1h")
    future_time = series[-1].close_time_ms + 1
    future = replace(
        dependency("smc.swing_structure"),
        event_time_ms=future_time,
        known_time_ms=future_time,
    )

    result = EqualHighLowPlugin(parameters).evaluate(
        context("context", series), {"smc.swing_structure": future}
    )

    assert (result.status, result.state) == ("FAIL", "INVALID_DEPENDENCY_EVIDENCE")


def test_equal_high_low_rejects_dependency_timeframe_mismatch() -> None:
    from dataclasses import replace

    from smc_ict.indicators.smc import EqualHighLowPlugin

    series = candles([("100", "110", "90", "100")] * 200, interval="1h")
    wrong_timeframe = replace(dependency("smc.swing_structure"), timeframe="5m")

    with pytest.raises(ValueError, match="timeframe"):
        EqualHighLowPlugin({"confirmation_bars": 3, "threshold_atr_fraction": "0.1"}).evaluate(
            context("context", series), {"smc.swing_structure": wrong_timeframe}
        )
