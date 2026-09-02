"""Binance USD-M public closed-kline adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from smc_ict.application.ports import (
    KlinePage,
    KlineRequest,
    ProviderConfigurationError,
    ProviderPermanentError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTemporaryError,
)
from smc_ict.domain import ClosedCandle

from .transport import JsonRequester

RequestJson = Callable[[str, dict[str, object]], object]


class BinanceUsdmProvider:
    """Read-only adapter for Binance USD-M one-minute klines."""

    provider_id = "binance_usdm"
    _base_url = "https://fapi.binance.com"

    def __init__(
        self,
        request_json: RequestJson | None = None,
        *,
        page_limit: int = 1500,
    ) -> None:
        if type(page_limit) is not int or not 1 <= page_limit <= 1500:
            raise ProviderConfigurationError("Binance page limit must be an integer from 1 to 1500")
        self._request_json = request_json or JsonRequester(self._base_url)
        self._page_limit = page_limit
        self._server_time_snapshot_ms: int | None = None

    def validate_instrument(self, mapping: object) -> None:
        # Binance's kline endpoint rejects an invalid configured symbol. No separate
        # metadata gate is required by the accepted v1 contract.
        del mapping

    def server_time_ms(self) -> int:
        if self._server_time_snapshot_ms is not None:
            return self._server_time_snapshot_ms
        payload = self._request_json("/fapi/v1/time", {})
        if type(payload) is not dict or type(payload.get("serverTime")) is not int:
            raise ProviderProtocolError("Binance time response is malformed")
        value = cast(int, payload["serverTime"])
        if len(str(value)) != 13:
            raise ProviderProtocolError("Binance server time has ambiguous units")
        self._server_time_snapshot_ms = value
        return value

    def latest_closed_open_time_ms(self) -> int:
        server_time = self.server_time_ms()
        if server_time < 60_000:
            raise ProviderProtocolError("Binance server time has no closed canonical minute")
        return ((server_time - 60_000) // 60_000) * 60_000

    def fetch_page(self, request: KlineRequest) -> KlinePage:
        if request.provider_id != self.provider_id:
            raise ProviderConfigurationError("request provider does not match Binance adapter")
        payload = self._request_json(
            "/fapi/v1/klines",
            {
                "symbol": request.provider_symbol,
                "interval": "1m",
                "startTime": request.start_open_time_ms,
                "endTime": request.end_open_time_ms,
                "limit": self._page_limit,
            },
        )
        if type(payload) is dict and type(payload.get("code")) is int:
            code = cast(int, payload["code"])
            if code == -1003:
                raise ProviderRateLimitError(None)
            if code in {-1000, -1001, -1007}:
                raise ProviderTemporaryError("Binance reported a temporary provider failure")
            raise ProviderPermanentError("Binance rejected the public market-data request")
        if type(payload) is not list:
            raise ProviderProtocolError("Binance kline response is malformed")
        closed_boundary = self.latest_closed_open_time_ms()
        candles = tuple(self._normalize_row(row, request, closed_boundary) for row in payload)
        if not candles:
            raise ProviderProtocolError("Binance returned no progress for the requested range")
        next_start = candles[-1].open_time_ms + 60_000
        complete = next_start > request.end_open_time_ms
        return KlinePage(
            candles=candles,
            next_start_open_time_ms=None if complete else next_start,
            complete=complete,
        )

    @staticmethod
    def _normalize_row(row: object, request: KlineRequest, closed_boundary: int) -> ClosedCandle:
        if type(row) is not list or len(row) != 12:
            raise ProviderProtocolError("Binance kline row is malformed")
        open_time = row[0]
        close_time = row[6]
        trade_count = row[8]
        if (
            type(open_time) is not int
            or type(close_time) is not int
            or type(trade_count) is not int
        ):
            raise ProviderProtocolError("Binance kline row has ambiguous numeric units")
        if len(str(open_time)) != 13 or len(str(close_time)) != 13:
            raise ProviderProtocolError("Binance kline row has ambiguous timestamp units")
        if not request.start_open_time_ms <= open_time <= request.end_open_time_ms:
            raise ProviderProtocolError("Binance kline row is outside the requested range")
        if open_time > closed_boundary:
            raise ProviderProtocolError("Binance returned an open candle")
        try:
            return ClosedCandle(
                provider_id="binance_usdm",
                market_type=request.market_type,
                instrument_id=request.instrument_id,
                provider_symbol=request.provider_symbol,
                interval="1m",
                open_time_ms=open_time,
                close_time_ms=close_time,
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                base_volume=row[5],
                quote_volume=row[7],
                source_fields={
                    "trade_count": trade_count,
                    "taker_buy_base_volume": row[9],
                    "taker_buy_quote_volume": row[10],
                },
            )
        except (TypeError, ValueError) as exc:
            raise ProviderProtocolError(
                "Binance kline row violates the canonical contract"
            ) from exc
