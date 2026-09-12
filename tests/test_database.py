from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest


def test_sqlite_exact_schema_idempotent_pages_conflicts_and_contiguous_sync(tmp_path: Path) -> None:
    from trading_research.adapters.persistence.sqlite import SourceConflictError, SQLiteRepository
    from trading_research.domain import ClosedCandle

    def candle(open_time_ms: int, close: str = "101") -> ClosedCandle:
        return ClosedCandle(
            provider_id="binance_usdm",
            market_type="LINEAR_PERPETUAL",
            instrument_id="BTC-USDT-PERP",
            provider_symbol="BTCUSDT",
            interval="1m",
            open_time_ms=open_time_ms,
            close_time_ms=open_time_ms + 59_999,
            open="100",
            high="102",
            low="99",
            close=close,
            base_volume="1.50",
            quote_volume="150.00",
            source_fields={
                "trade_count": 2,
                "taker_buy_base_volume": "0.75",
                "taker_buy_quote_volume": "75.00",
            },
        )

    path = tmp_path / "trading_research.db"
    repository = SQLiteRepository(path)
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        assert tables == {"candles_1m", "sync_state", "runs", "observations", "decisions"}
        assert connection.execute("PRAGMA user_version").fetchone() == (2,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []

    first = candle(0)
    repository.store_candle_page((first,), successful_sync_ms=180_000, required_start_open_ms=0)
    repository.store_candle_page((first,), successful_sync_ms=180_000, required_start_open_ms=0)
    assert repository.load_candles(
        "binance_usdm", "LINEAR_PERPETUAL", "BTC-USDT-PERP", 0, 60_000
    ) == (first,)
    assert (
        repository.load_sync_state(
            "binance_usdm", "LINEAR_PERPETUAL", "BTC-USDT-PERP"
        ).last_completed_open_time_ms
        == 0
    )

    with pytest.raises(SourceConflictError, match="SOURCE_CONFLICT"):
        repository.store_candle_page(
            (candle(60_000), replace(first, close="100")),
            successful_sync_ms=180_000,
            required_start_open_ms=0,
        )
    assert repository.load_candles(
        "binance_usdm", "LINEAR_PERPETUAL", "BTC-USDT-PERP", 0, 60_000
    ) == (first,)

    second = candle(60_000)
    repository.store_candle_page((second,), successful_sync_ms=180_000, required_start_open_ms=0)
    assert (
        repository.load_sync_state(
            "binance_usdm", "LINEAR_PERPETUAL", "BTC-USDT-PERP"
        ).last_completed_open_time_ms
        == 60_000
    )


def test_sqlite_v1_to_v2_migration_preserves_legacy_provenance_and_related_rows(
    tmp_path: Path,
) -> None:
    from trading_research.adapters.persistence.sqlite import V1_DDL, SQLiteRepository

    path = tmp_path / "legacy-v1.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(f"{V1_DDL}\nPRAGMA user_version=1;")
        connection.execute(
            "INSERT INTO runs (rowid,run_id,status,started_at_ms,completed_at_ms,"
            "strategy_name,strategy_version,"
            "strategy_config_hash,provider_id,market_type,market_config_hash,git_commit,"
            "data_start_open_ms,data_end_close_ms,data_hash,notification_dedup_json,"
            "notification_outcomes_json,scheduler_outcome,error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                42,
                "legacy-run",
                "SUCCEEDED",
                1,
                2,
                "legacy",
                "1",
                "a" * 64,
                "provider",
                "LINEAR_PERPETUAL",
                "b" * 64,
                "c" * 40,
                0,
                59_999,
                "d" * 64,
                '[{"delivered_at_seconds":3,"destination_id":"ops","deduplication_id":"one"}]',
                '[{"attempts":1,"destination_id":"ops","event_type":"run_succeeded",'
                '"outcome":"DELIVERED"}]',
                "SUCCEEDED",
                None,
            ),
        )
        connection.execute(
            "INSERT INTO observations VALUES (?,?,?,?,?,?,?,?)",
            ("legacy-run", "BTC-USDT-PERP", "signal.one", "PASS", 1, 1, "legacy", "{}"),
        )
        connection.execute(
            "INSERT INTO decisions VALUES (?,?,?,?,?,?,?,?,?)",
            (
                "legacy-run",
                "BTC-USDT-PERP",
                "NO_TRADE",
                None,
                None,
                None,
                None,
                "signal.one",
                "{}",
            ),
        )

    SQLiteRepository(path)
    SQLiteRepository(path)

    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(runs)")}
        retained = connection.execute(
            "SELECT rowid,status,strategy_name,git_commit,code_hash,notification_dedup_json,"
            "notification_outcomes_json,scheduler_outcome FROM runs WHERE run_id='legacy-run'"
        ).fetchone()
        observations = connection.execute("SELECT * FROM observations").fetchall()
        decisions = connection.execute("SELECT * FROM decisions").fetchall()
        indexes = {row[1] for row in connection.execute("PRAGMA index_list(runs)")}
        version = connection.execute("PRAGMA user_version").fetchone()
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
    assert {"git_commit", "code_hash", "notification_outcomes_json", "scheduler_outcome"} <= columns
    assert retained == (
        42,
        "SUCCEEDED",
        "legacy",
        "c" * 40,
        None,
        '[{"delivered_at_seconds":3,"destination_id":"ops","deduplication_id":"one"}]',
        '[{"attempts":1,"destination_id":"ops","event_type":"run_succeeded",'
        '"outcome":"DELIVERED"}]',
        "SUCCEEDED",
    )
    assert len(observations) == len(decisions) == 1
    assert "idx_runs_strategy_completed" in indexes
    assert version == (2,)
    assert integrity == ("ok",)
    assert foreign_keys == []


