from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "provider_fixtures"


def test_binance_normalizes_documented_closed_kline_shape_exactly() -> None:
    from smc_ict.adapters.market_data.binance_usdm import BinanceUsdmProvider
    from smc_ict.application.ports import KlineRequest

    payload = json.loads((FIXTURES / "binance_usdm_klines.json").read_text(encoding="utf-8"))
    calls: list[tuple[str, dict[str, object]]] = []

    def request_json(path: str, parameters: dict[str, object]) -> object:
        calls.append((path, parameters))
        if path == "/fapi/v1/time":
            return {"serverTime": 1722470460000}
        return payload

    provider = BinanceUsdmProvider(request_json=request_json, page_limit=100)
    page = provider.fetch_page(
        KlineRequest(
            provider_id="binance_usdm",
            market_type="LINEAR_PERPETUAL",
            instrument_id="BTC-USDT-PERP",
            provider_symbol="BTCUSDT",
            interval="1m",
            start_open_time_ms=1722470400000,
            end_open_time_ms=1722470400000,
        )
    )

    assert calls == [
        (
            "/fapi/v1/klines",
            {
                "symbol": "BTCUSDT",
                "interval": "1m",
                "startTime": 1722470400000,
                "endTime": 1722470400000,
                "limit": 100,
            },
        ),
        ("/fapi/v1/time", {}),
    ]
    assert page.complete is True
    assert page.next_start_open_time_ms is None
    assert page.candles[0].canonical_dict() == {
        "provider_id": "binance_usdm",
        "market_type": "LINEAR_PERPETUAL",
        "instrument_id": "BTC-USDT-PERP",
        "provider_symbol": "BTCUSDT",
        "interval": "1m",
        "open_time_ms": 1722470400000,
        "close_time_ms": 1722470459999,
        "open": "64123.45",
        "high": "64200",
        "low": "64000.1",
        "close": "64180.25",
        "base_volume": "12.34",
        "quote_volume": "791999.1234",
        "source_fields": {
            "trade_count": 321,
            "taker_buy_base_volume": "6.12",
            "taker_buy_quote_volume": "392000.5",
        },
    }


def test_okx_metadata_gate_and_documented_closed_kline_shape() -> None:
    from smc_ict.adapters.market_data.okx_swap import OkxSwapProvider
    from smc_ict.application.ports import InstrumentMapping, KlineRequest

    candles = json.loads((FIXTURES / "okx_history_candles.json").read_text(encoding="utf-8"))
    instruments = json.loads((FIXTURES / "okx_instruments.json").read_text(encoding="utf-8"))
    calls: list[tuple[str, dict[str, object]]] = []

    def request_json(path: str, parameters: dict[str, object]) -> object:
        calls.append((path, parameters))
        if path == "/api/v5/public/instruments":
            return instruments
        if path == "/api/v5/public/time":
            return {"code": "0", "msg": "", "data": [{"ts": "1722470460000"}]}
        return candles

    provider = OkxSwapProvider(request_json=request_json, page_limit=100)
    provider.validate_instrument(InstrumentMapping("BTC-USDT-PERP", "BTC-USDT-SWAP"))
    page = provider.fetch_page(
        KlineRequest(
            provider_id="okx_swap",
            market_type="LINEAR_PERPETUAL",
            instrument_id="BTC-USDT-PERP",
            provider_symbol="BTC-USDT-SWAP",
            interval="1m",
            start_open_time_ms=1722470400000,
            end_open_time_ms=1722470400000,
        )
    )

    assert calls == [
        (
            "/api/v5/public/instruments",
            {"instType": "SWAP", "instId": "BTC-USDT-SWAP"},
        ),
        (
            "/api/v5/market/history-candles",
            {
                "instId": "BTC-USDT-SWAP",
                "bar": "1m",
                "after": "1722470460000",
                "limit": "100",
            },
        ),
        ("/api/v5/public/time", {}),
    ]
    assert page.complete is True
    assert page.candles[0].canonical_dict() == {
        "provider_id": "okx_swap",
        "market_type": "LINEAR_PERPETUAL",
        "instrument_id": "BTC-USDT-PERP",
        "provider_symbol": "BTC-USDT-SWAP",
        "interval": "1m",
        "open_time_ms": 1722470400000,
        "close_time_ms": 1722470459999,
        "open": "64123.45",
        "high": "64200",
        "low": "64000.1",
        "close": "64180.25",
        "base_volume": "0.117",
        "quote_volume": "7500.125",
        "source_fields": {"contract_volume": "7.5"},
    }


