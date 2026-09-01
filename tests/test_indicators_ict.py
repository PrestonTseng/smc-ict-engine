from __future__ import annotations

from collections.abc import Iterable
from decimal import Decimal

import pytest

from smc_ict.application.graph import RunContext
from smc_ict.application.resampling import DerivedCandle
from smc_ict.domain import Observation


def candles(
    values: Iterable[tuple[str, str, str, str]], *, interval: str = "5m"
) -> tuple[DerivedCandle, ...]:
    from smc_ict.domain import Timeframe

    duration = Timeframe(interval).duration_minutes * 60_000
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


def context(series: tuple[DerivedCandle, ...]) -> RunContext:
    return RunContext("BTC-USDT-PERP", series[-1].close_time_ms, {"execution": series})


def dependency(
    signal_id: str,
    *,
    direction: str = "LONG",
    status: str = "PASS",
    level: str | None = None,
) -> Observation:
    return Observation.available(
        signal_id=signal_id,
        instrument_id="BTC-USDT-PERP",
        timeframe="1h" if signal_id.startswith("smc.") else "5m",
        status=status,
        event_type="FIXTURE" if status == "PASS" else None,
        direction=direction if status == "PASS" else None,
        event_time_ms=0 if status == "PASS" else None,
        known_time_ms=0 if status == "PASS" else None,
        state="CONFIRMED" if status == "PASS" else "UNAVAILABLE",
        dependency_ids=(),
        parameter_hash="a" * 64,
        source_manifest_ids=("fixture",),
        payload_schema_version=1,
        bounded_reason="fixture dependency",
        payload={},
        level_text=level,
    )


def liquidity_values() -> list[tuple[str, str, str, str]]:
    return [
        ("100", "105", "98", "100"),
        ("100", "104", "99", "100"),
        ("100", "105", "98", "100"),
        ("100", "110", "99", "100"),
        ("100", "105", "98", "100"),
        ("100", "105", "95", "100"),
        ("100", "104", "90", "100"),
        ("100", "105", "98", "100"),
        ("100", "105", "99", "100"),
        ("100", "110.2", "99", "100"),
        ("100", "105", "98", "100"),
        ("100", "105", "95", "100"),
        ("100", "104", "90", "100"),
        ("100", "105", "98", "100"),
        ("100", "105", "99", "100"),
        ("100", "109.9", "99", "100"),
        ("100", "105", "98", "100"),
    ]


def mirrored(values: list[tuple[str, str, str, str]]) -> list[tuple[str, str, str, str]]:
    ceiling = Decimal("200")
    return [
        (
            str(ceiling - Decimal(opening)),
            str(ceiling - Decimal(low)),
            str(ceiling - Decimal(high)),
            str(ceiling - Decimal(close)),
        )
        for opening, high, low, close in values
    ]


def test_execution_plugin_uses_the_configured_15m_candle_identity() -> None:
    from smc_ict.indicators.ict import ClusteredLiquidityPlugin

    series = candles([("100", "101", "99", "100")] * 12, interval="15m")
    observation = ClusteredLiquidityPlugin(
        {"pivot_width": 5, "minimum_pivots": 3, "margin_atr_fraction": "0.4"}
    ).evaluate(context(series), {"smc.equal_high_low": dependency("smc.equal_high_low")})

    assert observation.timeframe == "15m"


def test_execution_plugin_retains_configured_15m_identity_when_no_bars_are_available() -> None:
    from smc_ict.indicators.ict import ClusteredLiquidityPlugin

    plugin = ClusteredLiquidityPlugin(
        {"pivot_width": 5, "minimum_pivots": 3, "margin_atr_fraction": "0.4"}
    )
    unavailable = dependency("smc.equal_high_low", status="UNAVAILABLE")

    observation = plugin.evaluate(
        RunContext("BTC-USDT-PERP", 0, {"execution": ()}, {"execution": "15m"}),
        {"smc.equal_high_low": unavailable},
    )

    assert (observation.status, observation.timeframe) == ("UNAVAILABLE", "15m")


