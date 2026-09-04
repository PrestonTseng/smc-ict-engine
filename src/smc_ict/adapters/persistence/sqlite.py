"""Exact schema-v1 SQLite persistence adapter."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from hashlib import sha256
from pathlib import Path
from typing import cast

from smc_ict.application.ports.notifications import (
    NotificationDedupRecord,
    NotificationDeliveryRecord,
)
from smc_ict.application.ports.repository import (
    DecisionRecord,
    ObservationRecord,
    RunRecord,
    SyncState,
)
from smc_ict.application.receipt_contract import (
    RUN_RECEIPT_SUCCESS_STATUSES,
    SCHEDULER_FAILURE_OUTCOMES,
)
from smc_ict.domain import ClosedCandle

DDL = """
CREATE TABLE candles_1m (
    provider_id TEXT NOT NULL,
    market_type TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    provider_symbol TEXT NOT NULL,
    interval TEXT NOT NULL,
    open_time_ms INTEGER NOT NULL,
    close_time_ms INTEGER NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    base_volume TEXT NOT NULL,
    quote_volume TEXT,
    source_fields_json TEXT NOT NULL,
    PRIMARY KEY (provider_id, market_type, instrument_id, interval, open_time_ms),
    CHECK (length(provider_id) BETWEEN 1 AND 32),
    CHECK (market_type = 'LINEAR_PERPETUAL'),
    CHECK (length(instrument_id) BETWEEN 1 AND 64 AND instrument_id = upper(instrument_id)),
    CHECK (length(provider_symbol) BETWEEN 1 AND 64),
    CHECK (interval = '1m'),
    CHECK (open_time_ms >= 0 AND open_time_ms % 60000 = 0),
    CHECK (close_time_ms = open_time_ms + 59999),
    CHECK (length(open) BETWEEN 1 AND 64 AND open NOT GLOB '*[^0-9.]*'),
    CHECK (length(high) BETWEEN 1 AND 64 AND high NOT GLOB '*[^0-9.]*'),
    CHECK (length(low) BETWEEN 1 AND 64 AND low NOT GLOB '*[^0-9.]*'),
    CHECK (length(close) BETWEEN 1 AND 64 AND close NOT GLOB '*[^0-9.]*'),
    CHECK (length(base_volume) BETWEEN 1 AND 64 AND base_volume NOT GLOB '*[^0-9.]*'),
    CHECK (quote_volume IS NULL OR
        (length(quote_volume) BETWEEN 1 AND 64 AND quote_volume NOT GLOB '*[^0-9.]*')),
    CHECK (json_valid(source_fields_json))
) STRICT;
CREATE INDEX idx_candles_1m_active_range
    ON candles_1m(provider_id, market_type, instrument_id, interval, open_time_ms, close_time_ms);

CREATE TABLE sync_state (
    provider_id TEXT NOT NULL,
    market_type TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    interval TEXT NOT NULL,
    last_completed_open_time_ms INTEGER NOT NULL,
    last_completed_close_time_ms INTEGER NOT NULL,
    last_successful_sync_ms INTEGER NOT NULL,
    last_error TEXT,
    PRIMARY KEY (provider_id, market_type, instrument_id, interval),
    CHECK (length(provider_id) BETWEEN 1 AND 32),
    CHECK (market_type = 'LINEAR_PERPETUAL'),
    CHECK (instrument_id = upper(instrument_id)),
    CHECK (interval = '1m'),
    CHECK (last_completed_open_time_ms >= 0 AND last_completed_open_time_ms % 60000 = 0),
    CHECK (last_completed_close_time_ms = last_completed_open_time_ms + 59999),
    CHECK (last_successful_sync_ms >= 0),
    CHECK (last_error IS NULL OR length(last_error) BETWEEN 1 AND 1000)
) STRICT;

