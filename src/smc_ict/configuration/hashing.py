"""Canonical JSON and domain-separated configuration hashes."""

from __future__ import annotations

import json
from hashlib import sha256
from typing import Protocol


class CanonicalModel(Protocol):
    def canonical_dict(self) -> dict[str, object]: ...


class RedactedModel(Protocol):
    def redacted_dict(self) -> dict[str, object]: ...


def canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _hash(domain: bytes, value: object) -> str:
    return sha256(domain + canonical_json(value)).hexdigest()


def hash_market_data(config: CanonicalModel) -> str:
    return _hash(b"market-config-v1\0", config.canonical_dict())


def hash_schedule(config: CanonicalModel) -> str:
    return _hash(b"schedule-v1\0", config.canonical_dict())


def hash_strategy(config: CanonicalModel) -> str:
    return _hash(b"strategy-v2\0", config.canonical_dict())


def hash_notifications(config: RedactedModel) -> str:
    return _hash(b"notification-v1\0", config.redacted_dict())
