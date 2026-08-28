"""OKX linear USDT swap public closed-kline adapter."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast

from smc_ict.application.ports import (
    InstrumentMapping,
    KlinePage,
    KlineRequest,
    ProviderConfigurationError,
    ProviderInstrumentError,
    ProviderPermanentError,
    ProviderProtocolError,
    ProviderRateLimitError,
    ProviderTemporaryError,
)
from smc_ict.domain import ClosedCandle

from .transport import JsonRequester

RequestJson = Callable[[str, dict[str, object]], object]


class OkxSwapProvider:
    """Read-only adapter for confirmed OKX linear USDT swap one-minute candles."""

    provider_id = "okx_swap"
    _base_url = "https://www.okx.com"

    def __init__(
        self,
        request_json: RequestJson | None = None,
        *,
        page_limit: int = 100,
    ) -> None:
        if type(page_limit) is not int or not 1 <= page_limit <= 100:
            raise ProviderConfigurationError("OKX page limit must be an integer from 1 to 100")
        self._request_json = request_json or JsonRequester(self._base_url)
        self._page_limit = page_limit
        self._validated_symbols: set[str] = set()

    def validate_instrument(self, mapping: object) -> None:
        if not isinstance(mapping, InstrumentMapping):
            raise ProviderConfigurationError("OKX instrument mapping is invalid")
        payload = self._okx_data(
            self._request_json(
                "/api/v5/public/instruments",
                {"instType": "SWAP", "instId": mapping.provider_symbol},
            ),
            "instrument",
        )
        if len(payload) != 1 or type(payload[0]) is not dict:
            raise ProviderInstrumentError("OKX instrument metadata is missing or ambiguous")
        metadata = cast(dict[object, object], payload[0])
        expected = {
            "instType": "SWAP",
            "instId": mapping.provider_symbol,
            "ctType": "linear",
            "settleCcy": "USDT",
        }
        if any(metadata.get(key) != value for key, value in expected.items()):
            raise ProviderInstrumentError("OKX instrument is not the configured linear USDT swap")
        self._validated_symbols.add(mapping.provider_symbol)

    def server_time_ms(self) -> int:
        data = self._okx_data(self._request_json("/api/v5/public/time", {}), "time")
        if len(data) != 1 or type(data[0]) is not dict:
            raise ProviderProtocolError("OKX time response is malformed")
        raw = cast(dict[object, object], data[0]).get("ts")
        return self._millisecond_text(raw, "OKX server time")

    def latest_closed_open_time_ms(self) -> int:
        server_time = self.server_time_ms()
        if server_time < 60_000:
            raise ProviderProtocolError("OKX server time has no closed canonical minute")
        return ((server_time - 60_000) // 60_000) * 60_000

    def fetch_page(self, request: KlineRequest) -> KlinePage:
        if request.provider_id != self.provider_id:
            raise ProviderConfigurationError("request provider does not match OKX adapter")
        if request.provider_symbol not in self._validated_symbols:
            raise ProviderInstrumentError(
                "OKX instrument metadata must be validated before candles"
            )
        data = self._okx_data(
            self._request_json(
                "/api/v5/market/history-candles",
                {
                    "instId": request.provider_symbol,
                    "bar": "1m",
                    "after": str(request.end_open_time_ms + 60_000),
                    "limit": str(self._page_limit),
                },
            ),
            "history candle",
        )
        closed_boundary = self.latest_closed_open_time_ms()
        in_range: list[object] = []
        saw_older_row = False
        for row in data:
            if type(row) is not list or len(row) != 9:
                raise ProviderProtocolError("OKX candle row is malformed")
            open_time = self._millisecond_text(row[0], "OKX candle open time")
            if open_time > request.end_open_time_ms:
                raise ProviderProtocolError("OKX candle row is outside the requested upper bound")
            if open_time < request.start_open_time_ms:
                saw_older_row = True
                continue
            in_range.append(row)
        candles = tuple(
            sorted(
                (self._normalize_row(row, request, closed_boundary) for row in in_range),
                key=lambda candle: candle.open_time_ms,
            )
        )
        if not candles:
            raise ProviderProtocolError("OKX returned no progress for the requested range")
        earliest = candles[0].open_time_ms
        complete = (
            saw_older_row or earliest <= request.start_open_time_ms or len(data) < self._page_limit
        )
        return KlinePage(
            candles=candles,
            next_start_open_time_ms=None if complete else earliest - 60_000,
            complete=complete,
        )

    @classmethod
    def _normalize_row(
        cls, row: object, request: KlineRequest, closed_boundary: int
    ) -> ClosedCandle:
        if type(row) is not list or len(row) != 9:
            raise ProviderProtocolError("OKX candle row is malformed")
        open_time = cls._millisecond_text(row[0], "OKX candle open time")
        if not request.start_open_time_ms <= open_time <= request.end_open_time_ms:
            raise ProviderProtocolError("OKX candle row is outside the requested range")
        if row[8] != "1" or open_time > closed_boundary:
            raise ProviderProtocolError("OKX returned an incomplete candle")
        try:
            return ClosedCandle(
                provider_id="okx_swap",
                market_type=request.market_type,
                instrument_id=request.instrument_id,
                provider_symbol=request.provider_symbol,
                interval="1m",
                open_time_ms=open_time,
                close_time_ms=open_time + 59_999,
                open=row[1],
                high=row[2],
                low=row[3],
                close=row[4],
                base_volume=row[6],
                quote_volume=row[7],
                source_fields={"contract_volume": row[5]},
            )
        except (TypeError, ValueError) as exc:
            raise ProviderProtocolError("OKX candle row violates the canonical contract") from exc

    @staticmethod
    def _millisecond_text(value: object, field: str) -> int:
        if type(value) is not str or len(value) != 13 or not value.isdigit():
            raise ProviderProtocolError(f"{field} has ambiguous units")
        return int(value)

    @staticmethod
    def _okx_data(payload: object, operation: str) -> list[object]:
        if type(payload) is not dict:
            raise ProviderProtocolError(f"OKX {operation} response is malformed")
        code = payload.get("code")
        if code != "0":
            if code == "50011":
                raise ProviderRateLimitError(None)
            if code in {"50013", "50026"}:
                raise ProviderTemporaryError(f"OKX {operation} temporarily unavailable")
            if type(code) is str:
                raise ProviderPermanentError(f"OKX rejected {operation} request")
            raise ProviderProtocolError(f"OKX {operation} response is malformed")
        data = payload.get("data")
        if type(data) is not list:
            raise ProviderProtocolError(f"OKX {operation} response is malformed")
        return data
