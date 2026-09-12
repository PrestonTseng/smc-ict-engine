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
        ["uv", "run", "trading-research", *args],
        text=True,
        capture_output=True,
        timeout=10,
        check=False,
    )


def test_actual_cli_accepts_implemented_strategy_before_bootstrapping_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_FOLDER", str(tmp_path))
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

    bootstrap = _cli("database", "bootstrap")
    assert bootstrap.returncode == 0, bootstrap.stderr
    assert json.loads(bootstrap.stdout) == {"schema_version": 1, "status": "READY", "tables": 5}

    status = _cli("database", "status")
    assert status.returncode == 0, status.stderr
    assert json.loads(status.stdout)["runs"] == 0


def test_cli_uses_global_runtime_authority_and_sync_range_accepts_only_dates() -> None:
    from trading_research.cli import _parser

    parser = _parser()
    sync = parser.parse_args(
        [
            "market-data",
            "sync-range",
            "--start",
            "2026-01-01T00:00:00Z",
            "--end",
            "2026-01-01T00:01:00Z",
        ]
    )
    assert vars(sync) == {
        "command": "market-data",
        "market_data_command": "sync-range",
        "start": "2026-01-01T00:00:00Z",
        "end": "2026-01-01T00:01:00Z",
    }

    for command in (
        ["database", "status", "--database", "other.sqlite3"],
        ["run", "--strategy", "strategy.yaml", "--database", "other.sqlite3"],
        ["run", "--strategy", "strategy.yaml", "--lock", "other.lock"],
        ["scheduler-health", "--health-file", "other.ready"],
        ["scheduler", "--schedule", "schedule.yaml", "--config-root", "other-config"],
    ):
        with pytest.raises(SystemExit):
            parser.parse_args(command)


def test_sync_range_cli_delegates_to_the_shared_application_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from trading_research import cli

    calls: list[tuple[str, str]] = []

    class Receipt:
        def canonical_dict(self) -> dict[str, object]:
            return {"status": "SUCCEEDED", "provider": "fixture", "instruments": {}}

    monkeypatch.setattr(
        cli,
        "sync_historical_range",
        lambda *, start, end: (calls.append((start, end)), Receipt())[1],
        raising=False,
    )

    payload = cli._execute(
        cli._parser().parse_args(
            [
                "market-data",
                "sync-range",
                "--start",
                "2026-01-01T00:00:00Z",
                "--end",
                "2026-01-01T00:01:00Z",
            ]
        )
    )

    assert payload == {"status": "SUCCEEDED", "provider": "fixture", "instruments": {}}
    assert calls == [("2026-01-01T00:00:00Z", "2026-01-01T00:01:00Z")]


def test_run_requires_the_global_config_folder_before_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research import cli

    monkeypatch.setenv("DATA_FOLDER", str(tmp_path))
    monkeypatch.delenv("CONFIG_FOLDER", raising=False)
    monkeypatch.setattr(
        cli,
        "run_once",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("run composed without authority")),
    )

    with pytest.raises(ValueError, match="CONFIG_FOLDER is required"):
        cli._execute(cli._parser().parse_args(["run", "--strategy", "strategy.yaml"]))


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


def test_cli_structured_logging_emits_only_allowlisted_notification_fields() -> None:
    script = """
import logging
from trading_research.cli import _configure_logging
_configure_logging()
logging.getLogger('trading_research.application.notifications').info(
    'notification_delivery_outcome',
    extra={
        'destination_id': 'discord_debug',
        'adapter_id': 'discord_webhook',
        'attempted_at_seconds': 123,
        'attempts': 1,
        'outcome': 'SUCCESS',
        'reason_code': None,
        'status_code': 204,
        'endpoint': 'FICTIONAL_SECRET',
    },
)
"""

    result = subprocess.run(
        [sys.executable, "-c", script], text=True, capture_output=True, timeout=10, check=False
    )

    assert result.returncode == 0, result.stderr
    assert json.loads(result.stderr) == {
        "adapter_id": "discord_webhook",
        "attempted_at_seconds": 123,
        "attempts": 1,
        "destination_id": "discord_debug",
        "event": "notification_delivery_outcome",
        "outcome": "SUCCESS",
        "reason_code": None,
        "status_code": 204,
    }
    assert "FICTIONAL_SECRET" not in result.stderr


def test_write_makes_a_complete_json_line_visible_while_child_remains_alive() -> None:
    environment = os.environ.copy()
    environment.pop("PYTHONUNBUFFERED", None)
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys, time; "
                "from trading_research.cli import _write; "
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


def test_scheduler_cli_reports_readiness_and_shuts_down_gracefully(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DATA_FOLDER", str(tmp_path))
    monkeypatch.setenv("CONFIG_FOLDER", str(tmp_path))
    schedule = tmp_path / "schedule.yaml"
    schedule.write_text(
        "schedule:\n  enabled: false\n  timezone: UTC\n  jobs: []\n",
        encoding="utf-8",
    )
    process = subprocess.Popen(
        [
            "uv",
            "run",
            "trading-research",
            "scheduler",
            "--schedule",
            str(schedule),
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
    from trading_research.cli import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(
            [
                "scheduler",
                "--schedule",
                "schedule.yaml",
                "--retry-attempts",
                "2",
            ]
        )


def test_one_shot_cli_runner_wires_all_implemented_plugins_without_network(tmp_path: Path) -> None:
    from trading_research.composition.runtime_services import build_engine_runner
    from trading_research.configuration import IMPLEMENTED_PLUGIN_IDS

    runner = build_engine_runner(tmp_path / "runtime.sqlite3", tmp_path / "engine.lock")

    assert tuple(sorted(runner._plugin_factories)) == tuple(sorted(IMPLEMENTED_PLUGIN_IDS))
