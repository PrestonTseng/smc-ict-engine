#!/usr/bin/env python3
"""Generate a deterministic replay-shaped report benchmark without provider access."""

from __future__ import annotations

import argparse
import json
import os
import platform
import resource
import subprocess
import time
import tracemalloc
from hashlib import sha256
from pathlib import Path

from smc_ict.adapters.reporting.jsonl import BacktestReportPublisher
from smc_ict.application.backtesting import BacktestIdentity, RequiredRange
from smc_ict.application.execution_simulator import SimulationResult
from smc_ict.application.metrics import summarize_backtest
from smc_ict.configuration import load_backtest, load_market_data, load_strategy
from smc_ict.domain import Decision
from smc_ict.domain.backtesting import PipelineStep, PipelineTrace, ReplayEvaluation, ReplayResult

DEFAULT_EVALUATIONS = 210_240
PAGE_SIZE = 25
INSTRUMENTS = ("BTC-USDT-PERP", "ETH-USDT-PERP")
BASE_TIME_MS = 1_756_684_799_999


def _hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return sha256(encoded).hexdigest()


def _steps(*, rejected: bool, unavailable: bool) -> tuple[PipelineStep, ...]:
    indicator_state = "UNAVAILABLE" if unavailable else "PASS"
    indicator_reason = "SYNTHETIC_UNAVAILABLE" if unavailable else "SYNTHETIC_PASS"
    indicators = tuple(
        PipelineStep(
            "INDICATOR",
            f"synthetic.signal-{index}",
            indicator_state if index == 0 else "PASS",
            indicator_reason if index == 0 else "SYNTHETIC_PASS",
            (),
            _hash(["indicator", index]),
        )
        for index in range(7)
    )
    gates: list[PipelineStep] = []
    for index in range(7):
        if index == 0 and rejected:
            state = "UNAVAILABLE" if unavailable else "REJECT"
            reason = "REQUIRED_SIGNAL_UNAVAILABLE" if unavailable else "REQUIRED_SIGNAL_FAILED"
        elif rejected:
            state = "SKIPPED_AFTER_REJECTION"
            reason = "SKIPPED_AFTER_REJECTION"
        else:
            state = "PASS"
            reason = "SIGNAL_ACCEPTED"
        gates.append(
            PipelineStep(
                "DECISION_GATE",
                f"synthetic.signal-{index}",
                state,
                reason,
                (),
                None if rejected and index > 0 else _hash(["gate", index]),
            )
        )
    return indicators + tuple(gates)


