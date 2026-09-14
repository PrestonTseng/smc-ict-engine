from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from trading_research.adapters.persistence.sqlite import SQLiteRepository
from trading_research.application.ports import InstrumentMapping, KlinePage, KlineRequest
from trading_research.domain import ClosedCandle


def _candle(
    provider_id: str,
    open_time_ms: int,
    *,
    instrument_id: str = "BTC-USDT-PERP",
    provider_symbol: str = "BTCUSDT",
) -> ClosedCandle:
    source_fields: dict[str, int | str]
    if provider_id == "binance_usdm":
        source_fields = {
            "trade_count": 1,
            "taker_buy_base_volume": "0.5",
            "taker_buy_quote_volume": "50",
        }
    else:
        source_fields = {"contract_volume": "1"}
    return ClosedCandle(
        provider_id=provider_id,
        market_type="LINEAR_PERPETUAL",
        instrument_id=instrument_id,
        provider_symbol=provider_symbol,
        interval="1m",
        open_time_ms=open_time_ms,
        close_time_ms=open_time_ms + 59_999,
        open="100",
        high="102",
        low="99",
        close="101",
        base_volume="1",
        quote_volume="100",
        source_fields=source_fields,
    )


class _Provider:
    def __init__(self, provider_id: str = "binance_usdm", *, fail_after: int | None = None) -> None:
        self.provider_id = provider_id
        self.fail_after = fail_after
        self.requests: list[KlineRequest] = []

    def validate_instrument(self, mapping: object) -> None:
        assert isinstance(mapping, InstrumentMapping)
        assert mapping.instrument_id.endswith("-USDT-PERP")

    def server_time_ms(self) -> int:
        return 300_000

    def latest_closed_open_time_ms(self) -> int:
        return 240_000

    def fetch_page(self, request: KlineRequest) -> KlinePage:
        self.requests.append(request)
        if self.fail_after is not None and len(self.requests) > self.fail_after:
            raise RuntimeError("forced interruption")
        candles = tuple(
            _candle(
                self.provider_id,
                open_time,
                instrument_id=request.instrument_id,
                provider_symbol=request.provider_symbol,
            )
            for open_time in range(
                request.start_open_time_ms,
                request.end_open_time_ms + 1,
                60_000,
            )
        )
        return KlinePage(candles, None, True)


def test_historical_sync_fetches_only_exact_missing_subranges(tmp_path: Path) -> None:
    from trading_research.application.historical_sync import HistoricalRangeSyncService

    repository = SQLiteRepository(tmp_path / "runtime.sqlite3")
    repository.store_candle_page(
        (_candle("binance_usdm", 60_000), _candle("binance_usdm", 180_000)),
        successful_sync_ms=300_000,
        required_start_open_ms=60_000,
    )
    provider = _Provider()

    result = HistoricalRangeSyncService(provider, repository).sync_range(
        InstrumentMapping("BTC-USDT-PERP", "BTCUSDT"), 0, 180_000
    )

    assert [(item.start_open_time_ms, item.end_open_time_ms) for item in provider.requests] == [
        (0, 0),
        (120_000, 120_000),
    ]
    assert result.existing_rows == 2
    assert result.fetched_rows == 2
    assert result.total_rows == 4
    assert result.gap_count == 0
    assert [candle.open_time_ms for candle in result.candles] == [0, 60_000, 120_000, 180_000]


def test_historical_sync_all_returns_a_deterministic_provider_neutral_receipt(
    tmp_path: Path,
) -> None:
    from trading_research.application.historical_sync import HistoricalRangeSyncService

    repository = SQLiteRepository(tmp_path / "runtime.sqlite3")
    provider = _Provider("okx_swap")

    receipt = HistoricalRangeSyncService(provider, repository).sync_all(
        (
            InstrumentMapping("ETH-USDT-PERP", "ETH-USDT-SWAP"),
            InstrumentMapping("BTC-USDT-PERP", "BTC-USDT-SWAP"),
        ),
        0,
        60_000,
    )

    assert receipt.canonical_dict() == {
        "status": "SUCCEEDED",
        "provider": "okx_swap",
        "required_start_ms": 0,
        "required_end_ms": 60_000,
        "instruments": {
            "BTC-USDT-PERP": {
                "existing_rows": 0,
                "fetched_rows": 2,
                "total_rows": 2,
                "gap_count": 0,
                "data_hash": receipt.instruments["BTC-USDT-PERP"].data_hash,
            },
            "ETH-USDT-PERP": {
                "existing_rows": 0,
                "fetched_rows": 2,
                "total_rows": 2,
                "gap_count": 0,
                "data_hash": receipt.instruments["ETH-USDT-PERP"].data_hash,
            },
        },
    }
    assert [request.instrument_id for request in provider.requests] == [
        "BTC-USDT-PERP",
        "ETH-USDT-PERP",
    ]


def test_historical_sync_rejects_a_future_range_even_when_rows_already_exist(
    tmp_path: Path,
) -> None:
    from trading_research.application.historical_sync import HistoricalRangeSyncService

    repository = SQLiteRepository(tmp_path / "runtime.sqlite3")
    repository.store_candle_page(
        (_candle("binance_usdm", 300_000),),
        successful_sync_ms=300_000,
        required_start_open_ms=300_000,
    )

    with pytest.raises(ValueError, match="open canonical minute"):
        HistoricalRangeSyncService(_Provider(), repository).sync_range(
            InstrumentMapping("BTC-USDT-PERP", "BTCUSDT"), 300_000, 300_000
        )


