"""Atomic immutable JSON/JSONL publication for backtest evidence."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from smc_ict.adapters.reporting.html import render_report
from smc_ict.application.backtesting import BacktestIdentity
from smc_ict.application.execution_simulator import SimulationResult
from smc_ict.application.metrics import MetricsReport
from smc_ict.configuration.models import (
    BacktestScenarioConfig,
    MarketDataConfig,
    StrategyConfig,
)
from smc_ict.domain.backtesting import ReplayResult


@dataclass(frozen=True, slots=True)
class ReportPublication:
    status: str
    backtest_id: str
    path: Path
    artifact_count: int
    reused_existing: bool = False

    def canonical_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "backtest_id": self.backtest_id,
            "path": str(self.path),
            "artifact_count": self.artifact_count,
            "reused_existing": self.reused_existing,
        }


class BacktestReportPublisher:
    """Build a complete sibling directory and expose it with one atomic rename."""

    def __init__(self, root: str | Path) -> None:
        self._root = Path(root)

    def publish(
        self,
        *,
        identity: BacktestIdentity,
        scenario: BacktestScenarioConfig,
        strategy: StrategyConfig,
        market_data: MarketDataConfig,
        replay: ReplayResult,
        simulation: SimulationResult,
        metrics: MetricsReport,
    ) -> ReportPublication:
        self._root.mkdir(parents=True, exist_ok=True)
        destination = self._root / identity.backtest_id
        staging = Path(tempfile.mkdtemp(prefix=f".{identity.backtest_id}.", dir=self._root))
        try:
            decision_rows = [
                {
                    "instrument_id": item.instrument_id,
                    "evaluation_time_ms": item.evaluation_time_ms,
                    "decision": item.decision.canonical_dict(),
                    "decision_hash": item.decision_hash,
                }
                for item in replay.evaluations
            ]
            trace_rows = [item.trace.canonical_dict() for item in replay.evaluations]
            trade_rows = [item.canonical_dict() for item in simulation.trades]
            summary = metrics.canonical_dict()
            manifest_base = self._manifest_base(identity, scenario, strategy, market_data, replay)
            html_traces = [
                row | {"disposition": item.decision.status}
                for row, item in zip(trace_rows, replay.evaluations, strict=True)
            ]
            payloads = {
                "decisions.jsonl": _canonical_jsonl(decision_rows),
                "pipeline-traces.jsonl": _canonical_jsonl(trace_rows),
                "trades.jsonl": _canonical_jsonl(trade_rows),
                "summary.json": _canonical_json(summary),
                "report.html": render_report(
                    manifest=manifest_base,
                    summary=summary,
                    traces=html_traces,
                ),
            }
            for name, payload in payloads.items():
                _write_synced(staging / name, payload)
            artifacts = {
                name: {"bytes": len(payload), "sha256": sha256(payload).hexdigest()}
                for name, payload in sorted(payloads.items())
            }
            _write_synced(
                staging / "manifest.json",
                _canonical_json(manifest_base | {"artifacts": artifacts}),
            )
            _sync_directory(staging)
            if destination.is_symlink():
                raise FileExistsError(f"existing backtest result differs: {destination}")
            if destination.exists():
                if _directories_match(staging, destination):
                    shutil.rmtree(staging)
                    return ReportPublication(
                        "EXISTING_IDENTICAL", identity.backtest_id, destination, 6, True
                    )
                raise FileExistsError(f"existing backtest result differs: {destination}")
            os.rename(staging, destination)
            _sync_directory(self._root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return ReportPublication("SUCCEEDED", identity.backtest_id, destination, 6)

    @staticmethod
    def _manifest_base(
        identity: BacktestIdentity,
        scenario: BacktestScenarioConfig,
        strategy: StrategyConfig,
        market_data: MarketDataConfig,
        replay: ReplayResult,
    ) -> dict[str, object]:
        return {
            "schema_version": 1,
            "backtest_id": identity.backtest_id,
            "identity": {
                "scenario_hash": identity.scenario_hash,
                "strategy_hash": identity.strategy_hash,
                "market_data_hash": identity.market_data_hash,
                "candle_data_hash": identity.candle_data_hash,
                "git_commit": identity.git_commit,
                "period": identity.period.canonical_dict(),
                "required_range": identity.required_range.canonical_dict(),
            },
            "scenario": scenario.canonical_dict(),
            "strategy": strategy.canonical_dict(),
            "market_data": market_data.canonical_dict(),
            "coverage": {
                "candle_count": replay.candle_count,
                "evaluation_count": len(replay.evaluations),
            },
            "assumptions": {
                "research_only": True,
                "position_sizing": False,
                "live_order_submission": False,
                "performance_claim": False,
            },
        }


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )
        + "\n"
    ).encode("utf-8")


def _canonical_jsonl(rows: list[dict[str, object]]) -> bytes:
    return b"".join(_canonical_json(row) for row in rows)


def _write_synced(path: Path, payload: bytes) -> None:
    with path.open("xb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _directories_match(expected: Path, actual: Path) -> bool:
    if not actual.is_dir():
        return False
    expected_names = {path.name for path in expected.iterdir() if path.is_file()}
    actual_names = {path.name for path in actual.iterdir() if path.is_file()}
    if expected_names != actual_names:
        return False
    return all(
        (expected / name).read_bytes() == (actual / name).read_bytes() for name in expected_names
    )
