from __future__ import annotations

import base64
import hashlib
import json
import os
import zlib
from decimal import ROUND_DOWN, ROUND_UP, localcontext
from pathlib import Path
from typing import Any

import pytest

from smc_ict.application.backtesting import BacktestIdentity, RequiredRange
from smc_ict.application.execution_simulator import (
    ExecutionSimulator,
    SimulationResult,
    TradeRecord,
)
from smc_ict.application.metrics import summarize_backtest
from smc_ict.configuration import load_backtest_text, load_market_data_text, load_strategy
from smc_ict.configuration.models import BacktestPeriod
from smc_ict.domain import ClosedCandle, Decision
from smc_ict.domain.backtesting import PipelineStep, PipelineTrace, ReplayEvaluation, ReplayResult

SCENARIO = """\
backtest:
  name: fixture
  version: "1"
  strategy: fixture.yaml
  period:
    start: "2026-01-01T00:00:00Z"
    end: "2026-01-01T00:01:00Z"
  entry:
    mode: touch_limit
    expiry_execution_bars: 1
  execution:
    maximum_holding_minutes: 5
    intrabar_conflict: stop_first
    allow_same_minute_target: false
  costs:
    taker_fee_bps: "5"
    adverse_slippage_bps: "2"
  output:
    existing_result: fail
"""

MARKET = """\
market_data:
  provider: okx_swap
  market_type: LINEAR_PERPETUAL
  instruments:
    BTC-USDT-PERP: BTC-USDT-SWAP
"""


def _evidence():
    decision = Decision("BTC-USDT-PERP", "READY", "LONG", "100", "99", "102", None, {})
    decision_hash = "a" * 64
    step = PipelineStep("INDICATOR", "fixture.signal", "PASS", "FIXTURE_PASS", (), "b" * 64)
    gate = PipelineStep(
        "DECISION_GATE",
        "fixture.signal",
        "PASS",
        "SIGNAL_ACCEPTED",
        (("fixture.signal", "b" * 64),),
        "b" * 64,
    )
    trace = PipelineTrace("BTC-USDT-PERP", 1_767_225_659_999, (step, gate), None, decision_hash)
    evaluation = ReplayEvaluation(
        "BTC-USDT-PERP", 1_767_225_659_999, decision, decision_hash, trace
    )
    trade = TradeRecord(
        decision_hash=decision_hash,
        instrument_id="BTC-USDT-PERP",
        direction="LONG",
        status="CLOSED",
        signal_time_ms=evaluation.evaluation_time_ms,
        entry_time_ms=evaluation.evaluation_time_ms + 1,
        exit_time_ms=evaluation.evaluation_time_ms + 60_001,
        requested_entry="100",
        stop="99",
        target="102",
        executed_entry="100.02",
        requested_exit="102",
        executed_exit="101.9796",
        exit_reason="TARGET",
        initial_risk="1",
        gross_price_return="0.02",
        entry_fee="0.05001",
        exit_fee="0.0509898",
        slippage_cost="0.0404",
        net_price_return="0.018586002",
        gross_r="2",
        net_r="1.8586002",
        duration_minutes=2,
    )
    replay = ReplayResult((evaluation,), 6, "d" * 64)
    simulation = SimulationResult((trade,))
    metrics = summarize_backtest(replay.evaluations, simulation.trades)
    identity = BacktestIdentity.create(
        scenario_hash="1" * 64,
        strategy_hash="2" * 64,
        market_data_hash="3" * 64,
        candle_data_hash=replay.data_hash,
        git_commit="4" * 40,
        period=BacktestPeriod(1_767_225_600_000, 1_767_225_660_000),
        required_range=RequiredRange(1_767_225_300_000, 1_767_225_660_000),
    )
    return identity, replay, simulation, metrics


