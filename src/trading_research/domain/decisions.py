"""Immutable source-neutral decisions and canonical decision hashing."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from hashlib import sha256

from .evidence_values import canonical_evidence, freeze_evidence
from .values import DecimalText, InstrumentId


@dataclass(frozen=True, slots=True)
class Decision:
    instrument_id: str
    status: str
    direction: str | None
    entry_text: str | None
    stop_text: str | None
    target_text: str | None
    first_failed_signal: str | None
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        object.__setattr__(self, "instrument_id", str(InstrumentId(self.instrument_id)))
        if self.status not in {"READY", "NO_TRADE", "UNAVAILABLE"}:
            raise ValueError("unknown decision status")
        if self.status == "READY":
            if self.direction not in {"LONG", "SHORT"} or self.first_failed_signal is not None:
                raise ValueError("ready decision requires a direction and no failed signal")
            if any(value is None for value in (self.entry_text, self.stop_text, self.target_text)):
                raise ValueError("ready decision requires entry, stop, and target levels")
            for field in ("entry_text", "stop_text", "target_text"):
                object.__setattr__(self, field, str(DecimalText(getattr(self, field))))
        elif (
            self.direction is not None
            or self.entry_text is not None
            or self.stop_text is not None
            or self.target_text is not None
            or not self.first_failed_signal
        ):
            raise ValueError("non-ready decision requires only a first failed signal")
        object.__setattr__(self, "payload", freeze_evidence(self.payload))

    def canonical_dict(self) -> dict[str, object]:
        return {
            field: canonical_evidence(getattr(self, field)) for field in self.__dataclass_fields__
        }


def hash_decision(decision: Decision) -> str:
    encoded = json.dumps(
        decision.canonical_dict(),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return sha256(b"decision-v1\0" + encoded).hexdigest()