def test_interrupted_historical_sync_resumes_from_only_the_uncommitted_minutes(
    tmp_path: Path,
) -> None:
    from trading_research.application.historical_sync import HistoricalRangeSyncService

    class InterruptedProvider(_Provider):
        def fetch_page(self, request: KlineRequest) -> KlinePage:
            self.requests.append(request)
            if len(self.requests) == 1:
                return KlinePage(
                    (
                        _candle(self.provider_id, request.start_open_time_ms),
                        _candle(self.provider_id, request.start_open_time_ms + 60_000),
                    ),
                    request.start_open_time_ms + 120_000,
                    False,
                )
            raise RuntimeError("forced interruption")

    repository = SQLiteRepository(tmp_path / "runtime.sqlite3")
    interrupted = InterruptedProvider()
    with pytest.raises(RuntimeError, match="forced interruption"):
        HistoricalRangeSyncService(interrupted, repository).sync_range(
            InstrumentMapping("BTC-USDT-PERP", "BTCUSDT"), 0, 180_000
        )

    resumed = _Provider()
    result = HistoricalRangeSyncService(resumed, repository).sync_range(
        InstrumentMapping("BTC-USDT-PERP", "BTCUSDT"), 0, 180_000
    )

    assert [
        (request.start_open_time_ms, request.end_open_time_ms) for request in resumed.requests
    ] == [(120_000, 180_000)]
    assert result.existing_rows == 2
    assert result.fetched_rows == 2


def test_historical_sync_rejects_a_gap_remaining_after_provider_completion() -> None:
    from trading_research.application.historical_sync import HistoricalRangeSyncService

    class DroppingRepository:
        def store_candle_page(
            self,
            candles: Sequence[ClosedCandle],
            *,
            successful_sync_ms: int,
            required_start_open_ms: int,
        ) -> None:
            pass

        def load_candles(
            self,
            provider_id: str,
            market_type: str,
            instrument_id: str,
            start_open_ms: int,
            end_open_ms: int,
        ) -> tuple[ClosedCandle, ...]:
            return ()

    with pytest.raises(ValueError, match="not contiguous after synchronization"):
        HistoricalRangeSyncService(_Provider(), DroppingRepository()).sync_range(
            InstrumentMapping("BTC-USDT-PERP", "BTCUSDT"), 0, 0
        )


def test_standalone_sync_bootstraps_the_global_database_without_strategy_or_backtest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research.composition import runtime_services

    config_folder = tmp_path / "config"
    data_folder = tmp_path / "data"
    config_folder.mkdir()
    (config_folder / "market-data.yaml").write_text(
        "market_data:\n"
        "  provider: binance_usdm\n"
        "  market_type: LINEAR_PERPETUAL\n"
        "  instruments:\n"
        "    BTC-USDT-PERP: BTCUSDT\n"
        "    ETH-USDT-PERP: ETHUSDT\n",
        encoding="utf-8",
    )
    provider = _Provider()
    monkeypatch.setenv("CONFIG_FOLDER", str(config_folder))
    monkeypatch.setenv("DATA_FOLDER", str(data_folder))
    monkeypatch.setattr(runtime_services, "build_market_provider", lambda *_args: provider)

    receipt = runtime_services.sync_historical_range(
        start="1970-01-01T00:00:00Z",
        end="1970-01-01T00:01:00Z",
    )

    assert receipt.provider == "binance_usdm"
    assert tuple(receipt.instruments) == ("BTC-USDT-PERP", "ETH-USDT-PERP")
    assert (data_folder / "trading_research.db").is_file()
    assert [request.provider_symbol for request in provider.requests] == ["BTCUSDT", "ETHUSDT"]


def test_standalone_sync_rejects_invalid_range_before_provider_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research.composition import runtime_services

    config_folder = tmp_path / "config"
    config_folder.mkdir()
    (config_folder / "market-data.yaml").write_text(
        "market_data:\n"
        "  provider: binance_usdm\n"
        "  market_type: LINEAR_PERPETUAL\n"
        "  instruments: {BTC-USDT-PERP: BTCUSDT}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("CONFIG_FOLDER", str(config_folder))
    monkeypatch.setenv("DATA_FOLDER", str(tmp_path / "data"))
    monkeypatch.setattr(
        runtime_services,
        "build_market_provider",
        lambda *_args: (_ for _ in ()).throw(AssertionError("provider composed before validation")),
    )

    with pytest.raises(ValueError, match="canonical UTC minute"):
        runtime_services.sync_historical_range(start="invalid", end="1970-01-01T00:01:00Z")


def test_standalone_sync_rejects_reversed_range_before_provider_composition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from trading_research.composition import runtime_services

    monkeypatch.setenv("CONFIG_FOLDER", str(tmp_path / "config"))
    monkeypatch.setenv("DATA_FOLDER", str(tmp_path / "data"))
    monkeypatch.setattr(runtime_services, "load_market_data", lambda _path: object())
    monkeypatch.setattr(
        runtime_services,
        "build_market_provider",
        lambda *_args: (_ for _ in ()).throw(AssertionError("provider composed before validation")),
    )

    with pytest.raises(ValueError, match="start must not follow end"):
        runtime_services.sync_historical_range(
            start="1970-01-01T00:01:00Z", end="1970-01-01T00:00:00Z"
        )
