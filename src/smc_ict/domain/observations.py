"""Immutable provider-neutral indicator observations."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from .evidence_values import canonical_evidence, freeze_evidence
from .values import DecimalText, InstrumentId, Timeframe


@dataclass(frozen=True, slots=True)
class Observation:
    signal_id: str
    instrument_id: str
    timeframe: str
    status: str
    event_type: str | None
    direction: str | None
    event_time_ms: int | None
    known_time_ms: int | None
    state: str
    dependency_ids: tuple[str, ...]
    parameter_hash: str
    source_manifest_ids: tuple[str, ...]
    payload_schema_version: int
    bounded_reason: str
    payload: Mapping[str, object]
    level_text: str | None = None
    lower_text: str | None = None
    upper_text: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", str(InstrumentId(self.instrument_id)))
        object.__setattr__(self, "timeframe", str(Timeframe(self.timeframe)))
        if self.status not in {"PASS", "FAIL", "UNAVAILABLE"}:
            raise ValueError("unknown observation status")
        if (self.event_time_ms is None) != (self.known_time_ms is None):
            raise ValueError("event and known times must both be present or absent")
        if self.event_time_ms is not None and (
            type(self.event_time_ms) is not int
            or type(self.known_time_ms) is not int
            or self.event_time_ms < 0
            or self.known_time_ms < self.event_time_ms
        ):
            raise ValueError("observation times are invalid")
        if len(self.parameter_hash) != 64 or any(
            char not in "0123456789abcdef" for char in self.parameter_hash
        ):
            raise ValueError("parameter hash must be lowercase SHA-256")
        if type(self.payload_schema_version) is not int or self.payload_schema_version < 1:
            raise ValueError("payload schema version must be a positive integer")
        if not 1 <= len(self.bounded_reason) <= 1000:
            raise ValueError("observation reason must contain 1..1000 characters")
        for field in ("level_text", "lower_text", "upper_text"):
            value = getattr(self, field)
            if value is not None:
                object.__setattr__(self, field, str(DecimalText(value)))
        object.__setattr__(self, "payload", freeze_evidence(self.payload))

    @classmethod
    def available(cls, **values: object) -> Observation:
        return cls(**values)  # type: ignore[arg-type]

    def canonical_dict(self) -> dict[str, object]:
        return {
            field: canonical_evidence(getattr(self, field)) for field in self.__dataclass_fields__
        }


def hash_observation(observation: Observation) -> str:
    encoded = json.dumps(
        observation.canonical_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(b"observation-v1\0" + encoded).hexdigest()