def _json_lines(path: Path) -> list[dict[str, object]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _html_evidence(path: Path) -> dict[str, object]:
    html = path.read_text(encoding="utf-8")
    marker = '<script id="backtest-evidence" type="application/json" nonce="backtest-report">'
    payload = html.split(marker, 1)[1].split("</script>", 1)[0]
    store = json.loads(payload)
    assert isinstance(store, dict)

    def decode(encoded: str) -> Any:
        return json.loads(zlib.decompress(base64.b64decode(encoded), wbits=31))

    evaluations = [item for encoded in store["evaluation_chunks"] for item in decode(encoded)]
    return {
        "manifest": decode(store["manifest"]),
        "summary": decode(store["summary"]),
        "decisions": [item["decision"] for item in evaluations],
        "traces": [item["trace"] for item in evaluations],
        "trades": [trade for encoded in store["trade_chunks"] for trade in decode(encoded)],
    }


def _publish(root: Path):
    from smc_ict.adapters.reporting.jsonl import BacktestReportPublisher

    identity, replay, simulation, metrics = _evidence()
    receipt = BacktestReportPublisher(root).publish(
        identity=identity,
        scenario=load_backtest_text(SCENARIO),
        strategy=load_strategy(
            Path(__file__).parents[1] / "strategies/source-aligned-research.yaml"
        ),
        market_data=load_market_data_text(MARKET),
        replay=replay,
        simulation=simulation,
        metrics=metrics,
    )
    return identity, receipt


def _ambient_context_report(root: Path, *, precision: int, rounding: str) -> dict[str, object]:
    identity, base_replay, _, _ = _evidence()
    scenario = load_backtest_text(SCENARIO)
    decision = Decision(
        "BTC-USDT-PERP",
        "READY",
        "LONG",
        "123456789.123456789",
        "123000000",
        "124000000",
        None,
        {},
    )
    evaluation = ReplayEvaluation(
        "BTC-USDT-PERP",
        base_replay.evaluations[0].evaluation_time_ms,
        decision,
        base_replay.evaluations[0].decision_hash,
        base_replay.evaluations[0].trace,
    )
    candles = tuple(
        ClosedCandle(
            provider_id="okx_swap",
            market_type="LINEAR_PERPETUAL",
            instrument_id="BTC-USDT-PERP",
            provider_symbol="BTC-USDT-SWAP",
            interval="1m",
            open_time_ms=open_time_ms,
            close_time_ms=open_time_ms + 59_999,
            open=opening,
            high=high,
            low=low,
            close=close,
            base_volume="1",
            quote_volume="1",
            source_fields={"contract_volume": "1"},
        )
        for open_time_ms, opening, high, low, close in (
            (1_767_225_600_000, "123456789", "123456789", "123456789", "123456789"),
            (1_767_225_660_000, "123456789", "123500000", "123400000", "123450000"),
            (1_767_225_720_000, "124000000", "124000000", "123900000", "124000000"),
        )
    )
    replay = ReplayResult((evaluation,), base_replay.candle_count, base_replay.data_hash)
    with localcontext() as ambient:
        ambient.prec = precision
        ambient.rounding = rounding
        simulation = ExecutionSimulator(
            entry=scenario.entry,
            execution=scenario.execution,
            costs=scenario.costs,
            execution_bar_minutes=1,
        ).run(replay.evaluations, candles)
        metrics = summarize_backtest(replay.evaluations, simulation.trades)
        from smc_ict.adapters.reporting.jsonl import BacktestReportPublisher

        receipt = BacktestReportPublisher(root).publish(
            identity=identity,
            scenario=scenario,
            strategy=load_strategy(
                Path(__file__).parents[1] / "strategies/source-aligned-research.yaml"
            ),
            market_data=load_market_data_text(MARKET),
            replay=replay,
            simulation=simulation,
            metrics=metrics,
        )
    return {
        "backtest_id": receipt.backtest_id,
        "trades": simulation.trades,
        "summary": metrics.canonical_dict(),
        "artifacts": {path.name: path.read_bytes() for path in receipt.path.iterdir()},
    }


def test_report_publication_emits_consistent_canonical_artifacts(tmp_path: Path) -> None:
    identity, replay, simulation, metrics = _evidence()
    _, receipt = _publish(tmp_path)

    result = tmp_path / identity.backtest_id
    names = {path.name for path in result.iterdir()}
    assert names == {
        "manifest.json",
        "decisions.jsonl",
        "pipeline-traces.jsonl",
        "trades.jsonl",
        "summary.json",
        "report.html",
    }
    assert receipt.status == "SUCCEEDED"
    assert receipt.path == result
    decisions = _json_lines(result / "decisions.jsonl")
    traces = _json_lines(result / "pipeline-traces.jsonl")
    trades = _json_lines(result / "trades.jsonl")
    assert decisions[0]["decision_hash"] == traces[0]["decision_hash"] == trades[0]["decision_hash"]
    assert traces[0] == replay.evaluations[0].trace.canonical_dict()

    summary = json.loads((result / "summary.json").read_bytes())
    assert summary["overall"]["trade_count"] == 1
    assert summary["decision_status_counts"] == [["READY", 1]]

    manifest = json.loads((result / "manifest.json").read_bytes())
    assert manifest["backtest_id"] == identity.backtest_id
    assert manifest["identity"]["git_commit"] == "4" * 40
    assert manifest["assumptions"]["research_only"] is True
    assert manifest["assumptions"]["position_sizing"] is False
    assert set(manifest["artifacts"]) == names - {"manifest.json"}
    for name, metadata in manifest["artifacts"].items():
        payload = (result / name).read_bytes()
        assert metadata == {
            "bytes": len(payload),
            "sha256": hashlib.sha256(payload).hexdigest(),
        }


def test_trade_summary_id_and_artifacts_ignore_hostile_ambient_decimal_context(
    tmp_path: Path,
) -> None:
    low_precision = _ambient_context_report(tmp_path / "low", precision=6, rounding=ROUND_DOWN)
    high_precision = _ambient_context_report(tmp_path / "high", precision=28, rounding=ROUND_UP)

    trades = high_precision["trades"]
    assert isinstance(trades, tuple)
    assert trades[0].executed_entry == "123481480.4812814803578"
    assert low_precision["trades"] == high_precision["trades"]
    assert low_precision["summary"] == high_precision["summary"]
    assert low_precision["backtest_id"] == high_precision["backtest_id"]
    assert low_precision["artifacts"] == high_precision["artifacts"]


def test_byte_identical_rerun_reuses_result_and_tampering_fails_closed(tmp_path: Path) -> None:
    identity, first = _publish(tmp_path)
    result = first.path
    before = {path.name: path.read_bytes() for path in result.iterdir()}

    _, second = _publish(tmp_path)

    assert second.status == "EXISTING_IDENTICAL"
    assert second.reused_existing is True
    assert {path.name: path.read_bytes() for path in result.iterdir()} == before

    (result / "summary.json").write_bytes(b"{}\n")
    with pytest.raises(FileExistsError, match="differs"):
        _publish(tmp_path)
    assert (result / "summary.json").read_bytes() == b"{}\n"
    assert not tuple(tmp_path.glob(f".{identity.backtest_id}.*"))


def test_identical_result_comparison_streams_artifacts_without_read_bytes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _publish(tmp_path)

    monkeypatch.setattr(
        Path,
        "read_bytes",
        lambda _path: (_ for _ in ()).throw(AssertionError("whole artifact read")),
    )

    _, receipt = _publish(tmp_path)

    assert receipt.status == "EXISTING_IDENTICAL"


def test_report_failure_leaves_no_partial_result_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from smc_ict.adapters.reporting import jsonl

    monkeypatch.setattr(
        jsonl.html_reporting,
        "write_report",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("render failed")),
    )

    with pytest.raises(RuntimeError, match="render failed"):
        _publish(tmp_path)

    assert not tuple(tmp_path.iterdir())


