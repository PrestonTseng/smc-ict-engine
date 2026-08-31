"""Deterministic complete-only UTC resampling for logical strategy roles."""

from __future__ import annotations

import json
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from hashlib import sha256
from itertools import pairwise
from types import MappingProxyType

from smc_ict.domain import ClosedCandle, DecimalText, InstrumentId, Timeframe


@dataclass(frozen=True, slots=True)
class DerivedCandle:
    """Provider-neutral completed higher-timeframe candle held only in memory."""

    instrument_id: str
    interval: str
    open_time_ms: int
    close_time_ms: int
    open: str
    high: str
    low: str
    close: str
    base_volume: str
    quote_volume: str | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", str(InstrumentId(self.instrument_id)))
        timeframe = Timeframe(self.interval)
        if timeframe == "1m":
            raise ValueError("derived candles require a higher timeframe")
        object.__setattr__(self, "interval", str(timeframe))
        duration = timeframe.duration_minutes * 60_000
        if self.open_time_ms < 0 or self.open_time_ms % duration != 0:
            raise ValueError("derived candle must align to a UTC timeframe boundary")
        if self.close_time_ms != self.open_time_ms + duration - 1:
            raise ValueError("derived candle must cover one complete timeframe")
        for field in ("open", "high", "low", "close", "base_volume"):
            object.__setattr__(self, field, str(DecimalText(getattr(self, field))))
        if self.quote_volume is not None:
            object.__setattr__(self, "quote_volume", str(DecimalText(self.quote_volume)))

    def canonical_dict(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


def resample_complete(
    candles: Sequence[ClosedCandle],
    timeframe: str,
    *,
    evaluation_time_ms: int | None = None,
) -> tuple[DerivedCandle, ...]:
    """Return only fully populated UTC buckets; reject ambiguous input series."""

    canonical = Timeframe(timeframe)
    canonical_timeframe = str(canonical)
    if canonical == "1m":
        raise ValueError("resampling requires a higher timeframe")
    if evaluation_time_ms is not None and type(evaluation_time_ms) is not int:
        raise TypeError("evaluation time must be an integer; Boolean is not integer")
    if evaluation_time_ms is not None and evaluation_time_ms < 0:
        raise ValueError("evaluation time must be non-negative")
    if not candles:
        return ()
    ordered = tuple(candles)
    identity = (
        ordered[0].provider_id,
        ordered[0].market_type,
        ordered[0].instrument_id,
        ordered[0].provider_symbol,
    )
    if any(
        (
            candle.provider_id,
            candle.market_type,
            candle.instrument_id,
            candle.provider_symbol,
        )
        != identity
        for candle in ordered
    ):
        raise ValueError("resampling input must have one market identity")
    if any(right.open_time_ms != left.open_time_ms + 60_000 for left, right in pairwise(ordered)):
        raise ValueError("resampling input must be monotonic and contiguous")

    minutes = canonical.duration_minutes
    duration = minutes * 60_000
    buckets: dict[int, list[ClosedCandle]] = defaultdict(list)
    for candle in ordered:
        if evaluation_time_ms is not None and candle.close_time_ms > evaluation_time_ms:
            continue
        bucket_start = candle.open_time_ms - candle.open_time_ms % duration
        buckets[bucket_start].append(candle)

    result: list[DerivedCandle] = []
    for bucket_start in sorted(buckets):
        rows = buckets[bucket_start]
        if (
            len(rows) != minutes
            or rows[0].open_time_ms != bucket_start
            or rows[-1].close_time_ms != bucket_start + duration - 1
        ):
            continue
        quote_volume = (
            None
            if any(row.quote_volume is None for row in rows)
            else _sum_decimal(row.quote_volume for row in rows if row.quote_volume is not None)
        )
        result.append(
            DerivedCandle(
                instrument_id=rows[0].instrument_id,
                interval=canonical_timeframe,
                open_time_ms=bucket_start,
                close_time_ms=bucket_start + duration - 1,
                open=rows[0].open,
                high=str(DecimalText(str(max(Decimal(row.high) for row in rows)))),
                low=str(DecimalText(str(min(Decimal(row.low) for row in rows)))),
                close=rows[-1].close,
                base_volume=_sum_decimal(row.base_volume for row in rows),
                quote_volume=quote_volume,
            )
        )
    return tuple(result)


def resample_roles(
    candles: Sequence[ClosedCandle],
    roles: Mapping[str, str],
    *,
    evaluation_time_ms: int | None = None,
) -> Mapping[str, tuple[DerivedCandle, ...]]:
    """Bind configured logical roles without embedding role-specific timeframes."""

    if type(roles) is not dict and not isinstance(roles, Mapping):
        raise TypeError("roles must be a mapping")
    result: dict[str, tuple[DerivedCandle, ...]] = {}
    cache: dict[str, tuple[DerivedCandle, ...]] = {}
    for role, timeframe in roles.items():
        if type(role) is not str or not role:
            raise ValueError("role identifiers must be non-empty strings")
        if timeframe not in cache:
            cache[timeframe] = resample_complete(
                candles, timeframe, evaluation_time_ms=evaluation_time_ms
            )
        result[role] = cache[timeframe]
    return MappingProxyType(result)


def _sum_decimal(values: Iterable[str]) -> str:
    total = sum((Decimal(value) for value in values), start=Decimal(0))
    return str(DecimalText(format(total, "f")))


def hash_derived_candles(candles: Sequence[DerivedCandle]) -> str:
    """Hash provider-neutral derived rows in full canonical row order."""

    rows = [candle.canonical_dict() for candle in candles]
    rows.sort(key=_canonical_json)
    encoded = _canonical_json(rows).encode("utf-8")
    return sha256(b"derived-candles-v1\0" + encoded).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )
