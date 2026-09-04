from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_backtest_reporting import MARKET, SCENARIO

from smc_ict.adapters.persistence.sqlite import SQLiteRepository
from smc_ict.application.graph import ConfiguredNode, RunContext
from smc_ict.application.ports import InstrumentMapping
from smc_ict.configuration import load_backtest_text, load_market_data_text
from smc_ict.configuration.models import SignalConfig, StrategyConfig, frozen_mapping
from smc_ict.domain import ClosedCandle, Observation

INTEGRATION_SCENARIO = SCENARIO.replace(
    'start: "2026-01-01T00:00:00Z"', 'start: "1970-01-01T00:05:00Z"'
).replace('end: "2026-01-01T00:01:00Z"', 'end: "1970-01-01T00:14:00Z"')


def _candle(minute: int) -> ClosedCandle:
    return ClosedCandle(
        provider_id="okx_swap",
        market_type="LINEAR_PERPETUAL",
        instrument_id="BTC-USDT-PERP",
        provider_symbol="BTC-USDT-SWAP",
        interval="1m",
        open_time_ms=minute * 60_000,
        close_time_ms=minute * 60_000 + 59_999,
        open="100",
        high="100",
        low="100",
        close="100",
        base_volume="1",
        quote_volume="100",
        source_fields={"contract_volume": "1"},
    )


class _LevelsPlugin:
    plugin_id = "project.risk_levels"

    def __init__(self, parameters: Mapping[str, object]) -> None:
        self._parameter_hash = ConfiguredNode(
            "project.risk_levels",
            "project.risk_levels",
            "execution",
            (),
            parameters,
            1,
            "5m",
        ).parameter_hash

    def evaluate(self, context: RunContext, dependencies: Mapping[str, Observation]) -> Observation:
        del dependencies
        return Observation.available(
            signal_id="project.risk_levels",
            instrument_id=context.instrument_id,
            timeframe="5m",
            status="PASS",
            event_type=None,
            direction=None,
            event_time_ms=context.evaluation_time_ms,
            known_time_ms=context.evaluation_time_ms,
            state="fixture",
            dependency_ids=(),
            parameter_hash=self._parameter_hash,
            source_manifest_ids=(),
            payload_schema_version=1,
            bounded_reason="FIXTURE_PASS",
            payload={
                "direction": "LONG",
                "entry_text": "100",
                "stop_text": "99",
                "target_text": "102",
            },
        )


class _PluginRegistry:
    def resolve(self, plugin_id: str):
        assert plugin_id == "project.risk_levels"
        return _LevelsPlugin


class _CompleteProvider:
    provider_id = "okx_swap"

    def validate_instrument(self, mapping: InstrumentMapping) -> None:
        assert mapping == InstrumentMapping("BTC-USDT-PERP", "BTC-USDT-SWAP")

    def latest_closed_open_time_ms(self) -> int:
        return 14 * 60_000

    def fetch_page(self, *args: object, **kwargs: object) -> object:
        raise AssertionError("complete readiness range must not access the network")


def _strategy() -> StrategyConfig:
    return StrategyConfig(
        "fixture",
        "1",
        ("BTC-USDT-PERP",),
        5,
        frozen_mapping({"execution": "5m"}),
        (
            SignalConfig(
                "project.risk_levels",
                "execution",
                (),
                frozen_mapping({"minimum_reward_risk": "2"}),
                True,
                "LEVELS",
                1,
            ),
        ),
    )


def test_backtest_composition_runs_readiness_snapshot_offline_replay_and_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from smc_ict.composition import runtime_services

    data = tmp_path / "data"
    config = tmp_path / "config"
    data.mkdir()
    config.mkdir()
    scenario_path = tmp_path / "backtests" / "fixture" / "fixture.yaml"
    scenario_path.parent.mkdir(parents=True)
    scenario_path.write_text(INTEGRATION_SCENARIO, encoding="utf-8")
    repository = SQLiteRepository(data / "smc_ict.db")
    repository.store_candle_page(
        tuple(_candle(minute) for minute in range(15)),
        successful_sync_ms=15 * 60_000,
        required_start_open_ms=0,
    )
    table_counts_before = repository.database_status()
    monkeypatch.setenv("DATA_FOLDER", str(data))
    monkeypatch.setenv("CONFIG_FOLDER", str(config))
    monkeypatch.setenv("SMC_ICT_GIT_COMMIT", "4" * 40)
    monkeypatch.setattr(
        runtime_services,
        "load_backtest",
        lambda _path: load_backtest_text(INTEGRATION_SCENARIO),
    )
    monkeypatch.setattr(
        runtime_services, "resolve_backtest_strategy_path", lambda *_args: tmp_path / "fixture.yaml"
    )
    monkeypatch.setattr(runtime_services, "load_strategy", lambda _path: _strategy())
    monkeypatch.setattr(
        runtime_services, "load_market_data", lambda _path: load_market_data_text(MARKET)
    )
    monkeypatch.setattr(
        runtime_services, "build_market_provider", lambda *_args: _CompleteProvider()
    )
    monkeypatch.setattr(
        runtime_services,
        "indicator_composition_root",
        lambda: SimpleNamespace(plugins=_PluginRegistry()),
    )

    receipt = runtime_services.run_backtest(scenario_path)

    assert receipt.status == "SUCCEEDED"
    result = Path(receipt.path)
    assert result.parent == data / "backtests"
    assert json.loads((result / "summary.json").read_bytes())["decision_status_counts"] == [
        ["READY", 2]
    ]
    assert not tuple(data.glob(".backtest-*"))
    assert repository.database_status() == table_counts_before


def test_backtest_cli_accepts_only_a_scenario_and_delegates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from smc_ict import cli

    calls: list[str] = []
    receipt = SimpleNamespace(
        canonical_dict=lambda: {
            "status": "SUCCEEDED",
            "backtest_id": "a" * 64,
            "path": str(tmp_path / "backtests" / ("a" * 64)),
            "artifact_count": 6,
            "reused_existing": False,
        }
    )
    monkeypatch.setattr(
        cli,
        "run_backtest",
        lambda scenario: (calls.append(scenario), receipt)[1],
        raising=False,
    )

    payload = cli._execute(cli._parser().parse_args(["backtest", "scenario.yaml"]))

    assert payload["status"] == "SUCCEEDED"
    assert calls == ["scenario.yaml"]
    with pytest.raises(SystemExit):
        cli._parser().parse_args(["backtest", "scenario.yaml", "--market-data", "other.yaml"])