def test_okx_final_page_trims_older_provider_spill_rows() -> None:
    from smc_ict.adapters.market_data.okx_swap import OkxSwapProvider
    from smc_ict.application.ports import InstrumentMapping, KlineRequest

    row = json.loads((FIXTURES / "okx_history_candles.json").read_text(encoding="utf-8"))["data"][0]

    def request_json(path: str, parameters: dict[str, object]) -> object:
        if path.endswith("instruments"):
            return {
                "code": "0",
                "msg": "",
                "data": [
                    {
                        "instType": "SWAP",
                        "instId": "BTC-USDT-SWAP",
                        "ctType": "linear",
                        "settleCcy": "USDT",
                    }
                ],
            }
        if path.endswith("time"):
            return {"code": "0", "msg": "", "data": [{"ts": "1722470460000"}]}
        older = [str(1722470340000), *row[1:]]
        return {"code": "0", "msg": "", "data": [row, older]}

    provider = OkxSwapProvider(request_json=request_json, page_limit=2)
    provider.validate_instrument(InstrumentMapping("BTC-USDT-PERP", "BTC-USDT-SWAP"))

    page = provider.fetch_page(
        KlineRequest(
            "okx_swap",
            "LINEAR_PERPETUAL",
            "BTC-USDT-PERP",
            "BTC-USDT-SWAP",
            "1m",
            1722470400000,
            1722470400000,
        )
    )

    assert [candle.open_time_ms for candle in page.candles] == [1722470400000]
    assert page.complete is True
    assert page.next_start_open_time_ms is None


def test_generic_sync_paginates_and_rejects_non_contiguous_or_conflicting_pages() -> None:
    from smc_ict.application.market_sync import MarketSyncService
    from smc_ict.application.ports import InstrumentMapping, KlinePage
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
            base_volume="1",
            quote_volume="100",
            source_fields={
                "trade_count": 1,
                "taker_buy_base_volume": "0.5",
                "taker_buy_quote_volume": "50",
            },
        )

    class Provider:
        provider_id = "binance_usdm"

        def __init__(self, pages: list[KlinePage]) -> None:
            self.pages = pages
            self.requests: list[object] = []

        def validate_instrument(self, mapping: object) -> None:
            assert mapping == InstrumentMapping("BTC-USDT-PERP", "BTCUSDT")

        def server_time_ms(self) -> int:
            return 180_000

        def latest_closed_open_time_ms(self) -> int:
            return 120_000

        def fetch_page(self, request: object) -> KlinePage:
            self.requests.append(request)
            return self.pages.pop(0)

    class Repository:
        def __init__(self) -> None:
            self.pages: list[tuple[ClosedCandle, ...]] = []

        def store_candle_page(
            self,
            candles: tuple[ClosedCandle, ...],
            *,
            successful_sync_ms: int,
            required_start_open_ms: int,
        ) -> None:
            assert successful_sync_ms == 180_000
            assert required_start_open_ms == 0
            self.pages.append(candles)

    first = KlinePage((candle(0), candle(60_000)), 120_000, False)
    second = KlinePage((candle(120_000),), None, True)
    provider = Provider([first, second])
    repository = Repository()
    stored = MarketSyncService(provider, repository).sync_range(
        InstrumentMapping("BTC-USDT-PERP", "BTCUSDT"), 0, 120_000
    )

    assert [item.open_time_ms for item in stored] == [0, 60_000, 120_000]
    assert [page[0].open_time_ms for page in repository.pages] == [0, 120_000]
    assert [request.start_open_time_ms for request in provider.requests] == [0, 120_000]

    bad_page = KlinePage((candle(0), candle(120_000)), None, True)
    with pytest.raises(ValueError, match="contiguous"):
        MarketSyncService(Provider([bad_page]), Repository()).sync_range(
            InstrumentMapping("BTC-USDT-PERP", "BTCUSDT"), 0, 120_000
        )

    conflict = KlinePage((candle(0), replace(candle(0), close="100")), None, True)
    with pytest.raises(ValueError, match="conflicting duplicate"):
        MarketSyncService(Provider([conflict]), Repository()).sync_range(
            InstrumentMapping("BTC-USDT-PERP", "BTCUSDT"), 0, 0
        )


def test_switching_only_market_data_config_selects_the_concrete_provider() -> None:
    from smc_ict.adapters.market_data.binance_usdm import BinanceUsdmProvider
    from smc_ict.adapters.market_data.okx_swap import OkxSwapProvider
    from smc_ict.composition import build_market_provider, market_data_composition_root
    from smc_ict.configuration import load_market_data

    root = market_data_composition_root()
    binance = build_market_provider(
        load_market_data(Path("config/market-data.binance-usdm.yaml")), root
    )
    okx = build_market_provider(load_market_data(Path("config/market-data.okx-swap.yaml")), root)

    assert isinstance(binance, BinanceUsdmProvider)
    assert isinstance(okx, OkxSwapProvider)
    assert root.repositories.resolve("sqlite").__name__ == "SQLiteRepository"


