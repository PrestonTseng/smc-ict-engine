"""Thin command-line boundary over typed application and adapter services."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from threading import Event

from smc_ict.adapters.persistence.sqlite import SQLiteRepository
from smc_ict.application.ports.notifications import NotificationEvent
from smc_ict.composition.runtime_services import build_scheduler, run_once
from smc_ict.configuration import (
    load_market_data,
    load_notifications,
    load_schedule,
    load_strategy,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="smc-ict")
    commands = parser.add_subparsers(dest="command", required=True)

    validate = commands.add_parser("validate")
    validate.add_argument("--strategy", required=True)
    validate.add_argument("--market-data", required=True)
    validate.add_argument("--schedule")
    validate.add_argument("--notifications")

    database = commands.add_parser("database")
    database_commands = database.add_subparsers(dest="database_command", required=True)
    for name in ("bootstrap", "status"):
        child = database_commands.add_parser(name)
        child.add_argument("--database", required=True)

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

    scheduler_health = commands.add_parser("scheduler-health")
    scheduler_health.add_argument("--health-file", required=True)

    run = commands.add_parser("run")
    run.add_argument("--strategy", required=True)
    run.add_argument("--market-data", required=True)
    run.add_argument("--notifications")
    run.add_argument("--database", required=True)
    run.add_argument("--lock", required=True)
    run.add_argument("--trigger", choices=("manual", "scheduled"), default="manual")

    scheduler = commands.add_parser("scheduler")
    scheduler.add_argument("--schedule", required=True)
    scheduler.add_argument("--database", required=True)
    scheduler.add_argument("--lock", required=True)
    scheduler.add_argument("--config-root", default="/config")
    scheduler.add_argument("--health-file")
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
        status = SQLiteRepository(args.database).database_status()
        if args.database_command == "bootstrap":
            return {
                "status": status["status"],
                "schema_version": status["schema_version"],
                "tables": status["tables"],
            }
        return dict(status)
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
        payload = json.loads(Path(args.health_file).read_text(encoding="utf-8"))
        if payload.get("status") != "READY" or type(payload.get("pid")) is not int:
            raise RuntimeError("scheduler readiness marker is invalid")
        os.kill(payload["pid"], 0)
        return {"status": "READY", "pid": payload["pid"]}
    if args.command == "run":
        return run_once(
            strategy=args.strategy,
            market_data=args.market_data,
            notifications=args.notifications,
            database=args.database,
            lock_path=args.lock,
            trigger=args.trigger,
        ).canonical_dict()
    if args.command == "scheduler":
        service = build_scheduler(
            schedule_path=args.schedule,
            database=args.database,
            lock_path=args.lock,
            config_root=args.config_root,
        )
        stopped = Event()
        previous = {
            signum: signal.signal(signum, lambda _signum, _frame: stopped.set())
            for signum in (signal.SIGINT, signal.SIGTERM)
        }
        try:
            service.start()
            health = service.health()
            health_path = None if args.health_file is None else Path(args.health_file)
            if health_path is not None:
                health_path.write_text(
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
            if args.health_file is not None:
                Path(args.health_file).unlink(missing_ok=True)
            for signum, handler in previous.items():
                signal.signal(signum, handler)
        return {"status": "SHUTDOWN"}
    raise AssertionError("unreachable command")


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, delegate once, and map bounded failures to exit code 2."""
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