def test_clustered_liquidity_requires_three_pivots_and_tracks_strict_traversal() -> None:
    from smc_ict.indicators.ict import ClusteredLiquidityPlugin

    parameters = {"pivot_width": 3, "minimum_pivots": 3, "margin_atr_fraction": "0.4"}
    plugin = ClusteredLiquidityPlugin(parameters)
    dep = {"smc.equal_high_low": dependency("smc.equal_high_low")}
    series = candles(liquidity_values())

    active = plugin.evaluate(context(series), dep)

    assert (active.status, active.event_type, active.direction, active.state) == (
        "PASS",
        "CLUSTERED_LIQUIDITY",
        "SHORT",
        "ACTIVE",
    )
    assert active.payload["liquidity_side"] == "BUYSIDE"
    assert active.event_time_ms == series[-2].close_time_ms
    assert active.known_time_ms == series[-1].close_time_ms

    upper = Decimal(active.upper_text or "0")
    values = liquidity_values()
    values.append(("100", str(upper + 1), "99", str(upper)))
    boundary = plugin.evaluate(context(candles(values)), dep)
    assert boundary.state == "PARTIAL"
    assert boundary.event_time_ms == active.event_time_ms
    assert boundary.known_time_ms == candles(values)[-1].close_time_ms

    values.append((str(upper), str(upper + 1), "99", str(upper + Decimal("0.001"))))
    traversed = plugin.evaluate(context(candles(values)), dep)
    assert traversed.state == "FULL"
    assert traversed.payload["swept_extreme_text"] == str(upper + 1)
    assert traversed.known_time_ms == candles(values)[-1].close_time_ms

    values.append((str(upper), str(upper + 10), "99", str(upper + 2)))
    replayed = plugin.evaluate(context(candles(values)), dep)
    assert replayed.state == "FULL"
    assert replayed.payload["swept_extreme_text"] == str(upper + 1)
    assert replayed.known_time_ms == traversed.known_time_ms


def test_clustered_liquidity_is_bearishly_symmetric() -> None:
    from smc_ict.indicators.ict import ClusteredLiquidityPlugin

    plugin = ClusteredLiquidityPlugin(
        {"pivot_width": 3, "minimum_pivots": 3, "margin_atr_fraction": "0.4"}
    )
    dep = {"smc.equal_high_low": dependency("smc.equal_high_low")}
    values = mirrored(liquidity_values())
    observation = plugin.evaluate(context(candles(values)), dep)

    assert (observation.status, observation.direction, observation.payload["liquidity_side"]) == (
        "PASS",
        "LONG",
        "SELLSIDE",
    )

    lower = Decimal(observation.lower_text or "0")
    values.append(("100", "101", str(lower - 1), str(lower - Decimal("0.001"))))
    traversed = plugin.evaluate(context(candles(values)), dep)
    assert traversed.state == "FULL"
    assert traversed.payload["swept_extreme_text"] == str(lower - 1)

    values.append(("100", "101", str(lower - 10), str(lower - 2)))
    replayed = plugin.evaluate(context(candles(values)), dep)
    assert replayed.payload["swept_extreme_text"] == str(lower - 1)
    assert replayed.known_time_ms == traversed.known_time_ms


def test_completed_liquidity_zone_is_immutable_after_later_same_anchor_cluster() -> None:
    from smc_ict.indicators.ict import _liquidity_zones

    values = liquidity_values()
    active = _liquidity_zones(candles(values), 3, 3, Decimal("0.4"))[0]
    values.append(
        (
            "100",
            str(active.upper + 1),
            "99",
            str(active.upper + Decimal("0.001")),
        )
    )
    completed = next(
        zone
        for zone in _liquidity_zones(candles(values), 3, 3, Decimal("0.4"))
        if zone.side == "BUYSIDE" and zone.state == "FULL"
    )
    frozen = (
        completed.lower,
        completed.upper,
        completed.event_index,
        completed.known_index,
        completed.swept_extreme,
    )

    values.extend(
        [
            ("100", "101", "99", "100"),
            ("100", "101", "99", "100"),
            ("100", "101", "80", "100"),
            ("100", "101", "99", "100"),
            ("100", "102", "99", "100"),
            ("100", "103", "99", "100"),
            ("100", "112", "99", "100"),
            ("100", "101", "99", "100"),
        ]
    )
    replayed = next(
        zone
        for zone in _liquidity_zones(candles(values), 3, 3, Decimal("0.4"))
        if zone.side == "BUYSIDE" and zone.state == "FULL"
    )

    assert (
        replayed.lower,
        replayed.upper,
        replayed.event_index,
        replayed.known_index,
        replayed.swept_extreme,
    ) == frozen