def _evaluations(count: int) -> tuple[ReplayEvaluation, ...]:
    ready_steps = _steps(rejected=False, unavailable=False)
    rejected_steps = _steps(rejected=True, unavailable=False)
    unavailable_steps = _steps(rejected=True, unavailable=True)
    evaluations: list[ReplayEvaluation] = []
    for index in range(count):
        instrument = INSTRUMENTS[index % len(INSTRUMENTS)]
        evaluation_time = BASE_TIME_MS + (index // len(INSTRUMENTS)) * 300_000
        variant = index % 4
        if variant < 2:
            direction = "LONG" if variant == 0 else "SHORT"
            decision = Decision(instrument, "READY", direction, "100", "99", "102", None, {})
            steps = ready_steps
            first_rejection = None
        else:
            status = "NO_TRADE" if variant == 2 else "UNAVAILABLE"
            decision = Decision(
                instrument,
                status,
                None,
                None,
                None,
                None,
                "synthetic.signal-0",
                {},
            )
            steps = rejected_steps if status == "NO_TRADE" else unavailable_steps
            first_rejection = "synthetic.signal-0"
        decision_hash = _hash([instrument, evaluation_time, variant])
        trace = PipelineTrace(
            instrument,
            evaluation_time,
            steps,
            first_rejection,
            decision_hash,
        )
        evaluations.append(
            ReplayEvaluation(instrument, evaluation_time, decision, decision_hash, trace)
        )
    return tuple(evaluations)


def _max_rss_bytes() -> int:
    # Linux reports KiB; macOS reports bytes.
    value = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return value if platform.system() == "Darwin" else value * 1024


def _git_commit() -> str:
    configured = os.environ.get("SMC_ICT_GIT_COMMIT")
    if configured:
        return configured
    return subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()


def run(*, count: int, output_root: Path) -> dict[str, object]:
    if count < 1 or count > DEFAULT_EVALUATIONS:
        raise ValueError(f"evaluations must be between 1 and {DEFAULT_EVALUATIONS}")
    scenario = load_backtest("backtests/source-aligned-research/one-year-baseline.yaml")
    strategy = load_strategy("strategies/source-aligned-research.yaml")
    market_data = load_market_data("config/market-data.yaml")

    fixture_started = time.perf_counter()
    tracemalloc.start()
    evaluations = _evaluations(count)
    fixture_current, fixture_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    fixture_seconds = time.perf_counter() - fixture_started
    fixture_max_rss = _max_rss_bytes()

    replay = ReplayResult(evaluations, 0, _hash(["synthetic-report-benchmark", count]))
    metrics = summarize_backtest(evaluations, ())
    required_range = RequiredRange(
        scenario.period.start_ms - strategy.history_minutes * 60_000,
        scenario.period.end_ms,
    )
    identity = BacktestIdentity.create(
        scenario_hash=_hash(scenario.canonical_dict()),
        strategy_hash=_hash(strategy.canonical_dict()),
        market_data_hash=_hash(market_data.canonical_dict()),
        candle_data_hash=replay.data_hash,
        git_commit=_git_commit(),
        period=scenario.period,
        required_range=required_range,
    )

    publication_started = time.perf_counter()
    tracemalloc.start()
    receipt = BacktestReportPublisher(output_root).publish(
        identity=identity,
        scenario=scenario,
        strategy=strategy,
        market_data=market_data,
        replay=replay,
        simulation=SimulationResult(()),
        metrics=metrics,
    )
    publication_current, publication_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    publication_seconds = time.perf_counter() - publication_started

    files = {
        path.name: path.stat().st_size
        for path in sorted(receipt.path.iterdir(), key=lambda item: item.name)
    }
    return {
        "schema_version": 1,
        "benchmark_kind": "deterministic synthetic replay-shaped chunked report",
        "evaluation_count": count,
        "instrument_count": len(INSTRUMENTS),
        "execution_interval_minutes": 5,
        "ordered_trace_steps": 14,
        "backtest_id": receipt.backtest_id,
        "git_commit": identity.git_commit,
        "publication_status": receipt.status,
        "result_path": str(receipt.path.resolve()),
        "timings_seconds": {
            "synthetic_fixture_generation": fixture_seconds,
            "report_publication": publication_seconds,
            "total": fixture_seconds + publication_seconds,
        },
        "memory": {
            "fixture_tracemalloc_current_bytes": fixture_current,
            "fixture_tracemalloc_peak_bytes": fixture_peak,
            "fixture_process_max_rss_bytes": fixture_max_rss,
            "publication_tracemalloc_current_bytes": publication_current,
            "publication_tracemalloc_peak_bytes": publication_peak,
            "process_max_rss_bytes": _max_rss_bytes(),
        },
        "artifacts": {
            "count": len(files),
            "files": files,
            "total_bytes": sum(files.values()),
        },
        "report": {"page_size": PAGE_SIZE, "html_bytes": files["report.html"]},
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
        "sla": None,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evaluations", type=int, default=DEFAULT_EVALUATIONS)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument("--result-json", type=Path, required=True)
    args = parser.parse_args()
    result = run(count=args.evaluations, output_root=args.output_root)
    args.result_json.parent.mkdir(parents=True, exist_ok=True)
    args.result_json.write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
