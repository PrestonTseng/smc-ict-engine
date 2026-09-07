from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path


def test_report_benchmark_tool_generates_a_bounded_fixture(tmp_path: Path) -> None:
    result_path = tmp_path / "benchmark.json"
    completed = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_backtest_report.py",
            "--evaluations",
            "8",
            "--output-root",
            str(tmp_path / "reports"),
            "--result-json",
            str(result_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(result_path.read_text(encoding="utf-8"))
    assert result["evaluation_count"] == 8
    assert result["ordered_trace_steps"] == 14
    assert result["artifacts"]["count"] == 6
    assert result["report"]["page_size"] == 25
    assert result["sla"] is None


def test_browser_probe_exposes_a_dependency_free_help_path() -> None:
    completed = subprocess.run(
        [sys.executable, "scripts/probe_backtest_report.py", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    assert "report.html" in completed.stdout
    assert "--chromium-executable" in completed.stdout