def test_report_publication_streams_html_instead_of_rendering_one_bytes_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from smc_ict.adapters.reporting import jsonl

    monkeypatch.setattr(
        jsonl,
        "render_report",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("in-memory renderer called")),
    )

    _, receipt = _publish(tmp_path)

    assert (receipt.path / "report.html").read_bytes().startswith(b"<!doctype html>")


def test_existing_result_symlink_is_never_trusted(tmp_path: Path) -> None:
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _, first = _publish(source)
    destination.mkdir()
    (destination / first.backtest_id).symlink_to(first.path, target_is_directory=True)

    with pytest.raises(FileExistsError, match="differs"):
        _publish(destination)


def test_existing_result_with_six_external_artifact_symlinks_is_rejected(
    tmp_path: Path,
) -> None:
    external = tmp_path / "external"
    destination = tmp_path / "destination"
    _, external_receipt = _publish(external)
    result = destination / external_receipt.backtest_id
    result.mkdir(parents=True)
    for artifact in external_receipt.path.iterdir():
        (result / artifact.name).symlink_to(artifact)

    with pytest.raises(FileExistsError, match="differs"):
        _publish(destination)

    replacement = b'{"externally_mutated":true}\n'
    (external_receipt.path / "summary.json").write_bytes(replacement)
    assert (result / "summary.json").read_bytes() == replacement
    assert len(tuple(path for path in result.iterdir() if path.is_symlink())) == 6


