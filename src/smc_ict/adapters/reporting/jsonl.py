"""Atomic immutable JSON/JSONL publication for backtest evidence."""

from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path

from smc_ict.adapters.reporting import html as html_reporting
from smc_ict.application.backtesting import BacktestIdentity
from smc_ict.application.execution_simulator import SimulationResult
from smc_ict.application.metrics import MetricsReport
from smc_ict.configuration.models import (
    BacktestScenarioConfig,
    MarketDataConfig,
    StrategyConfig,
)
from smc_ict.domain.backtesting import ReplayResult

_ARTIFACT_NAMES = frozenset(
    {
        "decisions.jsonl",
        "manifest.json",
        "pipeline-traces.jsonl",
        "report.html",
        "summary.json",
        "trades.jsonl",
    }
)

# Kept as the small-report compatibility API. Publication deliberately does not call it.
render_report = html_reporting.render_report


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
            summary = metrics.canonical_dict()
            manifest_base = self._manifest_base(identity, scenario, strategy, market_data, replay)

            def decision_rows() -> Iterable[dict[str, object]]:
                for item in replay.evaluations:
                    yield {
                        "instrument_id": item.instrument_id,
                        "evaluation_time_ms": item.evaluation_time_ms,
                        "decision": item.decision.canonical_dict(),
                        "decision_hash": item.decision_hash,
                    }

            def trace_rows() -> Iterable[dict[str, object]]:
                return (item.trace.canonical_dict() for item in replay.evaluations)

            def trade_rows() -> Iterable[dict[str, object]]:
                return (item.canonical_dict() for item in simulation.trades)

            _write_jsonl_synced(staging / "decisions.jsonl", decision_rows())
            _write_jsonl_synced(staging / "pipeline-traces.jsonl", trace_rows())
            _write_jsonl_synced(staging / "trades.jsonl", trade_rows())
            _write_synced(staging / "summary.json", _canonical_json(summary))
            _write_report_synced(
                staging / "report.html",
                manifest=manifest_base,
                summary=summary,
                decisions=decision_rows,
                traces=trace_rows,
                trades=trade_rows,
            )
            artifacts = {
                name: _file_metadata(staging / name)
                for name in sorted(_ARTIFACT_NAMES - {"manifest.json"})
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
                        "EXISTING_IDENTICAL",
                        identity.backtest_id,
                        destination,
                        len(_ARTIFACT_NAMES),
                        True,
                    )
                raise FileExistsError(f"existing backtest result differs: {destination}")
            os.rename(staging, destination)
            _sync_directory(self._root)
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        return ReportPublication(
            "SUCCEEDED", identity.backtest_id, destination, len(_ARTIFACT_NAMES)
        )

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


def _write_jsonl_synced(path: Path, rows: Iterable[dict[str, object]]) -> None:
    with path.open("xb") as handle:
        for row in rows:
            handle.write(_canonical_json(row))
        handle.flush()
        os.fsync(handle.fileno())


def _write_report_synced(
    path: Path,
    *,
    manifest: dict[str, object],
    summary: dict[str, object],
    decisions: html_reporting.RowSource,
    traces: html_reporting.RowSource,
    trades: html_reporting.RowSource,
) -> None:
    with path.open("xb") as handle:
        html_reporting.write_report(
            handle,
            manifest=manifest,
            summary=summary,
            decisions=decisions,
            traces=traces,
            trades=trades,
        )
        handle.flush()
        os.fsync(handle.fileno())


def _file_metadata(path: Path) -> dict[str, object]:
    digest = sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return {"bytes": path.stat().st_size, "sha256": digest.hexdigest()}


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
    expected_metadata = {name: _file_identity(expected / name) for name in sorted(_ARTIFACT_NAMES)}
    return _read_direct_regular_files(actual) == expected_metadata