def structure_values() -> list[tuple[str, str, str, str]]:
    return [
        ("100", "105", "98", "100"),
        ("100", "104", "99", "100"),
        ("100", "105", "98", "100"),
        ("100", "110", "99", "100"),
        ("100", "105", "98", "100"),
        ("100", "105", "95", "100"),
        ("100", "104", "90", "100"),
        ("100", "105", "98", "100"),
        ("100", "105", "99", "100"),
        ("100", "111", "99", "100"),
        ("100", "105", "98", "100"),
        ("100", "105", "95", "100"),
        ("100", "104", "90", "100"),
        ("100", "105", "98", "100"),
        ("100", "105", "99", "100"),
        ("100", "115", "99", "100"),
        ("100", "112", "98", "112"),
    ]


def test_market_structure_emits_first_direction_mss_then_new_level_bos() -> None:
    from smc_ict.indicators.ict import MarketStructurePlugin

    parameters = {"pivot_width": 3, "emit_mss": True, "emit_bos": True}
    plugin = MarketStructurePlugin(parameters)
    dep = {"ict.clustered_liquidity": dependency("ict.clustered_liquidity")}
    first = candles(structure_values())

    mss = plugin.evaluate(context(first), dep)
    assert (mss.status, mss.event_type, mss.direction, mss.level_text) == (
        "PASS",
        "MSS",
        "BULLISH",
        "111",
    )

    values = structure_values()
    values.extend(
        [
            ("112", "113", "100", "110"),
            ("110", "113", "95", "110"),
            ("110", "112", "89", "100"),
            ("100", "113", "95", "100"),
            ("100", "113", "98", "100"),
            ("100", "113", "99", "100"),
            ("100", "116", "99", "100"),
            ("100", "115.5", "98", "115.5"),
        ]
    )
    bos = plugin.evaluate(context(candles(values)), dep)
    assert (bos.event_type, bos.direction, bos.level_text) == ("BOS", "BULLISH", "115")


def test_market_event_history_is_prefix_invariant() -> None:
    from smc_ict.indicators.ict import _market_events

    values = structure_values()
    values.extend(
        [
            ("112", "113", "100", "110"),
            ("110", "113", "95", "110"),
            ("110", "112", "89", "100"),
            ("100", "113", "95", "100"),
            ("100", "113", "98", "100"),
            ("100", "113", "99", "100"),
            ("100", "116", "99", "100"),
            ("100", "115.5", "98", "115.5"),
        ]
    )
    full = candles(values)
    all_events = _market_events(full, 3)

    for length in range(1, len(full) + 1):
        prefix = _market_events(full[:length], 3)
        assert prefix == tuple(event for event in all_events if event.index < length)


def test_market_structure_is_bearishly_symmetric_and_rejects_boolean_width() -> None:
    from smc_ict.indicators.ict import MarketStructurePlugin

    plugin = MarketStructurePlugin({"pivot_width": 3, "emit_mss": True, "emit_bos": True})
    observation = plugin.evaluate(
        context(candles(mirrored(structure_values()))),
        {"ict.clustered_liquidity": dependency("ict.clustered_liquidity")},
    )

    assert (
        observation.status,
        observation.event_type,
        observation.direction,
        observation.level_text,
    ) == (
        "PASS",
        "MSS",
        "BEARISH",
        "89",
    )
    with pytest.raises(TypeError, match="Boolean is not integer"):
        MarketStructurePlugin({"pivot_width": True, "emit_mss": True, "emit_bos": True})


@pytest.mark.parametrize("width", [3, 5, 10])
def test_ict_plugins_accept_source_valid_left_pivot_width(width: int) -> None:
    from smc_ict.indicators.ict import ClusteredLiquidityPlugin, MarketStructurePlugin

    ClusteredLiquidityPlugin(
        {"pivot_width": width, "minimum_pivots": 3, "margin_atr_fraction": "0.4"}
    )
    MarketStructurePlugin({"pivot_width": width, "emit_mss": True, "emit_bos": True})


