"""Bounded YAML 1.2 loading before typed model construction."""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping

from ruamel.yaml import YAML
from ruamel.yaml.error import YAMLError
from ruamel.yaml.events import (
    AliasEvent,
    DocumentStartEvent,
    MappingEndEvent,
    MappingStartEvent,
    ScalarEvent,
    SequenceEndEvent,
    SequenceStartEvent,
)

from .errors import StrictConfigurationError

type CanonicalValue = None | bool | int | str | list["CanonicalValue"] | dict[str, "CanonicalValue"]

MAX_BYTES = 65_536
MAX_DEPTH = 24
MAX_NODES = 4_096
MAX_SCALAR_BYTES = 8_192
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1
_AMBIGUOUS_PLAIN = frozenset(
    {
        "y",
        "Y",
        "yes",
        "Yes",
        "YES",
        "n",
        "N",
        "no",
        "No",
        "NO",
        "on",
        "On",
        "ON",
        "off",
        "Off",
        "OFF",
    }
)


def _fail(message: str) -> StrictConfigurationError:
    return StrictConfigurationError(f"strict YAML: {message}")


def _scan(yaml: YAML, text: str) -> None:
    depth = 0
    nodes = 0
    documents = 0
    try:
        events = yaml.parse(text)
        for event in events:
            if isinstance(event, DocumentStartEvent):
                documents += 1
                if documents > 1:
                    raise _fail("exactly one document is required")
            if isinstance(event, AliasEvent):
                raise _fail("aliases are not permitted")
            if getattr(event, "anchor", None) is not None:
                raise _fail("anchors are not permitted")
            if getattr(event, "tag", None) is not None:
                raise _fail("explicit tags are not permitted")
            if isinstance(event, MappingStartEvent | SequenceStartEvent | ScalarEvent):
                nodes += 1
                if nodes > MAX_NODES:
                    raise _fail(f"node count exceeds {MAX_NODES}")
            if isinstance(event, MappingStartEvent | SequenceStartEvent):
                depth += 1
                if depth > MAX_DEPTH:
                    raise _fail(f"depth exceeds {MAX_DEPTH}")
            elif isinstance(event, MappingEndEvent | SequenceEndEvent):
                depth -= 1
            elif isinstance(event, ScalarEvent):
                if len(event.value.encode("utf-8")) > MAX_SCALAR_BYTES:
                    raise _fail(f"scalar exceeds {MAX_SCALAR_BYTES} UTF-8 bytes")
                if event.value == "<<":
                    raise _fail("merge keys are not permitted")
                if event.style is None and event.value in _AMBIGUOUS_PLAIN:
                    raise _fail(f"ambiguous plain scalar {event.value!r} must be quoted")
    except StrictConfigurationError:
        raise
    except YAMLError as exc:
        raise _fail("invalid syntax or duplicate key") from exc
    if documents != 1:
        raise _fail("exactly one document is required")


def _canonical_plain(value: object, path: str = "$", depth: int = 0) -> CanonicalValue:
    if depth > MAX_DEPTH:
        raise _fail(f"constructed depth exceeds {MAX_DEPTH}")
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        if not INT64_MIN <= value <= INT64_MAX:
            raise _fail(f"{path} integer is outside signed 64-bit range")
        return value
    if type(value) is str:
        return unicodedata.normalize("NFC", value)
    if type(value) is list:
        return [
            _canonical_plain(item, f"{path}[{index}]", depth + 1)
            for index, item in enumerate(value)
        ]
    if isinstance(value, Mapping):
        result: dict[str, CanonicalValue] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _fail(f"{path} contains a non-string key")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in result:
                raise _fail(f"{path} contains duplicate keys after NFC normalization")
            result[normalized_key] = _canonical_plain(item, f"{path}.{normalized_key}", depth + 1)
        return result
    raise _fail(f"{path} has unsupported implicit type {type(value).__name__}")


def load_yaml_12(text: str) -> CanonicalValue:
    """Load one bounded YAML 1.2 document into canonical plain data."""
    if type(text) is not str:
        raise _fail("input must be text")
    try:
        encoded = text.encode("utf-8")
    except UnicodeError as exc:
        raise _fail("input must be valid UTF-8") from exc
    if len(encoded) > MAX_BYTES:
        raise _fail(f"document exceeds {MAX_BYTES} UTF-8 bytes")
    yaml = YAML(typ="safe", pure=True)
    yaml.version = (1, 2)
    yaml.allow_duplicate_keys = False
    _scan(yaml, text)
    try:
        loaded = yaml.load(text)
    except YAMLError as exc:
        raise _fail("invalid syntax or duplicate key") from exc
    return _canonical_plain(loaded)