CREATE TABLE runs (
    run_id TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    started_at_ms INTEGER NOT NULL,
    completed_at_ms INTEGER,
    strategy_name TEXT NOT NULL,
    strategy_version TEXT NOT NULL,
    strategy_config_hash TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    market_type TEXT NOT NULL,
    market_config_hash TEXT NOT NULL,
    git_commit TEXT NOT NULL,
    data_start_open_ms INTEGER NOT NULL,
    data_end_close_ms INTEGER NOT NULL,
    data_hash TEXT NOT NULL,
    notification_dedup_json TEXT NOT NULL DEFAULT '[]',
    notification_outcomes_json TEXT NOT NULL DEFAULT '[]',
    scheduler_outcome TEXT,
    error TEXT,
    CHECK (status IN ('RUNNING','SUCCEEDED','FAILED')),
    CHECK (started_at_ms >= 0),
    CHECK (completed_at_ms IS NULL OR completed_at_ms >= started_at_ms),
    CHECK (length(strategy_config_hash)=64 AND strategy_config_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(provider_id) BETWEEN 1 AND 32),
    CHECK (market_type = 'LINEAR_PERPETUAL'),
    CHECK (length(market_config_hash)=64 AND market_config_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (length(git_commit)=40 AND git_commit NOT GLOB '*[^0-9a-f]*'),
    CHECK (data_start_open_ms >= 0 AND data_start_open_ms % 60000 = 0),
    CHECK (data_end_close_ms >= data_start_open_ms + 59999 AND data_end_close_ms % 60000 = 59999),
    CHECK (length(data_hash)=64 AND data_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (json_valid(notification_dedup_json)),
    CHECK (json_valid(notification_outcomes_json)),
    CHECK (scheduler_outcome IS NULL OR scheduler_outcome IN
        ('SUCCEEDED','SUCCEEDED_WITH_WARNINGS','FAILED','OVERLAP_SKIPPED',
         'MAXIMUM_RUNTIME','SCHEDULER_SHUTDOWN','PROCESS_RESTART')),
    CHECK (error IS NULL OR length(error) BETWEEN 1 AND 4000),
    CHECK ((status='RUNNING' AND completed_at_ms IS NULL AND error IS NULL)
        OR (status='SUCCEEDED' AND completed_at_ms IS NOT NULL AND error IS NULL)
        OR (status='FAILED' AND completed_at_ms IS NOT NULL AND error IS NOT NULL))
) STRICT;
CREATE INDEX idx_runs_strategy_completed
    ON runs(strategy_name, strategy_version, completed_at_ms DESC);

CREATE TABLE observations (
    run_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    signal_id TEXT NOT NULL,
    status TEXT NOT NULL,
    event_time_ms INTEGER,
    known_time_ms INTEGER,
    reason TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, instrument_id, signal_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    CHECK (instrument_id = upper(instrument_id)),
    CHECK (status IN ('PASS','FAIL','UNAVAILABLE')),
    CHECK ((event_time_ms IS NULL AND known_time_ms IS NULL)
        OR (event_time_ms IS NOT NULL AND known_time_ms IS NOT NULL
            AND event_time_ms >= 0 AND known_time_ms >= event_time_ms)),
    CHECK (length(reason) BETWEEN 1 AND 1000),
    CHECK (json_valid(payload_json))
) STRICT;
CREATE INDEX idx_observations_signal_event ON observations(signal_id, event_time_ms);

CREATE TABLE decisions (
    run_id TEXT NOT NULL,
    instrument_id TEXT NOT NULL,
    decision_status TEXT NOT NULL,
    direction TEXT,
    entry_text TEXT,
    stop_text TEXT,
    target_text TEXT,
    first_failed_signal TEXT,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (run_id, instrument_id),
    FOREIGN KEY (run_id) REFERENCES runs(run_id) ON DELETE CASCADE,
    CHECK (instrument_id = upper(instrument_id)),
    CHECK (decision_status IN ('READY','NO_TRADE','UNAVAILABLE')),
    CHECK (direction IS NULL OR direction IN ('LONG','SHORT')),
    CHECK (json_valid(payload_json)),
    CHECK ((decision_status='READY' AND direction IN ('LONG','SHORT')
            AND entry_text IS NOT NULL AND stop_text IS NOT NULL AND target_text IS NOT NULL
            AND first_failed_signal IS NULL)
        OR (decision_status IN ('NO_TRADE','UNAVAILABLE') AND direction IS NULL
            AND entry_text IS NULL AND stop_text IS NULL AND target_text IS NULL
            AND first_failed_signal IS NOT NULL AND length(first_failed_signal) BETWEEN 1 AND 200))
) STRICT;
"""

_TABLES = {"candles_1m", "sync_state", "runs", "observations", "decisions"}
_CANDLE_COLUMNS = (
    "provider_id",
    "market_type",
    "instrument_id",
    "provider_symbol",
    "interval",
    "open_time_ms",
    "close_time_ms",
    "open",
    "high",
    "low",
    "close",
    "base_volume",
    "quote_volume",
    "source_fields_json",
)
_RUN_COLUMNS = (
    "run_id",
    "status",
    "started_at_ms",
    "completed_at_ms",
    "strategy_name",
    "strategy_version",
    "strategy_config_hash",
    "provider_id",
    "market_type",
    "market_config_hash",
    "git_commit",
    "data_start_open_ms",
    "data_end_close_ms",
    "data_hash",
    "error",
)


class SourceConflictError(RuntimeError):
    """A durable primary key already names different market truth."""


class PersistenceConflictError(RuntimeError):
    """An idempotence key already names different durable evidence."""


class SQLiteRepository:
    repository_id = "sqlite"

    def __init__(self, path: str | Path = "./data/smc_ict.db") -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute("PRAGMA busy_timeout=5000")
        connection.execute("PRAGMA trusted_schema=OFF")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
                )
            }
            if version == 0 and not tables:
                connection.executescript(f"{DDL}\nPRAGMA user_version=1;")
                return
            if version != 1 or tables != _TABLES:
                raise RuntimeError("unsupported or malformed SQLite schema")
            run_columns = {
                row[1] for row in connection.execute("PRAGMA table_info(runs)").fetchall()
            }
            if "notification_dedup_json" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN notification_dedup_json TEXT NOT NULL "
                    "DEFAULT '[]' CHECK (json_valid(notification_dedup_json))"
                )
            if "notification_outcomes_json" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN notification_outcomes_json TEXT NOT NULL "
                    "DEFAULT '[]' CHECK (json_valid(notification_outcomes_json))"
                )
            if "scheduler_outcome" not in run_columns:
                connection.execute(
                    "ALTER TABLE runs ADD COLUMN scheduler_outcome TEXT CHECK "
                    "(scheduler_outcome IS NULL OR scheduler_outcome IN "
                    "('SUCCEEDED','SUCCEEDED_WITH_WARNINGS','FAILED','OVERLAP_SKIPPED',"
                    "'MAXIMUM_RUNTIME','SCHEDULER_SHUTDOWN','PROCESS_RESTART'))"
                )

    def store_candle_page(
        self,
        candles: Sequence[ClosedCandle],
        *,
        successful_sync_ms: int,
        required_start_open_ms: int,
    ) -> None:
        page = tuple(candles)
        if not page:
            raise ValueError("candle page must not be empty")
        if type(successful_sync_ms) is not int or successful_sync_ms < 0:
            raise ValueError("successful sync time must be a non-negative integer")
        if (
            type(required_start_open_ms) is not int
            or required_start_open_ms < 0
            or required_start_open_ms % 60_000 != 0
        ):
            raise ValueError("required sync start must be an aligned non-negative integer")
        identity = (
            page[0].provider_id,
            page[0].market_type,
            page[0].instrument_id,
            page[0].interval,
        )
        if any(
            (item.provider_id, item.market_type, item.instrument_id, item.interval) != identity
            for item in page
        ):
            raise ValueError("one candle page cannot mix market identities")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for candle in page:
                values = self._candle_values(candle)
                existing = connection.execute(
                    "SELECT " + ",".join(_CANDLE_COLUMNS) + " FROM candles_1m "
                    "WHERE provider_id=? AND market_type=? AND instrument_id=? "
                    "AND interval=? AND open_time_ms=?",
                    candle.primary_key,
                ).fetchone()
                if existing is None:
                    placeholders = ",".join("?" for _ in _CANDLE_COLUMNS)
                    connection.execute(
                        f"INSERT INTO candles_1m ({','.join(_CANDLE_COLUMNS)}) "
                        f"VALUES ({placeholders})",
                        values,
                    )
                elif tuple(existing[column] for column in _CANDLE_COLUMNS) != values:
                    raise SourceConflictError("SOURCE_CONFLICT at canonical candle primary key")
            self._advance_sync_state(
                connection, identity, successful_sync_ms, required_start_open_ms
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _advance_sync_state(
        self,
        connection: sqlite3.Connection,
        identity: tuple[str, str, str, str],
        successful_sync_ms: int,
        required_start_open_ms: int,
    ) -> None:
        existing = connection.execute(
            "SELECT last_completed_open_time_ms FROM sync_state "
            "WHERE provider_id=? AND market_type=? AND instrument_id=? AND interval=?",
            identity,
        ).fetchone()
        expected = required_start_open_ms
        if existing is not None and cast(int, existing[0]) + 60_000 < required_start_open_ms:
            return
        prefix: int | None = None
        for row in connection.execute(
            "SELECT open_time_ms FROM candles_1m WHERE provider_id=? AND market_type=? "
            "AND instrument_id=? AND interval=? AND open_time_ms>=? ORDER BY open_time_ms",
            (*identity, required_start_open_ms),
        ):
            open_time = cast(int, row[0])
            if open_time != expected:
                break
            prefix = open_time
            expected += 60_000
        if prefix is None:
            return
        if existing is not None:
            prefix = max(prefix, cast(int, existing[0]))
        connection.execute(
            "INSERT INTO sync_state (provider_id,market_type,instrument_id,interval,"
            "last_completed_open_time_ms,last_completed_close_time_ms,"
            "last_successful_sync_ms,last_error) VALUES (?,?,?,?,?,?,?,NULL) "
            "ON CONFLICT(provider_id,market_type,instrument_id,interval) DO UPDATE SET "
            "last_completed_open_time_ms=excluded.last_completed_open_time_ms,"
            "last_completed_close_time_ms=excluded.last_completed_close_time_ms,"
            "last_successful_sync_ms=excluded.last_successful_sync_ms,last_error=NULL",
            (*identity, prefix, prefix + 59_999, successful_sync_ms),
        )

    def load_candles(
        self,
        provider_id: str,
        market_type: str,
        instrument_id: str,
        start_open_ms: int,
        end_open_ms: int,
    ) -> tuple[ClosedCandle, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT " + ",".join(_CANDLE_COLUMNS) + " FROM candles_1m "
                "WHERE provider_id=? AND market_type=? AND instrument_id=? AND interval='1m' "
                "AND open_time_ms BETWEEN ? AND ? ORDER BY open_time_ms",
                (provider_id, market_type, instrument_id, start_open_ms, end_open_ms),
            ).fetchall()
        return tuple(self._row_to_candle(row) for row in rows)

    def load_sync_state(
        self, provider_id: str, market_type: str, instrument_id: str
    ) -> SyncState | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT provider_id,market_type,instrument_id,interval,"
                "last_completed_open_time_ms,last_completed_close_time_ms,"
                "last_successful_sync_ms,last_error FROM sync_state "
                "WHERE provider_id=? AND market_type=? AND instrument_id=? AND interval='1m'",
                (provider_id, market_type, instrument_id),
            ).fetchone()
        return None if row is None else SyncState(*tuple(row))

    def store_run(self, run: RunRecord) -> None:
        columns = tuple(run.__dataclass_fields__)
        values = tuple(getattr(run, column) for column in columns)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT " + ",".join(columns) + " FROM runs WHERE run_id=?", (run.run_id,)
            ).fetchone()
            if existing is None:
                connection.execute(
                    f"INSERT INTO runs ({','.join(columns)}) VALUES "
                    f"({','.join('?' for _ in columns)})",
                    values,
                )
            elif tuple(existing[column] for column in columns) != values:
                raise PersistenceConflictError("PERSISTENCE_CONFLICT for run ID")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def start_scheduler_attempt(self, job_id: str, *, started_at_ms: int, sequence: int) -> str:
        """Persist a redacted scheduler-owned attempt before invoking its child operation."""
        if type(job_id) is not str or not 1 <= len(job_id) <= 200:
            raise ValueError("scheduler job ID must contain 1..200 characters")
        if type(started_at_ms) is not int or started_at_ms < 0:
            raise ValueError("scheduler attempt time must be a non-negative integer")
        if type(sequence) is not int or sequence < 0:
            raise ValueError("scheduler attempt sequence must be a non-negative integer")
        identity = self._canonical_json([job_id, started_at_ms, sequence]).encode()
        attempt_id = "scheduler-attempt-" + sha256(b"scheduler-attempt-v1\0" + identity).hexdigest()
        aligned_start = started_at_ms // 60_000 * 60_000
        self.store_run(
            RunRecord(
                run_id=attempt_id,
                status="RUNNING",
                started_at_ms=started_at_ms,
                completed_at_ms=None,
                strategy_name=f"scheduler:{job_id}",
                strategy_version="attempt-v1",
                strategy_config_hash=sha256(job_id.encode()).hexdigest(),
                provider_id="scheduler",
                market_type="LINEAR_PERPETUAL",
                market_config_hash=sha256(b"scheduler-attempt-v1").hexdigest(),
                git_commit="0" * 40,
                data_start_open_ms=aligned_start,
                data_end_close_ms=aligned_start + 59_999,
                data_hash=sha256(attempt_id.encode()).hexdigest(),
                error=None,
            )
        )
        return attempt_id

    def finish_scheduler_attempt(
        self, attempt_id: str, outcome: str, *, completed_at_ms: int
    ) -> RunRecord:
        """Finish an attempt with only its outcome category, never child error text."""
        terminal_status = "SUCCEEDED" if outcome in RUN_RECEIPT_SUCCESS_STATUSES else "FAILED"
        error = (
            None
            if terminal_status == "SUCCEEDED"
            else outcome
            if outcome in SCHEDULER_FAILURE_OUTCOMES
            else "FAILED"
        )
        return self.finish_run(
            attempt_id,
            terminal_status,
            completed_at_ms=completed_at_ms,
            error=error,
            scheduler_outcome=outcome,
        )

    def notification_delivered_at(self, destination_id: str, deduplication_id: str) -> int | None:
        self._validate_notification_identity(destination_id, deduplication_id)
        delivered: list[int] = []
        with self._connect() as connection:
            rows = connection.execute("SELECT notification_dedup_json FROM runs").fetchall()
        for row in rows:
            for record in self._notification_records(row[0]):
                if (
                    record["destination_id"] == destination_id
                    and record["deduplication_id"] == deduplication_id
                ):
                    delivered.append(cast(int, record["delivered_at_seconds"]))
        return max(delivered, default=None)

    def store_notification_deliveries(self, records: tuple[NotificationDedupRecord, ...]) -> None:
        if not records:
            return
        for record in records:
            if type(record.run_id) is not str or not record.run_id:
                raise ValueError("invalid notification run identity")
            self._validate_notification_identity(record.destination_id, record.deduplication_id)
            if type(record.delivered_at_seconds) is not int or record.delivered_at_seconds < 0:
                raise ValueError("invalid notification delivery time")

        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            by_run: dict[str, list[NotificationDedupRecord]] = {}
            for record in records:
                by_run.setdefault(record.run_id, []).append(record)
            for run_id, additions in by_run.items():
                row = connection.execute(
                    "SELECT notification_dedup_json FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("unknown notification run identity")
                current = self._notification_records(row[0])
                identities = {
                    (record.destination_id, record.deduplication_id) for record in additions
                }
                retained = [
                    record
                    for record in current
                    if (record["destination_id"], record["deduplication_id"]) not in identities
                ]
                retained.extend(
                    {
                        "destination_id": record.destination_id,
                        "deduplication_id": record.deduplication_id,
                        "delivered_at_seconds": record.delivered_at_seconds,
                    }
                    for record in additions
                )
                connection.execute(
                    "UPDATE runs SET notification_dedup_json=? WHERE run_id=?",
                    (self._canonical_json(retained), run_id),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def store_notification_outcomes(self, records: tuple[NotificationDeliveryRecord, ...]) -> None:
        if not records:
            return
        for record in records:
            self._validate_notification_outcome(record)
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            by_run: dict[str, list[NotificationDeliveryRecord]] = {}
            for record in records:
                by_run.setdefault(record.run_id, []).append(record)
            for run_id, additions in by_run.items():
                row = connection.execute(
                    "SELECT notification_outcomes_json FROM runs WHERE run_id=?", (run_id,)
                ).fetchone()
                if row is None:
                    raise KeyError("unknown notification run identity")
                current = self._notification_outcome_records(row[0], run_id)
                current.extend(additions)
                connection.execute(
                    "UPDATE runs SET notification_outcomes_json=? WHERE run_id=?",
                    (
                        self._canonical_json(
                            [
                                self._notification_outcome_payload(record)
                                for record in current[-100:]
                            ]
                        ),
                        run_id,
                    ),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_notification_outcomes(self, run_id: str) -> tuple[NotificationDeliveryRecord, ...]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT notification_outcomes_json FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        if row is None:
            raise KeyError("unknown notification run identity")
        return tuple(self._notification_outcome_records(row[0], run_id))

    @classmethod
    def _notification_outcome_records(
        cls, encoded: object, run_id: str
    ) -> list[NotificationDeliveryRecord]:
        if type(encoded) is not str:
            raise RuntimeError("invalid notification outcome state")
        try:
            payload = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid notification outcome state") from exc
        fields = {
            "destination_id",
            "adapter_id",
            "attempted_at_seconds",
            "attempts",
            "outcome",
            "reason_code",
            "status_code",
        }
        if type(payload) is not list or len(payload) > 100:
            raise RuntimeError("invalid notification outcome state")
        records: list[NotificationDeliveryRecord] = []
        for item in payload:
            if type(item) is not dict or set(item) != fields:
                raise RuntimeError("invalid notification outcome state")
            record = NotificationDeliveryRecord(run_id=run_id, **item)
            try:
                cls._validate_notification_outcome(record)
            except (TypeError, ValueError) as exc:
                raise RuntimeError("invalid notification outcome state") from exc
            records.append(record)
        return records

    @staticmethod
    def _notification_outcome_payload(record: NotificationDeliveryRecord) -> dict[str, object]:
        return {
            "destination_id": record.destination_id,
            "adapter_id": record.adapter_id,
            "attempted_at_seconds": record.attempted_at_seconds,
            "attempts": record.attempts,
            "outcome": record.outcome,
            "reason_code": record.reason_code,
            "status_code": record.status_code,
        }

    @staticmethod
    def _validate_notification_outcome(record: NotificationDeliveryRecord) -> None:
        if type(record.run_id) is not str or not record.run_id:
            raise ValueError("invalid notification run identity")
        if type(record.destination_id) is not str or not 1 <= len(record.destination_id) <= 64:
            raise ValueError("invalid notification destination identity")
        if type(record.adapter_id) is not str or not 1 <= len(record.adapter_id) <= 64:
            raise ValueError("invalid notification adapter identity")
        if type(record.attempted_at_seconds) is not int or record.attempted_at_seconds < 0:
            raise ValueError("invalid notification outcome time")
        if type(record.attempts) is not int or not 0 <= record.attempts <= 5:
            raise ValueError("invalid notification attempt count")
        if record.outcome == "SUCCESS":
            valid = (
                record.attempts >= 1
                and record.reason_code is None
                and type(record.status_code) is int
                and 200 <= record.status_code < 300
            )
        elif record.outcome == "FAILURE":
            valid = (
                (
                    record.reason_code == "ADAPTER_UNAVAILABLE"
                    and record.attempts == 0
                    and record.status_code is None
                )
                or (
                    record.reason_code == "TRANSPORT_ERROR"
                    and record.attempts >= 1
                    and record.status_code is None
                )
                or (
                    type(record.status_code) is int
                    and 100 <= record.status_code <= 599
                    and record.reason_code == f"HTTP_{record.status_code}"
                )
            )
        else:
            valid = False
        if not valid:
            raise ValueError("invalid notification outcome")

    @staticmethod
    def _validate_notification_identity(destination_id: object, deduplication_id: object) -> None:
        if type(destination_id) is not str or not 1 <= len(destination_id) <= 64:
            raise ValueError("invalid notification destination identity")
        if (
            type(deduplication_id) is not str
            or len(deduplication_id) != 64
            or any(character not in "0123456789abcdef" for character in deduplication_id)
        ):
            raise ValueError("invalid notification deduplication identity")

    @classmethod
    def _notification_records(cls, encoded: object) -> list[dict[str, object]]:
        if type(encoded) is not str:
            raise RuntimeError("invalid notification deduplication state")
        try:
            value = json.loads(encoded)
        except json.JSONDecodeError as exc:
            raise RuntimeError("invalid notification deduplication state") from exc
        if type(value) is not list:
            raise RuntimeError("invalid notification deduplication state")
        records: list[dict[str, object]] = []
        for item in value:
            if type(item) is not dict or set(item) != {
                "destination_id",
                "deduplication_id",
                "delivered_at_seconds",
            }:
                raise RuntimeError("invalid notification deduplication state")
            try:
                cls._validate_notification_identity(
                    item["destination_id"], item["deduplication_id"]
                )
            except ValueError as exc:
                raise RuntimeError("invalid notification deduplication state") from exc
            delivered_at = item["delivered_at_seconds"]
            if type(delivered_at) is not int or delivered_at < 0:
                raise RuntimeError("invalid notification deduplication state")
            records.append(item)
        return records

    def finish_run(
        self,
        run_id: str,
        status: str,
        *,
        completed_at_ms: int,
        error: str | None,
        scheduler_outcome: str | None = None,
    ) -> RunRecord:
        if status not in {"SUCCEEDED", "FAILED"}:
            raise ValueError("terminal run status must be SUCCEEDED or FAILED")
        allowed_scheduler_outcomes = RUN_RECEIPT_SUCCESS_STATUSES | SCHEDULER_FAILURE_OUTCOMES
        if scheduler_outcome is not None and scheduler_outcome not in allowed_scheduler_outcomes:
            raise ValueError("invalid scheduler outcome")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError("unknown run ID")
            if row["status"] == "RUNNING":
                connection.execute(
                    "UPDATE runs SET status=?,completed_at_ms=?,error=?,scheduler_outcome=? "
                    "WHERE run_id=? AND status='RUNNING'",
                    (status, completed_at_ms, error, scheduler_outcome, run_id),
                )
            elif (
                row["status"] != status
                or row["completed_at_ms"] != completed_at_ms
                or row["error"] != error
                or row["scheduler_outcome"] != scheduler_outcome
            ):
                raise PersistenceConflictError("PERSISTENCE_CONFLICT for terminal run state")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.load_run(run_id)
        if result is None:
            raise RuntimeError("completed run disappeared")
        return result

    def load_run(self, run_id: str) -> RunRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT " + ",".join(_RUN_COLUMNS) + " FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
        return None if row is None else RunRecord(*tuple(row))

    def store_observations(self, observations: Sequence[ObservationRecord]) -> None:
        rows = tuple(observations)
        if not rows:
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for observation in rows:
                values = (
                    observation.run_id,
                    observation.instrument_id,
                    observation.signal_id,
                    observation.status,
                    observation.event_time_ms,
                    observation.known_time_ms,
                    observation.reason,
                    self._canonical_json(observation.payload),
                )
                existing = connection.execute(
                    "SELECT run_id,instrument_id,signal_id,status,event_time_ms,known_time_ms,"
                    "reason,payload_json FROM observations "
                    "WHERE run_id=? AND instrument_id=? AND signal_id=?",
                    values[:3],
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO observations "
                        "(run_id,instrument_id,signal_id,status,event_time_ms,known_time_ms,"
                        "reason,payload_json) VALUES (?,?,?,?,?,?,?,?)",
                        values,
                    )
                elif tuple(existing) != values:
                    raise PersistenceConflictError(
                        "PERSISTENCE_CONFLICT for observation primary key"
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_observations(self, run_id: str) -> tuple[ObservationRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id,instrument_id,signal_id,status,event_time_ms,known_time_ms,"
                "reason,payload_json FROM observations WHERE run_id=? "
                "ORDER BY instrument_id,signal_id",
                (run_id,),
            ).fetchall()
        return tuple(
            ObservationRecord(
                run_id=row["run_id"],
                instrument_id=row["instrument_id"],
                signal_id=row["signal_id"],
                status=row["status"],
                event_time_ms=row["event_time_ms"],
                known_time_ms=row["known_time_ms"],
                reason=row["reason"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        )

    def store_decisions(self, decisions: Sequence[DecisionRecord]) -> None:
        rows = tuple(decisions)
        if not rows:
            return
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            for decision in rows:
                values = (
                    decision.run_id,
                    decision.instrument_id,
                    decision.decision_status,
                    decision.direction,
                    decision.entry_text,
                    decision.stop_text,
                    decision.target_text,
                    decision.first_failed_signal,
                    self._canonical_json(decision.payload),
                )
                existing = connection.execute(
                    "SELECT run_id,instrument_id,decision_status,direction,entry_text,stop_text,"
                    "target_text,first_failed_signal,payload_json FROM decisions "
                    "WHERE run_id=? AND instrument_id=?",
                    values[:2],
                ).fetchone()
                if existing is None:
                    connection.execute(
                        "INSERT INTO decisions "
                        "(run_id,instrument_id,decision_status,direction,entry_text,stop_text,"
                        "target_text,first_failed_signal,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                        values,
                    )
                elif tuple(existing) != values:
                    raise PersistenceConflictError("PERSISTENCE_CONFLICT for decision primary key")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_decisions(self, run_id: str) -> tuple[DecisionRecord, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT run_id,instrument_id,decision_status,direction,entry_text,stop_text,"
                "target_text,first_failed_signal,payload_json FROM decisions WHERE run_id=? "
                "ORDER BY instrument_id",
                (run_id,),
            ).fetchall()
        return tuple(
            DecisionRecord(
                run_id=row["run_id"],
                instrument_id=row["instrument_id"],
                decision_status=row["decision_status"],
                direction=row["direction"],
                entry_text=row["entry_text"],
                stop_text=row["stop_text"],
                target_text=row["target_text"],
                first_failed_signal=row["first_failed_signal"],
                payload=json.loads(row["payload_json"]),
            )
            for row in rows
        )

    def commit_run(
        self,
        run_id: str,
        observations: Sequence[ObservationRecord],
        decisions: Sequence[DecisionRecord],
        *,
        completed_at_ms: int,
    ) -> RunRecord:
        """Atomically persist analysis evidence and the successful terminal state."""
        if any(observation.run_id != run_id for observation in observations) or any(
            decision.run_id != run_id for decision in decisions
        ):
            raise PersistenceConflictError("PERSISTENCE_CONFLICT for evidence run ID")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["status"] != "RUNNING":
                raise PersistenceConflictError("run is not in RUNNING state")
            for observation in observations:
                connection.execute(
                    "INSERT INTO observations "
                    "(run_id,instrument_id,signal_id,status,event_time_ms,known_time_ms,reason,"
                    "payload_json) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        observation.run_id,
                        observation.instrument_id,
                        observation.signal_id,
                        observation.status,
                        observation.event_time_ms,
                        observation.known_time_ms,
                        observation.reason,
                        self._canonical_json(observation.payload),
                    ),
                )
            for decision in decisions:
                connection.execute(
                    "INSERT INTO decisions "
                    "(run_id,instrument_id,decision_status,direction,entry_text,stop_text,target_text,"
                    "first_failed_signal,payload_json) VALUES (?,?,?,?,?,?,?,?,?)",
                    (
                        decision.run_id,
                        decision.instrument_id,
                        decision.decision_status,
                        decision.direction,
                        decision.entry_text,
                        decision.stop_text,
                        decision.target_text,
                        decision.first_failed_signal,
                        self._canonical_json(decision.payload),
                    ),
                )
            connection.execute(
                "UPDATE runs SET status='SUCCEEDED',completed_at_ms=?,error=NULL "
                "WHERE run_id=? AND status='RUNNING'",
                (completed_at_ms, run_id),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        result = self.load_run(run_id)
        if result is None:
            raise RuntimeError("committed run disappeared")
        return result

    def recover_running_runs(
        self,
        *,
        completed_at_ms: int,
        reason: str,
        include_scheduler_attempts: bool = True,
    ) -> tuple[str, ...]:
        """Fail stale RUNNING rows atomically during scheduler restart recovery."""
        if type(completed_at_ms) is not int or completed_at_ms < 0:
            raise ValueError("recovery time must be a non-negative integer")
        if not 1 <= len(reason) <= 4000:
            raise ValueError("recovery reason must contain 1..4000 characters")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            ownership_clause = "" if include_scheduler_attempts else " AND provider_id<>'scheduler'"
            rows = connection.execute(
                "SELECT run_id FROM runs WHERE status='RUNNING' AND started_at_ms<=? "
                + ownership_clause
                + " ORDER BY run_id",
                (completed_at_ms,),
            ).fetchall()
            run_ids = tuple(row["run_id"] for row in rows)
            connection.execute(
                "UPDATE runs SET status='FAILED',completed_at_ms=?,error=? "
                "WHERE status='RUNNING' AND started_at_ms<=?" + ownership_clause,
                (completed_at_ms, reason, completed_at_ms),
            )
            connection.commit()
            return run_ids
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def database_status(self) -> dict[str, int | str]:
        """Return a non-secret readiness snapshot for CLI health checks."""
        with self._connect() as connection:
            version = cast(int, connection.execute("PRAGMA user_version").fetchone()[0])
            tables = cast(
                int,
                connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table'"
                ).fetchone()[0],
            )
            candles = cast(int, connection.execute("SELECT count(*) FROM candles_1m").fetchone()[0])
            runs = cast(int, connection.execute("SELECT count(*) FROM runs").fetchone()[0])
        return {
            "status": "READY",
            "schema_version": version,
            "tables": tables,
            "candles": candles,
            "runs": runs,
        }

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(
            SQLiteRepository._json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        )

    @staticmethod
    def _json_value(value: object) -> object:
        if isinstance(value, Mapping):
            return {key: SQLiteRepository._json_value(item) for key, item in value.items()}
        if isinstance(value, list | tuple):
            return [SQLiteRepository._json_value(item) for item in value]
        return value

    @staticmethod
    def _source_fields_json(candle: ClosedCandle) -> str:
        return SQLiteRepository._canonical_json(dict(candle.source_fields))

    @classmethod
    def _candle_values(cls, candle: ClosedCandle) -> tuple[object, ...]:
        return (
            candle.provider_id,
            candle.market_type,
            candle.instrument_id,
            candle.provider_symbol,
            candle.interval,
            candle.open_time_ms,
            candle.close_time_ms,
            candle.open,
            candle.high,
            candle.low,
            candle.close,
            candle.base_volume,
            candle.quote_volume,
            cls._source_fields_json(candle),
        )

    @staticmethod
    def _row_to_candle(row: sqlite3.Row) -> ClosedCandle:
        return ClosedCandle(
            provider_id=row["provider_id"],
            market_type=row["market_type"],
            instrument_id=row["instrument_id"],
            provider_symbol=row["provider_symbol"],
            interval=row["interval"],
            open_time_ms=row["open_time_ms"],
            close_time_ms=row["close_time_ms"],
            open=row["open"],
            high=row["high"],
            low=row["low"],
            close=row["close"],
            base_volume=row["base_volume"],
            quote_volume=row["quote_volume"],
            source_fields=json.loads(row["source_fields_json"]),
        )