def test_sqlite_v1_to_v2_migration_rolls_back_every_change_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research.adapters.persistence import sqlite as sqlite_adapter

    path = tmp_path / "rollback-v1.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(f"{sqlite_adapter.V1_DDL}\nPRAGMA user_version=1;")
        connection.execute(
            "INSERT INTO runs (run_id,status,started_at_ms,completed_at_ms,strategy_name,"
            "strategy_version,strategy_config_hash,provider_id,market_type,market_config_hash,"
            "git_commit,data_start_open_ms,data_end_close_ms,data_hash,error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "legacy-run",
                "RUNNING",
                1,
                None,
                "legacy",
                "1",
                "a" * 64,
                "provider",
                "LINEAR_PERPETUAL",
                "b" * 64,
                "c" * 40,
                0,
                59_999,
                "d" * 64,
                None,
            ),
        )
    monkeypatch.setattr(
        sqlite_adapter,
        "_RUNS_V2_DDL",
        sqlite_adapter._RUNS_V2_DDL.replace("length(git_commit)=40", "length(git_commit)=39"),
    )

    with pytest.raises(sqlite3.IntegrityError):
        sqlite_adapter.SQLiteRepository(path)

    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        assert connection.execute("SELECT git_commit FROM runs").fetchone() == ("c" * 40,)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE type='table' AND name='runs_v2'"
        ).fetchone() == (0,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_sqlite_v1_to_v2_migration_accepts_v1_before_notification_outcome_columns(
    tmp_path: Path,
) -> None:
    from trading_research.adapters.persistence.sqlite import V1_DDL, SQLiteRepository

    legacy_ddl = (
        V1_DDL.replace("    notification_outcomes_json TEXT NOT NULL DEFAULT '[]',\n", "")
        .replace("    scheduler_outcome TEXT,\n", "")
        .replace("    CHECK (json_valid(notification_outcomes_json)),\n", "")
        .replace(
            "    CHECK (scheduler_outcome IS NULL OR scheduler_outcome IN\n"
            "        ('SUCCEEDED','SUCCEEDED_WITH_WARNINGS','FAILED','OVERLAP_SKIPPED',\n"
            "         'MAXIMUM_RUNTIME','SCHEDULER_SHUTDOWN','PROCESS_RESTART')),\n",
            "",
        )
    )
    path = tmp_path / "early-v1.db"
    with sqlite3.connect(path) as connection:
        connection.executescript(f"{legacy_ddl}\nPRAGMA user_version=1;")
        connection.execute(
            "INSERT INTO runs (run_id,status,started_at_ms,completed_at_ms,strategy_name,"
            "strategy_version,strategy_config_hash,provider_id,market_type,market_config_hash,"
            "git_commit,data_start_open_ms,data_end_close_ms,data_hash,error) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "early-run",
                "SUCCEEDED",
                1,
                2,
                "legacy",
                "1",
                "a" * 64,
                "provider",
                "LINEAR_PERPETUAL",
                "b" * 64,
                "c" * 40,
                0,
                59_999,
                "d" * 64,
                None,
            ),
        )

    repository = SQLiteRepository(path)

    migrated = repository.load_run("early-run")
    assert migrated is not None
    assert migrated.git_commit == "c" * 40
    assert migrated.code_hash is None
    assert repository.load_notification_outcomes("early-run") == ()


def test_sqlite_run_observation_and_decision_batches_are_idempotent_and_atomic(
    tmp_path: Path,
) -> None:
    from trading_research.adapters.persistence.sqlite import (
        PersistenceConflictError,
        SQLiteRepository,
    )
    from trading_research.application.ports import DecisionRecord, ObservationRecord, RunRecord

    repository = SQLiteRepository(tmp_path / "evidence.db")
    run = RunRecord(
        run_id="run-1",
        status="RUNNING",
        started_at_ms=1000,
        completed_at_ms=None,
        strategy_name="research",
        strategy_version="1",
        strategy_config_hash="a" * 64,
        provider_id="binance_usdm",
        market_type="LINEAR_PERPETUAL",
        market_config_hash="b" * 64,
        git_commit=None,
        code_hash="c" * 64,
        data_start_open_ms=0,
        data_end_close_ms=59_999,
        data_hash="d" * 64,
        error=None,
    )
    repository.store_run(run)
    repository.store_run(run)

    observation = ObservationRecord(
        run_id="run-1",
        instrument_id="BTC-USDT-PERP",
        signal_id="signal.one",
        status="PASS",
        event_time_ms=59_999,
        known_time_ms=59_999,
        reason="confirmed",
        payload={"value": "1.25"},
    )
    repository.store_observations((observation,))
    repository.store_observations((observation,))
    with pytest.raises(PersistenceConflictError, match="PERSISTENCE_CONFLICT"):
        repository.store_observations((replace(observation, reason="changed"),))

    second = replace(observation, signal_id="signal.two")
    missing_parent = replace(observation, run_id="missing", signal_id="signal.three")
    with pytest.raises(sqlite3.IntegrityError):
        repository.store_observations((second, missing_parent))
    assert repository.load_observations("run-1") == (observation,)

    decision = DecisionRecord(
        run_id="run-1",
        instrument_id="BTC-USDT-PERP",
        decision_status="NO_TRADE",
        direction=None,
        entry_text=None,
        stop_text=None,
        target_text=None,
        first_failed_signal="signal.two",
        payload={"ordered": ["signal.one", "signal.two"]},
    )
    repository.store_decisions((decision,))
    repository.store_decisions((decision,))
    assert repository.load_decisions("run-1") == (decision,)

    succeeded = repository.finish_run("run-1", "SUCCEEDED", completed_at_ms=2000, error=None)
    assert succeeded.status == "SUCCEEDED"
    assert (
        repository.finish_run("run-1", "SUCCEEDED", completed_at_ms=2000, error=None) == succeeded
    )
    with pytest.raises(PersistenceConflictError, match="PERSISTENCE_CONFLICT"):
        repository.finish_run("run-1", "FAILED", completed_at_ms=2000, error="late")


def test_sqlite_requires_exactly_one_valid_run_provenance(tmp_path: Path) -> None:
    from trading_research.adapters.persistence.sqlite import SQLiteRepository
    from trading_research.application.ports import RunRecord

    repository = SQLiteRepository(tmp_path / "provenance.db")
    valid = RunRecord(
        run_id="valid",
        status="RUNNING",
        started_at_ms=1,
        completed_at_ms=None,
        strategy_name="research",
        strategy_version="1",
        strategy_config_hash="a" * 64,
        provider_id="provider",
        market_type="LINEAR_PERPETUAL",
        market_config_hash="b" * 64,
        git_commit=None,
        code_hash="c" * 64,
        data_start_open_ms=0,
        data_end_close_ms=59_999,
        data_hash="d" * 64,
        error=None,
    )
    repository.store_run(valid)

    with pytest.raises(ValueError, match="new runs require code hash provenance"):
        repository.store_run(
            replace(valid, run_id="legacy-write", git_commit="e" * 40, code_hash=None)
        )

    invalid = (
        replace(valid, run_id="missing", code_hash=None),
        replace(valid, run_id="both", git_commit="e" * 40),
        replace(valid, run_id="uppercase", code_hash="F" * 64),
    )
    for run in invalid:
        with pytest.raises(sqlite3.IntegrityError):
            repository.store_run(run)


def test_sqlite_rejects_version_two_schema_without_canonical_provenance_constraint(
    tmp_path: Path,
) -> None:
    from trading_research.adapters.persistence.sqlite import DDL, SQLiteRepository

    path = tmp_path / "malformed-v2.db"
    malformed = DDL.replace("length(code_hash)=64", "length(code_hash)=63")
    with sqlite3.connect(path) as connection:
        connection.executescript(f"{malformed}\nPRAGMA user_version=2;")

    with pytest.raises(RuntimeError, match="unsupported or malformed SQLite schema"):
        SQLiteRepository(path)


def test_sqlite_canonicalizes_nested_immutable_payload_mappings(tmp_path: Path) -> None:
    from trading_research.adapters.persistence.sqlite import SQLiteRepository
    from trading_research.application.ports import ObservationRecord, RunRecord

    repository = SQLiteRepository(tmp_path / "nested-evidence.db")
    repository.store_run(
        RunRecord(
            run_id="nested-run",
            status="RUNNING",
            started_at_ms=1000,
            completed_at_ms=None,
            strategy_name="research",
            strategy_version="1",
            strategy_config_hash="a" * 64,
            provider_id="binance_usdm",
            market_type="LINEAR_PERPETUAL",
            market_config_hash="b" * 64,
            git_commit=None,
            code_hash="c" * 64,
            data_start_open_ms=0,
            data_end_close_ms=59_999,
            data_hash="d" * 64,
            error=None,
        )
    )
    observation = ObservationRecord(
        run_id="nested-run",
        instrument_id="BTC-USDT-PERP",
        signal_id="signal.nested",
        status="PASS",
        event_time_ms=59_999,
        known_time_ms=59_999,
        reason="nested evidence",
        payload={"evidence": MappingProxyType({"value": "1.25"})},
    )

    repository.store_observations((observation,))

    assert repository.load_observations("nested-run")[0].payload == {"evidence": {"value": "1.25"}}


@pytest.mark.parametrize("kind", ("observation", "decision", "mixed"))
def test_commit_run_rejects_evidence_owned_by_another_running_run_before_any_write(
    tmp_path: Path, kind: str
) -> None:
    from trading_research.adapters.persistence.sqlite import (
        PersistenceConflictError,
        SQLiteRepository,
    )
    from trading_research.application.ports import DecisionRecord, ObservationRecord, RunRecord

    repository = SQLiteRepository(tmp_path / "cross-run-evidence.db")

    def running_run(run_id: str) -> RunRecord:
        return RunRecord(
            run_id=run_id,
            status="RUNNING",
            started_at_ms=1000,
            completed_at_ms=None,
            strategy_name="research",
            strategy_version="1",
            strategy_config_hash="a" * 64,
            provider_id="binance_usdm",
            market_type="LINEAR_PERPETUAL",
            market_config_hash="b" * 64,
            git_commit=None,
            code_hash="c" * 64,
            data_start_open_ms=0,
            data_end_close_ms=59_999,
            data_hash="d" * 64,
            error=None,
        )

    def observation(run_id: str, signal_id: str) -> ObservationRecord:
        return ObservationRecord(
            run_id=run_id,
            instrument_id="BTC-USDT-PERP",
            signal_id=signal_id,
            status="PASS",
            event_time_ms=59_999,
            known_time_ms=59_999,
            reason="confirmed",
            payload={},
        )

    def decision(run_id: str) -> DecisionRecord:
        return DecisionRecord(
            run_id=run_id,
            instrument_id="BTC-USDT-PERP",
            decision_status="NO_TRADE",
            direction=None,
            entry_text=None,
            stop_text=None,
            target_text=None,
            first_failed_signal="signal.one",
            payload={},
        )

    repository.store_run(running_run("run-a"))
    repository.store_run(running_run("run-b"))
    observations = {
        "observation": (observation("run-b", "signal.b"),),
        "decision": (),
        "mixed": (observation("run-a", "signal.a"), observation("run-b", "signal.b")),
    }[kind]
    decisions = {
        "observation": (),
        "decision": (decision("run-b"),),
        "mixed": (decision("run-a"), decision("run-b")),
    }[kind]

    with pytest.raises(PersistenceConflictError, match="PERSISTENCE_CONFLICT"):
        repository.commit_run("run-a", observations, decisions, completed_at_ms=2000)

    assert repository.load_observations("run-a") == ()
    assert repository.load_decisions("run-a") == ()
    assert repository.load_observations("run-b") == ()
    assert repository.load_decisions("run-b") == ()
    run_a = repository.load_run("run-a")
    run_b = repository.load_run("run-b")
    assert run_a is not None
    assert run_b is not None
    assert run_a.status == "RUNNING"
    assert run_b.status == "RUNNING"
