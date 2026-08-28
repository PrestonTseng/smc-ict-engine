"""Canonical immutable JSON values used by observation and decision evidence."""

from __future__ import annotations

from collections.abc import Mapping
from types import MappingProxyType


def freeze_evidence(value: object) -> object:
    if isinstance(value, Mapping):
        if any(type(key) is not str for key in value):
            raise TypeError("evidence payload must contain canonical JSON values")
        return MappingProxyType({key: freeze_evidence(item) for key, item in value.items()})
    if isinstance(value, list | tuple):
        return tuple(freeze_evidence(item) for item in value)
    if value is None or type(value) in {bool, int, str}:
        return value
    raise TypeError("evidence payload must contain canonical JSON values")


def canonical_evidence(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): canonical_evidence(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [canonical_evidence(item) for item in value]
    return value