@pytest.mark.parametrize("width", [True, 2, 11])
def test_ict_plugins_reject_boolean_or_out_of_range_left_pivot_width(width: object) -> None:
    from smc_ict.indicators.ict import ClusteredLiquidityPlugin, MarketStructurePlugin

    with pytest.raises((TypeError, ValueError), match="pivot_width"):
        ClusteredLiquidityPlugin(
            {"pivot_width": width, "minimum_pivots": 3, "margin_atr_fraction": "0.4"}
        )
    with pytest.raises((TypeError, ValueError), match="pivot_width"):
        MarketStructurePlugin({"pivot_width": width, "emit_mss": True, "emit_bos": True})


@pytest.mark.parametrize("margin", ["0.2", "0.4", "0.7"])
def test_clustered_liquidity_accepts_source_representable_margin_tenths(margin: str) -> None:
    from smc_ict.indicators.ict import ClusteredLiquidityPlugin

    ClusteredLiquidityPlugin({"pivot_width": 5, "minimum_pivots": 3, "margin_atr_fraction": margin})


@pytest.mark.parametrize("margin", ["0.1", "0.25", "0.8"])
def test_clustered_liquidity_rejects_non_source_margin_fraction(margin: str) -> None:
    from smc_ict.indicators.ict import ClusteredLiquidityPlugin

    with pytest.raises(ValueError, match="margin_atr_fraction"):
        ClusteredLiquidityPlugin(
            {"pivot_width": 5, "minimum_pivots": 3, "margin_atr_fraction": margin}
        )


def fvg_values() -> list[tuple[str, str, str, str]]:
    return [
        ("98", "100", "97", "99"),
        ("98", "100", "97", "99"),
        ("100", "111", "99", "110"),
        ("102", "112", "101", "103"),
    ]


def test_ordinary_fvg_requires_displacement_and_full_traversal_is_strict() -> None:
    from smc_ict.indicators.ict import FairValueGapPlugin

    parameters = {
        "kind": "ordinary",
        "require_displacement": True,
        "displacement_length": 3,
        "mitigation": "full_traversal",
    }
    plugin = FairValueGapPlugin(parameters)
    dep = {"ict.market_structure": dependency("ict.market_structure", direction="BULLISH")}
    series = candles(fvg_values())

    active = plugin.evaluate(context(series), dep)
    assert (active.status, active.event_type, active.direction, active.state) == (
        "PASS",
        "FAIR_VALUE_GAP",
        "BULLISH",
        "ACTIVE",
    )
    assert (active.lower_text, active.upper_text, active.level_text) == ("100", "101", "100.5")
    assert active.dependency_ids == ("ict.market_structure",)
    assert active.payload["market_structure_direction"] == "BULLISH"

    values = fvg_values()
    values.append(("103", "104", "100", "102"))
    boundary = plugin.evaluate(context(candles(values)), dep)
    assert (boundary.status, boundary.state) == ("PASS", "PARTIAL")
    assert boundary.event_time_ms == active.event_time_ms
    assert boundary.known_time_ms == candles(values)[-1].close_time_ms

    values.append(("102", "103", "99.9", "101"))
    mitigated = plugin.evaluate(context(candles(values)), dep)
    assert (mitigated.status, mitigated.state) == ("FAIL", "NO_ACTIVE_GAP")


def test_ordinary_fvg_rejects_gap_that_contradicts_market_structure_direction() -> None:
    from smc_ict.indicators.ict import FairValueGapPlugin

    plugin = FairValueGapPlugin(
        {
            "kind": "ordinary",
            "require_displacement": True,
            "displacement_length": 3,
            "mitigation": "full_traversal",
        }
    )

    result = plugin.evaluate(
        context(candles(fvg_values())),
        {"ict.market_structure": dependency("ict.market_structure", direction="BEARISH")},
    )

    assert (result.status, result.state) == ("FAIL", "CONTRADICTORY_EVIDENCE")
    assert result.bounded_reason == "FVG direction contradicts market-structure dependency"


