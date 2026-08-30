from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
from pathlib import Path

import pytest


def _cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["uv", "run", "smc-ict", *args],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_actual_cli_accepts_implemented_strategy_before_bootstrapping_database(
    tmp_path: Path,
) -> None:
    validated = _cli(
        "validate",
        "--strategy",
        "strategies/source-aligned-research.yaml",
        "--market-data",
        "config/market-data.yaml",
        "--schedule",
        "config/schedule.yaml",
    )
    assert validated.returncode == 0, validated.stderr
    assert json.loads(validated.stdout) == {"status": "VALID"}

    database = tmp_path / "runtime.sqlite3"
    bootstrap = _cli("database", "bootstrap", "--database", str(database))
    assert bootstrap.returncode == 0, bootstrap.stderr
    assert json.loads(bootstrap.stdout) == {"schema_version": 1, "status": "READY", "tables": 5}

    status = _cli("database", "status", "--database", str(database))
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["runs"] == 0


def test_notifier_test_is_a_redacted_dry_run_without_delivery(tmp_path: Path) -> None:
    notifications = tmp_path / "notifications.yaml"
    notifications.write_text(
        "notifications:\n  enabled: false\n  destinations: {}\n", encoding="utf-8"
    )

    result = _cli(
        "notifier-test",
        "--notifications",
        str(notifications),
        "--event",
        "run_succeeded",
        "--run-id",
        "fixture-run",
        "--strategy-id",
        "fixture-strategy",
        "--payload",
        '{"decision_count":1}',
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {
        "delivery_attempted": False,
        "destinations": [],
        "event": "run_succeeded",
        "payload_sha256": "acba77cc59af63113d4c757f3afa639379707d4ccefab2b625dad3b33aaa9906",
        "status": "DRY_RUN",
    }


def test_notifier_test_rejects_a_nested_payload_before_secret_resolution(tmp_path: Path) -> None:
    notifications = tmp_path / "notifications.yaml"
    notifications.write_text(
        "notifications:\n  enabled: false\n  destinations: {}\n", encoding="utf-8"
    )

    result = _cli(
        "notifier-test",
        "--notifications",
        str(notifications),
        "--event",
        "run_failed",
        "--run-id",
        "fixture-run",
        "--strategy-id",
        "fixture-strategy",
        "--payload",
        '{"nested":{"secret":"must-not-pass"}}',
    )

    assert result.returncode == 2
    assert "payload values must be scalar" in result.stderr


def test_write_makes_a_complete_json_line_visible_while_child_remains_alive() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONUNBUFFERED", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time; "
                "from smc_ict.cli import _write; "
                "_write({'status': 'READY'}); "
                "sys.stderr.write('WRITE_RETURNED\\n'); "
                "sys.stderr.flush(); "
                "time.sleep(30)"
            ),
        ],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        assert process.stdout is not None
        assert process.stderr is not None
        called, _, _ = select.select([process.stderr], [], [], 5)
        assert called, "child did not return from _write"
        assert process.stderr.readline() == "WRITE_RETURNED\n"
        assert process.poll() is None

        readable, _, _ = select.select([process.stdout], [], [], 1)
        assert readable, "complete JSON line was not visible while child remained alive"
        assert process.stdout.readline() == '{"status":"READY"}\n'
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


def test_scheduler_cli_reports_readiness_and_shuts_down_gracefully(tmp_path: Path) -> None:
    schedule = tmp_path / "schedule.yaml"
    schedule.write_text(
        "schedule:\n  enabled: false\n  timezone: UTC\n  jobs: []\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "smc-ict",
            "scheduler",
            "--schedule",
            str(schedule),
            "--database",
            str(tmp_path / "runtime.sqlite3"),
            "--lock",
            str(tmp_path / "engine.lock"),
        ],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    readable, _, _ = select.select([process.stdout], [], [], 5)
    assert readable, "scheduler did not report readiness"
    ready = json.loads(process.stdout.readline())
    assert ready == {
        "configured_jobs": 0,
        "recovered_run_ids": [],
        "status": "READY",
        "timezone": "UTC",
    }

    process.send_signal(signal.SIGTERM)
    stdout, stderr = process.communicate(timeout=5)
    assert process.returncode == 0, stderr
    assert json.loads(stdout)["status"] == "SHUTDOWN"


def test_scheduler_cli_has_no_complete_job_retry_policy() -> None:
    from smc_ict.cli import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "scheduler",
                "--schedule",
                "schedule.yaml",
                "--database",
                "db.sqlite3",
                "--lock",
                "engine.lock",
                "--retry-attempts",
                "2",
            ]
        )


def test_one_shot_cli_runner_wires_all_implemented_plugins_without_network(tmp_path: Path) -> None:
    from smc_ict.composition.runtime_services import build_engine_runner
    from smc_ict.configuration import IMPLEMENTED_PLUGIN_IDS

    runner = build_engine_runner(tmp_path / "runtime.sqlite3", tmp_path / "engine.lock")

    assert tuple(sorted(runner._plugin_factories)) == tuple(sorted(IMPLEMENTED_PLUGIN_IDS))
