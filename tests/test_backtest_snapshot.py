from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from smc_ict.domain import ClosedCandle


def candle(minute: int, close: str = "100") -> ClosedCandle:
    return ClosedCandle(
        provider_id="okx_swap",
        market_type="LINEAR_PERPETUAL",
        instrument_id="BTC-USDT-PERP",
        provider_symbol="BTC-USDT-SWAP",
        interval="1m",
        open_time_ms=minute * 60_000,
        close_time_ms=minute * 60_000 + 59_999,
        open=close,
        high=close,
        low=close,
        close=close,
        base_volume="1",
        quote_volume="100",
        source_fields={"contract_volume": "1"},
    )


def test_online_backup_freezes_an_offline_read_only_range_without_mutating_source(
    tmp_path: Path,
) -> None:
    from smc_ict.adapters.persistence.sqlite import (
        SQLiteRepository,
        SQLiteSnapshotReader,
        create_sqlite_snapshot,
    )
    from smc_ict.domain import hash_candles

    source_path = tmp_path / "live.sqlite3"
    snapshot_path = tmp_path / "backtests" / "snapshot.sqlite3"
    repository = SQLiteRepository(source_path)
    initial = tuple(candle(minute) for minute in range(3))
    repository.store_candle_page(initial, successful_sync_ms=180_000, required_start_open_ms=0)
    with sqlite3.connect(source_path) as connection:
        counts_before = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("candles_1m", "sync_state", "runs", "observations", "decisions")
        )

    receipt = create_sqlite_snapshot(source_path, snapshot_path)
    repository.store_candle_page((candle(3),), successful_sync_ms=240_000, required_start_open_ms=0)
    reader = SQLiteSnapshotReader(snapshot_path)
    frozen = reader.load_candles(
        "okx_swap", "LINEAR_PERPETUAL", "BTC-USDT-PERP", 0, 180_000
    )

    assert frozen == initial
    assert receipt.data_hash == hash_candles(initial)
    assert receipt.candle_count == 3
    with sqlite3.connect(source_path) as connection:
        counts_after = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("candles_1m", "sync_state", "runs", "observations", "decisions")
        )
    assert counts_after == (4, *counts_before[1:])
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        with sqlite3.connect(f"file:{snapshot_path}?mode=ro", uri=True) as connection:
            connection.execute("DELETE FROM candles_1m")
    source_path.unlink()
    assert reader.load_candles(
        "okx_swap", "LINEAR_PERPETUAL", "BTC-USDT-PERP", 0, 180_000
    ) == initial


def test_snapshot_fails_closed_on_existing_destination(tmp_path: Path) -> None:
    from smc_ict.adapters.persistence.sqlite import SQLiteRepository, create_sqlite_snapshot

    source_path = tmp_path / "live.sqlite3"
    destination = tmp_path / "snapshot.sqlite3"
    SQLiteRepository(source_path)
    destination.write_bytes(b"do-not-overwrite")

    with pytest.raises(FileExistsError):
        create_sqlite_snapshot(source_path, destination)

    assert destination.read_bytes() == b"do-not-overwrite"
