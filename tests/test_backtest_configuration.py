from __future__ import annotations

from pathlib import Path

import pytest

from trading_research.configuration import StrictConfigurationError

VALID_SCENARIO = """\
backtest:
  name: one-year-baseline
  version: "1"
  strategy: source-aligned-research.yaml
  period:
    start: "2025-09-01T00:00:00Z"
    end: "2026-08-31T23:59:00Z"
  entry:
    mode: touch_limit
    expiry_execution_bars: 6
  execution:
    maximum_holding_minutes: 360
    intrabar_conflict: stop_first
    allow_same_minute_target: false
  costs:
    taker_fee_bps: "5"
    adverse_slippage_bps: "2"
  output:
    existing_result: fail
"""


def test_backtest_scenario_is_strict_canonical_and_resolves_only_its_exact_strategy(
    tmp_path: Path,
) -> None:
    from trading_research.configuration import (
        hash_backtest,
        load_backtest_text,
        resolve_backtest_strategy_path,
    )

    strategies = tmp_path / "strategies"
    scenarios = tmp_path / "backtests" / "source-aligned-research"
    strategies.mkdir()
    scenarios.mkdir(parents=True)
    selected = strategies / "source-aligned-research.yaml"
    selected.write_text("name: selected\n", encoding="utf-8")
    (strategies / "another.yaml").write_text("name: another\n", encoding="utf-8")
    scenario_path = scenarios / "one-year-baseline.yaml"
    scenario_path.write_text(VALID_SCENARIO, encoding="utf-8")

    scenario = load_backtest_text(VALID_SCENARIO)

    assert scenario.period.start_ms == 1_756_684_800_000
    assert scenario.period.end_ms == 1_788_220_740_000
    assert scenario.costs.taker_fee_bps == "5"
    assert len(hash_backtest(scenario)) == 64
    assert resolve_backtest_strategy_path(scenario_path, scenario) == selected


@pytest.mark.parametrize(
    ("old", "new", "message"),
    [
        ('start: "2025-09-01T00:00:00Z"', 'start: "2025-09-01T00:00:01Z"', "UTC minute"),
        ('end: "2026-08-31T23:59:00Z"', 'end: "2025-08-31T23:59:00Z"', "start must precede"),
        ("expiry_execution_bars: 6", "expiry_execution_bars: true", "Boolean"),
        ('taker_fee_bps: "5"', 'taker_fee_bps: "5.0"', "canonical decimal"),
        (
            "  output:\n    existing_result: fail",
            "  provider: okx_swap\n  output:\n    existing_result: fail",
            "unknown fields",
        ),
        (
            "strategy: source-aligned-research.yaml",
            "strategy: ../source-aligned-research.yaml",
            "strategy leaf",
        ),
    ],
)
def test_backtest_scenario_rejects_ambiguous_or_forbidden_values(
    old: str, new: str, message: str
) -> None:
    from trading_research.configuration import load_backtest_text

    with pytest.raises(StrictConfigurationError, match=message):
        load_backtest_text(VALID_SCENARIO.replace(old, new))


def test_backtest_required_range_includes_the_exact_strategy_warmup() -> None:
    from trading_research.application.backtesting import RequiredRangeResolver
    from trading_research.configuration import load_backtest_text

    scenario = load_backtest_text(VALID_SCENARIO)

    required = RequiredRangeResolver.resolve(scenario.period, history_minutes=129_600)

    assert required.start_open_ms == scenario.period.start_ms - 129_600 * 60_000
    assert required.end_open_ms == scenario.period.end_ms


def test_checked_in_scenario_binds_the_checked_in_strategy() -> None:
    from trading_research.configuration import load_backtest, resolve_backtest_strategy_path

    root = Path(__file__).parents[1]
    scenario_path = root / "backtests/source-aligned-research/one-year-baseline.yaml"
    scenario = load_backtest(scenario_path)

    assert resolve_backtest_strategy_path(scenario_path, scenario) == (
        root / "strategies/source-aligned-research.yaml"
    )
