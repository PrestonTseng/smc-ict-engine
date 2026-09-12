"""Immutable closed-candle contract and deterministic data hashing."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType

from .values import DecimalText, InstrumentId, Timeframe

_MARKET_TYPE = "LINEAR_PERPETUAL"
_PROVIDER_EXTENSIONS = {
    "binance_usdm": frozenset({"trade_count", "taker_buy_base_volume", "taker_buy_quote_volume"}),
    "okx_swap": frozenset({"contract_volume"}),
}


@dataclass(frozen=True, slots=True)
class ClosedCandle:
    """One validated, closed, canonical one-minute market-data row."""

    provider_id: str
    market_type: str
    instrument_id: str
    provider_symbol: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: str
    high: str
    low: str
    close: str
    base_volume: str
    quote_volume: str | None
    source_fields: Mapping[str, bool | int | str | None]

    def __post_init__(self) -> None:
        if type(self.provider_id) is not str or self.provider_id not in _PROVIDER_EXTENSIONS:
            raise ValueError("unknown provider identifier")
        if self.market_type != _MARKET_TYPE:
            raise ValueError("market type must be LINEAR_PERPETUAL")
        object.__setattr__(self, "instrument_id", str(InstrumentId(self.instrument_id)))
        if type(self.provider_symbol) is not str or not 1 <= len(self.provider_symbol) <= 64:
            raise ValueError("invalid provider symbol")
        if Timeframe(self.interval) != "1m":
            raise ValueError("stored candle interval must be 1m")
        if type(self.open_time_ms) is not int or type(self.close_time_ms) is not int:
            raise TypeError("candle times must be integers; Booleans are not integers")
        if self.open_time_ms < 0 or self.open_time_ms % 60_000 != 0:
            raise ValueError("open time must align to a canonical minute")
        if self.close_time_ms != self.open_time_ms + 59_999:
            raise ValueError("closed candle must end 59,999 ms after open")
        for field in ("open", "high", "low", "close", "base_volume"):
            object.__setattr__(self, field, str(DecimalText(getattr(self, field))))
        if self.quote_volume is not None:
            object.__setattr__(self, "quote_volume", str(DecimalText(self.quote_volume)))
        low = DecimalText(self.low).decimal
        high = DecimalText(self.high).decimal
        opening = DecimalText(self.open).decimal
        closing = DecimalText(self.close).decimal
        if any(price <= 0 for price in (opening, high, low, closing)):
            raise ValueError("OHLC prices must be positive")
        if low > high or not low <= opening <= high or not low <= closing <= high:
            raise ValueError("OHLC values violate low/high relations")
        object.__setattr__(self, "source_fields", self._validated_source_fields())

    def _validated_source_fields(self) -> Mapping[str, bool | int | str | None]:
        if type(self.source_fields) is not dict and not isinstance(self.source_fields, Mapping):
            raise TypeError("source fields must be a mapping")
        values = dict(self.source_fields)
        expected = _PROVIDER_EXTENSIONS[self.provider_id]
        if set(values) != expected:
            raise ValueError("source fields do not match the provider extension schema")
        if self.provider_id == "binance_usdm":
            count = values["trade_count"]
            if type(count) is not int or count < 0:
                raise TypeError("trade_count must be a non-negative integer")
            for key in ("taker_buy_base_volume", "taker_buy_quote_volume"):
                values[key] = str(DecimalText(values[key]))  # type: ignore[arg-type]
        else:
            values["contract_volume"] = str(DecimalText(values["contract_volume"]))  # type: ignore[arg-type]
        return MappingProxyType(values)

    @property
    def primary_key(self) -> tuple[str, str, str, str, int]:
        return (
            self.provider_id,
            self.market_type,
            self.instrument_id,
            self.interval,
            self.open_time_ms,
        )

    def canonical_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "market_type": self.market_type,
            "instrument_id": self.instrument_id,
            "provider_symbol": self.provider_symbol,
            "interval": self.interval,
            "open_time_ms": self.open_time_ms,
            "close_time_ms": self.close_time_ms,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "base_volume": self.base_volume,
            "quote_volume": self.quote_volume,
            "source_fields": dict(self.source_fields),
        }


def hash_candles(candles: Sequence[ClosedCandle]) -> str:
    """Hash candle rows in canonical primary-key order with the v2 domain."""
    rows = [
        candle.canonical_dict() for candle in sorted(candles, key=lambda item: item.primary_key)
    ]
    encoded = json.dumps(
        rows, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return sha256(b"candles-v2\0" + encoded).hexdigest()
