"""Canonical identifiers, exact decimals, and UTC timestamps."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Self

_DECIMAL = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z")
_INSTRUMENT = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+){2,5}\Z")
_EVENT_TYPES = frozenset(
    {"run_started", "run_succeeded", "run_failed", "decision_found", "no_decision"}
)
_TIMEFRAME_MINUTES = {"1m": 1, "5m": 5, "15m": 15, "1h": 60, "4h": 240}


class DecimalText(str):
    """A finite non-negative decimal stored in canonical fixed-point form."""

    def __new__(cls, value: str) -> Self:
        if type(value) is not str:
            raise TypeError("decimal value must be a string")
        if len(value) > 64 or _DECIMAL.fullmatch(value) is None:
            raise ValueError("decimal value must use non-negative fixed-point syntax")
        try:
            number = Decimal(value)
        except InvalidOperation as exc:
            raise ValueError("invalid decimal value") from exc
        normalized = format(number, "f")
        if "." in normalized:
            normalized = normalized.rstrip("0").rstrip(".")
        return super().__new__(cls, normalized or "0")

    @property
    def decimal(self) -> Decimal:
        return Decimal(self)


class InstrumentId(str):
    """Canonical uppercase instrument identity, independent of transport symbols."""

    def __new__(cls, value: str) -> Self:
        if type(value) is not str:
            raise TypeError("instrument identifier must be a string")
        if not 1 <= len(value) <= 64 or _INSTRUMENT.fullmatch(value) is None:
            raise ValueError("invalid canonical instrument identifier")
        return super().__new__(cls, value)


class Timeframe(str):
    """A closed v1 canonical timeframe identifier."""

    def __new__(cls, value: str) -> Self:
        if type(value) is not str:
            raise TypeError("timeframe must be a string")
        if value not in _TIMEFRAME_MINUTES:
            raise ValueError(f"unknown timeframe {value!r}")
        return super().__new__(cls, value)

    @property
    def duration_minutes(self) -> int:
        """Return the single canonical duration authority for this timeframe."""

        return _TIMEFRAME_MINUTES[self]

    @classmethod
    def allowed_values(cls) -> tuple[str, ...]:
        """Expose the closed canonical set without duplicating it in other layers."""

        return tuple(_TIMEFRAME_MINUTES)


class EventType(str):
    """A provider-neutral notification event identifier."""

    def __new__(cls, value: str) -> Self:
        if type(value) is not str:
            raise TypeError("event type must be a string")
        if value not in _EVENT_TYPES:
            raise ValueError(f"unknown event type {value!r}")
        return super().__new__(cls, value)


@dataclass(frozen=True, slots=True)
class UtcTimestamp:
    """A timezone-aware UTC instant with exact millisecond precision."""

    datetime: datetime

    def __post_init__(self) -> None:
        value = self.datetime
        if (
            type(value) is not datetime
            or value.tzinfo is None
            or value.utcoffset() != UTC.utcoffset(value)
        ):
            raise ValueError("timestamp must be timezone-aware UTC")
        if value.microsecond % 1000 != 0:
            raise ValueError("timestamp must have millisecond precision")
        object.__setattr__(self, "datetime", value.astimezone(UTC))

    @property
    def milliseconds(self) -> int:
        return int(self.datetime.timestamp() * 1000)

    @classmethod
    def from_milliseconds(cls, value: int) -> Self:
        if type(value) is not int:
            raise TypeError("timestamp milliseconds must be an integer")
        if value < 0:
            raise ValueError("timestamp milliseconds must be non-negative")
        return cls(datetime.fromtimestamp(value / 1000, tz=UTC))

    @classmethod
    def parse(cls, value: str) -> Self:
        if type(value) is not str or not value.endswith("Z"):
            raise ValueError("timestamp must use an RFC 3339 UTC Z suffix")
        try:
            parsed = datetime.fromisoformat(value[:-1] + "+00:00")
        except ValueError as exc:
            raise ValueError("invalid UTC timestamp") from exc
        return cls(parsed)

    def __str__(self) -> str:
        rendered = self.datetime.isoformat(timespec="milliseconds")
        return rendered.removesuffix("+00:00") + "Z"
