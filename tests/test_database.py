from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType

import pytest


def test_sqlite_exact_schema_idempotent_pages_conflicts_and_contiguous_sync(tmp_path: Path) -> None:
    from smc_ict.adapters.persistence.sqlite import SourceConflictError, SQLiteRepository
    from smc_ict.domain import ClosedCandle

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

    path = tmp_path / "smc_ict.db"
    repository = SQLiteRepository(path)
    with sqlite3.connect(path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        }
        assert tables == {"candles_1m", "sync_state", "runs", "observations", "decisions"}
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
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


def test_sqlite_run_observation_and_decision_batches_are_idempotent_and_atomic(
    tmp_path: Path,
) -> None:
    from smc_ict.adapters.persistence.sqlite import PersistenceConflictError, SQLiteRepository
    from smc_ict.application.ports import DecisionRecord, ObservationRecord, RunRecord

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
        git_commit="c" * 40,
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


def test_sqlite_canonicalizes_nested_immutable_payload_mappings(tmp_path: Path) -> None:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository
    from smc_ict.application.ports import ObservationRecord, RunRecord

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
            git_commit="c" * 40,
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
    from smc_ict.adapters.persistence.sqlite import PersistenceConflictError, SQLiteRepository
    from smc_ict.application.ports import DecisionRecord, ObservationRecord, RunRecord

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
            git_commit="c" * 40,
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