def test_ordinary_fvg_knowledge_time_includes_retained_market_structure_evidence() -> None:
    from dataclasses import replace

    from smc_ict.indicators.ict import FairValueGapPlugin

    values = fvg_values()
    values.append(("102", "112", "101", "102"))
    series = candles(values)
    market_structure = replace(
        dependency("ict.market_structure", direction="BULLISH"),
        event_time_ms=series[-1].close_time_ms,
        known_time_ms=series[-1].close_time_ms,
    )

    result = FairValueGapPlugin(
        {
            "kind": "ordinary",
            "require_displacement": True,
            "displacement_length": 3,
            "mitigation": "full_traversal",
        }
    ).evaluate(context(series), {"ict.market_structure": market_structure})

    assert result.status == "PASS"
    assert result.known_time_ms == market_structure.known_time_ms
    assert result.payload["market_structure_known_time_ms"] == market_structure.known_time_ms


def test_one_right_bar_same_candle_high_low_collision_is_high_then_low() -> None:
    from smc_ict.indicators.ict import _pivot_candidates

    series = candles(
        [
            ("100", "105", "95", "100"),
            ("100", "110", "90", "100"),
            ("100", "105", "95", "100"),
        ]
    )

    assert _pivot_candidates(series, 1) == ((2, 1), (2, -1))


def test_liquidity_cluster_scan_stops_at_first_same_side_point_above_margin() -> None:
    from smc_ict.indicators.ict import _matching_liquidity_points, _ZigzagPoint

    points = [
        _ZigzagPoint(1, 9, Decimal("100")),
        _ZigzagPoint(-1, 8, Decimal("90")),
        _ZigzagPoint(1, 7, Decimal("105")),
        _ZigzagPoint(-1, 6, Decimal("90")),
        _ZigzagPoint(1, 5, Decimal("100.2")),
        _ZigzagPoint(-1, 4, Decimal("90")),
        _ZigzagPoint(1, 3, Decimal("99.9")),
    ]

    matching = _matching_liquidity_points(points, 1, Decimal("100"), Decimal("1"))

    assert tuple(point.index for point in matching) == (9,)


def test_market_structure_selects_previous_zigzag_high_and_low_not_current_point() -> None:
    from smc_ict.indicators.ict import _selected_structure_points, _ZigzagPoint

    current_high = _ZigzagPoint(1, 9, Decimal("115"))
    previous_low = _ZigzagPoint(-1, 7, Decimal("92"))
    previous_high = _ZigzagPoint(1, 5, Decimal("111"))
    points = [current_high, previous_low, previous_high]

    high, low = _selected_structure_points(points)

    assert high is previous_high
    assert low is previous_low


def test_consecutive_ordinary_fvg_extends_latest_gap_instead_of_creating_a_second_gap() -> None:
    from smc_ict.indicators.ict import _active_gaps

    series = candles(
        [
            ("99", "100", "98", "99"),
            ("99", "100", "98", "99"),
            ("101", "102", "101", "102"),
            ("102", "103", "102", "103"),
        ]
    )

    gaps = _active_gaps(series, displacement_length=3, require_displacement=False)

    assert len(gaps) == 1
    assert (gaps[0].direction, gaps[0].lower, gaps[0].upper) == (
        "BULLISH",
        Decimal("100"),
        Decimal("102"),
    )


def test_ordinary_fvg_is_bearishly_symmetric() -> None:
    from smc_ict.indicators.ict import FairValueGapPlugin

    parameters = {
        "kind": "ordinary",
        "require_displacement": True,
        "displacement_length": 3,
        "mitigation": "full_traversal",
    }
    series = candles(
        [
            ("102", "103", "100", "101"),
            ("102", "103", "100", "101"),
            ("110", "111", "99", "100"),
            ("98", "99", "90", "97"),
        ]
    )
    observation = FairValueGapPlugin(parameters).evaluate(
        context(series),
        {"ict.market_structure": dependency("ict.market_structure", direction="BEARISH")},
    )

    assert (observation.status, observation.direction) == ("PASS", "BEARISH")
    assert (observation.lower_text, observation.upper_text, observation.level_text) == (
        "99",
        "100",
        "99.5",
    )