def test_provider_error_bodies_are_translated_without_retaining_remote_payloads() -> None:
    from smc_ict.adapters.market_data.binance_usdm import BinanceUsdmProvider
    from smc_ict.adapters.market_data.okx_swap import OkxSwapProvider
    from smc_ict.application.ports import (
        InstrumentMapping,
        KlineRequest,
        ProviderPermanentError,
        ProviderRateLimitError,
    )

    request = KlineRequest(
        provider_id="binance_usdm",
        market_type="LINEAR_PERPETUAL",
        instrument_id="BTC-USDT-PERP",
        provider_symbol="BTCUSDT",
        interval="1m",
        start_open_time_ms=0,
        end_open_time_ms=0,
    )

    with pytest.raises(ProviderPermanentError) as binance_error:
        BinanceUsdmProvider(
            request_json=lambda path, parameters: {"code": -1121, "msg": "remote-secret"}
        ).fetch_page(request)
    assert "remote-secret" not in str(binance_error.value)

    okx = OkxSwapProvider(
        request_json=lambda path, parameters: {
            "code": "0",
            "msg": "",
            "data": [
                {
                    "instType": "SWAP",
                    "instId": "BTC-USDT-SWAP",
                    "ctType": "linear",
                    "settleCcy": "USDT",
                }
            ],
        }
        if path.endswith("instruments")
        else {"code": "50011", "msg": "remote-secret", "data": []}
    )
    okx.validate_instrument(InstrumentMapping("BTC-USDT-PERP", "BTC-USDT-SWAP"))
    with pytest.raises(ProviderRateLimitError) as okx_error:
        okx.fetch_page(replace(request, provider_id="okx_swap", provider_symbol="BTC-USDT-SWAP"))
    assert "remote-secret" not in str(okx_error.value)


def test_binance_rejects_unit_ambiguous_timestamps_even_when_minute_aligned() -> None:
    from smc_ict.adapters.market_data.binance_usdm import BinanceUsdmProvider
    from smc_ict.application.ports import KlineRequest, ProviderProtocolError

    ambiguous_open = 1_800_000_000
    row = [
        ambiguous_open,
        "100",
        "101",
        "99",
        "100",
        "1",
        ambiguous_open + 59_999,
        "100",
        1,
        "0.5",
        "50",
        "0",
    ]

    def request_json(path: str, parameters: dict[str, object]) -> object:
        if path.endswith("time"):
            return {"serverTime": ambiguous_open + 60_000}
        return [row]

    with pytest.raises(ProviderProtocolError, match="ambiguous"):
        BinanceUsdmProvider(request_json=request_json).fetch_page(
            KlineRequest(
                provider_id="binance_usdm",
                market_type="LINEAR_PERPETUAL",
                instrument_id="BTC-USDT-PERP",
                provider_symbol="BTCUSDT",
                interval="1m",
                start_open_time_ms=ambiguous_open,
                end_open_time_ms=ambiguous_open,
            )
        )


def test_json_transport_identifies_the_public_read_only_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.request import Request

    import smc_ict.adapters.market_data.transport as transport

    captured: list[Request] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"serverTime":1722470460000}'

    def fake_urlopen(request: Request, *, timeout: float) -> Response:
        assert timeout == 10.0
        captured.append(request)
        return Response()

    monkeypatch.setattr(transport, "urlopen", fake_urlopen)

    assert transport.JsonRequester("https://example.test")("/public/time", {}) == {
        "serverTime": 1722470460000
    }
    assert captured[0].get_header("Accept") == "application/json"
    assert captured[0].get_header("User-agent") == "smc-ict-engine/0.1 public-market-data"


def test_json_transport_retries_temporary_failures_with_bounded_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from urllib.error import URLError
    from urllib.request import Request

    import smc_ict.adapters.market_data.transport as transport

    attempts: list[Request] = []
    sleeps: list[float] = []

    class Response:
        def __enter__(self) -> Response:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self) -> bytes:
            return b'{"ok":true}'

    def flaky_urlopen(request: Request, *, timeout: float) -> Response:
        assert timeout == 3.0
        attempts.append(request)
        if len(attempts) < 3:
            raise URLError("temporary")
        return Response()

    monkeypatch.setattr(transport, "urlopen", flaky_urlopen)
    requester = transport.JsonRequester(
        "https://example.test",
        timeout_seconds=3.0,
        backoff_seconds=(0.25, 0.5),
        sleeper=sleeps.append,
    )

    assert requester("/public/time", {}) == {"ok": True}
    assert len(attempts) == 3
    assert sleeps == [0.25, 0.5]
