"""Thin command-line boundary over typed application and adapter services."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import signal
import sys
from collections.abc import Sequence
from threading import Event

from trading_research.adapters.persistence.sqlite import SQLiteRepository
from trading_research.application.ports.notifications import NotificationEvent
from trading_research.composition.runtime_services import (
    RuntimePaths,
    build_scheduler,
    required_runtime_folder,
    run_backtest,
    run_once,
    sync_historical_range,
)
from trading_research.configuration import (
    load_market_data,
    load_notifications,
    load_schedule,
    load_strategy,
)

_STRUCTURED_LOG_FIELDS = (
    "destination_id",
    "adapter_id",
    "attempted_at_seconds",
    "attempts",
    "outcome",
    "reason_code",
    "status_code",
    "attempt_id",
    "job_id",
    "child_run_id",
    "event_type",
    "run_id",
)
_STRUCTURED_LOG_EVENTS = frozenset(
    {
        "notification_delivery_outcome",
        "scheduler_job_receipt",
        "scheduler_job_overlap_skipped",
        "notification_batch_failed",
        "notification_event_failed",
    }
)


class _StructuredLogFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        event = record.msg if type(record.msg) is str else "application_log"
        payload: dict[str, object] = {
            "event": event if event in _STRUCTURED_LOG_EVENTS else "application_log"
        }
        for field in _STRUCTURED_LOG_FIELDS:
            value = getattr(record, field, None)
            if value is None or type(value) in (bool, int):
                if hasattr(record, field):
                    payload[field] = value
            elif type(value) is str:
                payload[field] = value[:200]
        return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _configure_logging() -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(_StructuredLogFormatter())
    logger = logging.getLogger("trading_research")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trading-research")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--strategy", required=True)
    validate.add_argument("--market-data", required=True)
    validate.add_argument("--schedule")
    validate.add_argument("--notifications")

    database = commands.add_parser("database")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    for name in ("bootstrap", "status"):
        database_commands.add_parser(name)

    market_data = commands.add_parser("market-data")
    market_data_commands = market_data.add_subparsers(dest="market_data_command", required=True)
    sync_range = market_data_commands.add_parser("sync-range")
    sync_range.add_argument("--start", required=True)
    sync_range.add_argument("--end", required=True)

    notifier = commands.add_parser("notifier-test")
    notifier.add_argument("--notifications", required=True)
    notifier.add_argument(
        "--event",
        choices=("run_started", "run_succeeded", "run_failed", "decision_found", "no_decision"),
        required=True,
    )
    notifier.add_argument("--run-id", required=True)
    notifier.add_argument("--strategy-id", required=True)
    notifier.add_argument("--instrument-id")
    notifier.add_argument("--payload", default="{}")

    commands.add_parser("scheduler-health")

    run = commands.add_parser("run")
    run.add_argument("--strategy", required=True)
    run.add_argument("--notifications")
    run.add_argument("--trigger", choices=("manual", "scheduled"), default="manual")

    backtest = commands.add_parser("backtest")
    backtest.add_argument("scenario")

    scheduler = commands.add_parser("scheduler")
    scheduler.add_argument("--schedule", required=True)
    return parser


def _write(payload: object, *, error: bool = False) -> None:
    stream = sys.stderr if error else sys.stdout
    stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n")
    stream.flush()


def _execute(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "validate":
        strategy = load_strategy(args.strategy)
        market = load_market_data(args.market_data)
        if set(strategy.instruments) - market.instruments.keys():
            raise ValueError("strategy instruments are missing from market-data configuration")
        if args.schedule is not None:
            load_schedule(args.schedule)
        if args.notifications is not None:
            load_notifications(args.notifications)
        return {"status": "VALID"}
    if args.command == "database":
        status = SQLiteRepository(RuntimePaths.from_environ().database).database_status()
        if args.database_command == "bootstrap":
            return {
                "status": status["status"],
                "schema_version": status["schema_version"],
                "tables": status["tables"],
            }
        return dict(status)
    if args.command == "market-data":
        return sync_historical_range(start=args.start, end=args.end).canonical_dict()
    if args.command == "notifier-test":
        config = load_notifications(args.notifications)
        payload = json.loads(args.payload)
        if type(payload) is not dict or any(type(key) is not str for key in payload):
            raise ValueError("payload must be a JSON object with string keys")
        if any(
            value is not None and type(value) not in (bool, int, str) for value in payload.values()
        ):
            raise ValueError("payload values must be scalar")
        event = NotificationEvent(
            args.event,
            args.run_id,
            args.instrument_id,
            args.strategy_id,
            1,
            payload,
        )
        canonical_payload = json.dumps(
            dict(event.payload), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return {
            "status": "DRY_RUN",
            "delivery_attempted": False,
            "destinations": sorted(
                destination_id
                for destination_id, destination in config.destinations.items()
                if config.enabled
                and destination.enabled
                and event.event_type in destination.enabled_events
            ),
            "event": event.event_type,
            "payload_sha256": hashlib.sha256(canonical_payload).hexdigest(),
        }
    if args.command == "scheduler-health":
        payload = json.loads(RuntimePaths.from_environ().health.read_text(encoding="utf-8"))
        if payload.get("status") != "READY" or type(payload.get("pid")) is not int:
            raise RuntimeError("scheduler readiness marker is invalid")
        os.kill(payload["pid"], 0)
        return {"status": "READY", "pid": payload["pid"]}
    if args.command == "run":
        paths = RuntimePaths.from_environ()
        config_folder = required_runtime_folder("CONFIG_FOLDER")
        return run_once(
            strategy=args.strategy,
            market_data=config_folder / "market-data.yaml",
            notifications=args.notifications,
            database=paths.database,
            lock_path=paths.lock,
            trigger=args.trigger,
        ).canonical_dict()
    if args.command == "backtest":
        return run_backtest(args.scenario).canonical_dict()
    if args.command == "scheduler":
        paths = RuntimePaths.from_environ()
        service = build_scheduler(
            schedule_path=args.schedule,
            database=paths.database,
            lock_path=paths.lock,
            config_root=required_runtime_folder("CONFIG_FOLDER"),
        )
        stopped = Event()
        previous = {
            signum: signal.signal(signum, lambda _signum, _frame: stopped.set())
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            service.start()
            health = service.health()
            paths.health.write_text(
                json.dumps({"pid": os.getpid(), "status": "READY"}, separators=(",", ":")),
                encoding="utf-8",
            )
            _write(
                {
                    "status": "READY",
                    "timezone": "UTC",
                    "configured_jobs": health.configured_jobs,
                    "recovered_run_ids": list(health.recovered_run_ids),
                }
            )
            stopped.wait()
        finally:
            service.shutdown(wait=True)
            paths.health.unlink(missing_ok=True)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return {"status": "SHUTDOWN"}
    raise AssertionError("unreachable command")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, delegate once, and map bounded failures to exit code 2."""
    _configure_logging()
    try:
        payload = _execute(_parser().parse_args(argv))
    except (OSError, RuntimeError, ValueError) as exc:
        _write({"status": "ERROR", "error": f"{type(exc).__name__}: {exc}"}, error=True)
        return 2
    _write(payload)
    status = payload.get("status")
    if status == "FAILED":
        return 1
    if status == "OVERLAP_SKIPPED":
        return 75
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