def _read_direct_regular_files(directory: Path) -> dict[str, tuple[int, str]] | None:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    directory_only = getattr(os, "O_DIRECTORY", None)
    if no_follow is None or directory_only is None:
        return None
    try:
        parent_descriptor = os.open(directory.parent, os.O_RDONLY | directory_only)
    except OSError:
        return None
    try:
        return _read_relative_directory(
            parent_descriptor,
            directory.parent,
            directory.name,
            no_follow=no_follow,
            directory_only=directory_only,
        )
    finally:
        os.close(parent_descriptor)


def _read_relative_directory(
    parent_descriptor: int,
    parent: Path,
    name: str,
    *,
    no_follow: int,
    directory_only: int,
) -> dict[str, tuple[int, str]] | None:
    try:
        descriptor = os.open(
            name,
            os.O_RDONLY | directory_only | no_follow,
            dir_fd=parent_descriptor,
        )
    except OSError:
        return None
    try:
        return _read_open_directory(parent_descriptor, parent, name, descriptor, no_follow)
    finally:
        os.close(descriptor)


def _read_open_directory(
    parent_descriptor: int,
    parent: Path,
    name: str,
    descriptor: int,
    no_follow: int,
) -> dict[str, tuple[int, str]] | None:
    directory_state = os.fstat(descriptor)
    parent_state = os.fstat(parent_descriptor)
    try:
        if set(os.listdir(descriptor)) != _ARTIFACT_NAMES:
            return None
        opened = _open_regular_files(descriptor, no_follow)
        if opened is None:
            return None
        try:
            payloads = {
                artifact_name: _read_descriptor(file_descriptor)
                for artifact_name, (file_descriptor, _) in opened.items()
            }
            if not _entries_unchanged(descriptor, opened):
                return None
            if set(os.listdir(descriptor)) != _ARTIFACT_NAMES:
                return None
            if not _same_file_state(
                directory_state,
                os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False),
            ):
                return None
            if not _same_file_identity(parent_state, os.stat(parent)):
                return None
            return payloads
        finally:
            for file_descriptor, _ in opened.values():
                os.close(file_descriptor)
    except OSError:
        return None


def _open_regular_files(
    directory_descriptor: int,
    no_follow: int,
) -> dict[str, tuple[int, os.stat_result]] | None:
    opened: dict[str, tuple[int, os.stat_result]] = {}
    try:
        for name in sorted(_ARTIFACT_NAMES):
            descriptor = os.open(
                name,
                os.O_RDONLY | os.O_NONBLOCK | no_follow,
                dir_fd=directory_descriptor,
            )
            file_state = os.fstat(descriptor)
            if not stat.S_ISREG(file_state.st_mode):
                os.close(descriptor)
                return None
            opened[name] = (descriptor, file_state)
    except OSError:
        return None
    finally:
        if len(opened) != len(_ARTIFACT_NAMES):
            for descriptor, _ in opened.values():
                os.close(descriptor)
    return opened


def _file_identity(path: Path) -> tuple[int, str]:
    digest = sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _read_descriptor(descriptor: int) -> tuple[int, str]:
    digest = sha256()
    size = 0
    while chunk := os.read(descriptor, 1024 * 1024):
        size += len(chunk)
        digest.update(chunk)
    return size, digest.hexdigest()


def _entries_unchanged(
    directory_descriptor: int,
    opened: dict[str, tuple[int, os.stat_result]],
) -> bool:
    for name, (descriptor, initial_state) in opened.items():
        final_state = os.fstat(descriptor)
        linked_state = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
        if not _same_file_state(initial_state, final_state):
            return False
        if not _same_file_state(final_state, linked_state):
            return False
    return True


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino, left.st_mode) == (right.st_dev, right.st_ino, right.st_mode)


def _same_file_state(left: os.stat_result, right: os.stat_result) -> bool:
    return (
        left.st_dev,
        left.st_ino,
        left.st_mode,
        left.st_size,
        left.st_mtime_ns,
        left.st_ctime_ns,
    ) == (
        right.st_dev,
        right.st_ino,
        right.st_mode,
        right.st_size,
        right.st_mtime_ns,
        right.st_ctime_ns,
    )