@pytest.mark.parametrize("entry_kind", ("regular", "directory", "dangling_symlink", "fifo"))
def test_existing_result_rejects_unexpected_entries_of_every_file_type(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    _, receipt = _publish(tmp_path)
    unexpected = receipt.path / "unexpected"
    if entry_kind == "regular":
        unexpected.write_bytes(b"unexpected")
    elif entry_kind == "directory":
        unexpected.mkdir()
    elif entry_kind == "dangling_symlink":
        unexpected.symlink_to(tmp_path / "outside" / "missing")
    else:
        os.mkfifo(unexpected)

    with pytest.raises(FileExistsError, match="differs"):
        _publish(tmp_path)


@pytest.mark.parametrize("entry_kind", ("directory", "dangling_symlink", "fifo"))
def test_existing_result_rejects_nonregular_expected_artifacts(
    tmp_path: Path,
    entry_kind: str,
) -> None:
    _, receipt = _publish(tmp_path)
    artifact = receipt.path / "summary.json"
    artifact.unlink()
    if entry_kind == "directory":
        artifact.mkdir()
    elif entry_kind == "dangling_symlink":
        artifact.symlink_to(tmp_path / "outside" / "missing")
    else:
        os.mkfifo(artifact)

    with pytest.raises(FileExistsError, match="differs"):
        _publish(tmp_path)


def test_html_is_offline_and_exposes_complete_trace_filters(tmp_path: Path) -> None:
    identity, receipt = _publish(tmp_path)

    html = (receipt.path / "report.html").read_text(encoding="utf-8")
    trace = _json_lines(receipt.path / "pipeline-traces.jsonl")[0]

    assert identity.backtest_id in html
    assert "http://" not in html and "https://" not in html
    assert "<script src=" not in html and '<link rel="stylesheet"' not in html
    for control in (
        'id="instrument-filter"',
        'id="direction-filter"',
        'id="evaluation-filter"',
        'id="status-filter"',
        'id="failed-step-filter"',
        'id="reason-filter"',
    ):
        assert control in html
    steps = trace["steps"]
    assert isinstance(steps, list)
    embedded_trace = _html_evidence(receipt.path / "report.html")["traces"][0]
    for step in steps:
        assert isinstance(step, dict)
        assert step in embedded_trace["steps"]
    assert "first rejection" in html


def test_html_projects_every_canonical_artifact_into_complete_report_views(tmp_path: Path) -> None:
    _, receipt = _publish(tmp_path)
    result = receipt.path

    html = (result / "report.html").read_text(encoding="utf-8")
    evidence = _html_evidence(result / "report.html")
    manifest = json.loads((result / "manifest.json").read_bytes())

    assert evidence == {
        "manifest": {key: value for key, value in manifest.items() if key != "artifacts"},
        "summary": json.loads((result / "summary.json").read_bytes()),
        "decisions": _json_lines(result / "decisions.jsonl"),
        "traces": _json_lines(result / "pipeline-traces.jsonl"),
        "trades": _json_lines(result / "trades.jsonl"),
    }
    for section in (
        'id="overall-summary"',
        'id="instrument-summary"',
        'id="direction-summary"',
        'id="trade-outcomes"',
        'id="decision-outcomes"',
        'id="pipeline-explorer"',
    ):
        assert section in html


def test_html_escapes_untrusted_evidence_and_enforces_an_offline_csp(tmp_path: Path) -> None:
    from smc_ict.adapters.reporting.html import render_report

    attack = '</script><script src="https://attacker.invalid/payload.js">alert(1)</script>&'
    report = tmp_path / "report.html"
    report.write_bytes(
        render_report(
            manifest={"backtest_id": attack},
            summary={"overall": {}, "by_instrument": [], "by_direction": []},
            decisions=[{"decision": {"status": "NO_TRADE", "reason": attack}}],
            traces=[{"instrument_id": attack, "steps": [{"reason": attack}]}],
            trades=[{"exit_reason": attack}],
        )
    )

    html = report.read_text(encoding="utf-8")
    assert attack not in html
    assert "default-src 'none'" in html
    assert "connect-src 'none'" in html
    assert "script-src-attr 'none'" in html
    assert "script-src 'nonce-backtest-report'" in html
    assert '<script src="' not in html
    assert _html_evidence(report)["manifest"] == {"backtest_id": attack}


def test_html_bounds_trace_dom_and_exposes_filter_and_page_boundaries(tmp_path: Path) -> None:
    from smc_ict.adapters.reporting.html import render_report

    count = 53
    decisions: list[dict[str, object]] = [
        {"decision": {"status": "NO_TRADE" if index % 2 else "READY"}} for index in range(count)
    ]
    traces: list[dict[str, object]] = [
        {
            "instrument_id": f"instrument-{index % 2}",
            "evaluation_time_ms": index,
            "first_rejection": None if index % 2 else "gate",
            "steps": [
                {
                    "kind": "DECISION_GATE",
                    "step_id": f"step-{index}",
                    "state": "PASS" if index % 2 else "REJECT",
                    "reason": f"reason-{index}",
                }
            ],
        }
        for index in range(count)
    ]
    report = tmp_path / "report.html"
    report.write_bytes(
        render_report(
            manifest={"backtest_id": "bounded"},
            summary={
                "overall": {"status_counts": [], "exit_reason_counts": []},
                "by_instrument": [],
                "by_direction": [],
                "decision_status_counts": [["NO_TRADE", 26], ["READY", 27]],
                "unavailable_reason_counts": [],
            },
            decisions=decisions,
            traces=traces,
            trades=[],
        )
    )

    html = report.read_text(encoding="utf-8")
    initial_markup = html.split('<script id="backtest-evidence"', 1)[0]
    assert "<details" not in initial_markup
    embedded_traces = _html_evidence(report)["traces"]
    assert isinstance(embedded_traces, list)
    assert len(embedded_traces) == count
    assert "const PAGE_SIZE=25" in html
    assert "index<Math.min(start+PAGE_SIZE,count)" in html
    assert "Math.max(0,Math.min(pages[kind],pageCount(count)-1))" in html
    assert "Array.from({length:count}" not in html
    assert "Array.from({length:store.evaluation_count}" not in html
    assert "pageRange('trade',count)" in html
    assert "pageRange('decision',store.evaluation_count)" in html
    assert "const matches=[]" not in html
    assert "matchingCount()" not in html
    assert "matchingPage(count)" not in html
    assert "Promise.all(store.evaluation_index_chunks.map(gunzip))" not in html
    assert "evaluationIndexChunks" not in html
    assert "async function matchingPageAndCount()" in html
    assert "for(const encoded of store.evaluation_index_chunks)" in html
    assert "if(!hasFilters())" in html
    for control in (
        'id="trace-first"',
        'id="trace-prev"',
        'id="trace-page-input"',
        'id="trace-go"',
        'id="trace-next"',
        'id="trace-last"',
    ):
        assert control in html
    assert "pages.trace=0;void renderTraces()" in html
    assert "pages.trace=pageCount(traceMatchCount)-1" in html
    assert "pages.trace=Number(document.getElementById('trace-page-input').value)-1" in html


def test_html_uses_deterministic_compressed_chunks_with_at_most_25_evaluations(
    tmp_path: Path,
) -> None:
    from smc_ict.adapters.reporting.html import render_report

    count = 53
    decisions = [
        {
            "instrument_id": f"instrument-{index % 2}",
            "evaluation_time_ms": index,
            "decision": {
                "status": "NO_TRADE" if index % 2 else "READY",
                "direction": "SHORT" if index % 2 else "LONG",
            },
        }
        for index in range(count)
    ]
    traces = [
        {
            "instrument_id": f"instrument-{index % 2}",
            "evaluation_time_ms": index,
            "first_rejection": "gate" if index % 2 else None,
            "steps": [{"reason": f"reason-{index}"}],
        }
        for index in range(count)
    ]
    kwargs = {
        "manifest": {"backtest_id": "chunked"},
        "summary": {
            "overall": {"status_counts": [], "exit_reason_counts": []},
            "by_instrument": [],
            "by_direction": [],
            "decision_status_counts": [],
            "unavailable_reason_counts": [],
        },
        "decisions": decisions,
        "traces": traces,
        "trades": [],
    }

    first = render_report(**kwargs)
    second = render_report(**kwargs)

    assert first == second
    marker = b'<script id="backtest-evidence" type="application/json" nonce="backtest-report">'
    payload = json.loads(first.split(marker, 1)[1].split(b"</script>", 1)[0])
    assert payload["schema_version"] == 2
    assert payload["compression"] == "gzip"
    assert len(payload["evaluation_chunks"]) == 3
    decoded_chunks = [
        json.loads(zlib.decompress(base64.b64decode(chunk), wbits=31))
        for chunk in payload["evaluation_chunks"]
    ]
    assert [len(chunk) for chunk in decoded_chunks] == [25, 25, 3]
    assert [item["decision"] for chunk in decoded_chunks for item in chunk] == decisions
    assert [item["trace"] for chunk in decoded_chunks for item in chunk] == traces
    assert "DecompressionStream" in first.decode("utf-8")
    assert "This browser cannot decompress the embedded backtest evidence" in first.decode("utf-8")
