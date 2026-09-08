from __future__ import annotations

import json
import os
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


def test_documented_report_probe_uses_the_locked_playwright_client() -> None:
    root = Path(__file__).parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    probe = (root / "scripts/probe_backtest_report.py").read_text(encoding="utf-8")
    project = (root / "pyproject.toml").read_text(encoding="utf-8")
    ci = (root / ".github/workflows/ci.yml").read_text(encoding="utf-8")

    assert '"playwright==1.62.0"' in project
    assert "uvx" not in readme
    assert "uvx" not in probe
    assert "uv run --locked playwright install chromium" in readme
    assert "UV_NO_NETWORK=1 uv run --locked python scripts/probe_backtest_report.py" in readme
    assert "uv run --locked playwright install --with-deps chromium" in readme
    assert "uv run playwright install --with-deps chromium" in ci
    assert "uv run --locked python scripts/probe_backtest_report.py ..." in probe


def test_browser_probe_bounds_sparse_filter_evaluation_decoding(tmp_path: Path) -> None:
    result_path = tmp_path / "benchmark.json"
    generated = subprocess.run(
        [
            sys.executable,
            "scripts/benchmark_backtest_report.py",
            "--evaluations",
            "103",
            "--output-root",
            str(tmp_path / "reports"),
            "--result-json",
            str(result_path),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert generated.returncode == 0, generated.stderr
    benchmark = json.loads(result_path.read_text(encoding="utf-8"))
    browser_path = tmp_path / "browser.json"
    env = {**os.environ, "UV_NO_NETWORK": "1"}

    probed = subprocess.run(
        [
            "uv",
            "run",
            "--locked",
            "python",
            "scripts/probe_backtest_report.py",
            str(Path(benchmark["result_path"]) / "report.html"),
            "--result-json",
            str(browser_path),
        ],
        check=False,
        capture_output=True,
        text=True,
        env=env,
    )

    assert probed.returncode == 0, probed.stderr
    browser = json.loads(browser_path.read_text(encoding="utf-8"))
    assert browser["payload_node_count"] == 1
    assert max(browser["rendered_evaluation_counts"].values()) <= 25
    assert max(browser["decoded_evaluation_counts"].values()) <= 25
    assert 0 < browser["decoded_evaluation_counts"]["filter_instrument"] <= 25
    assert 0 < browser["decoded_evaluation_counts"]["filter_reason"] <= 25
