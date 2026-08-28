"""Frozen typed configuration models."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

CanonicalScalar = None | bool | int | str
CanonicalData = CanonicalScalar | list["CanonicalData"] | dict[str, "CanonicalData"]


def frozen_mapping[T](values: Mapping[str, T]) -> Mapping[str, T]:
    return MappingProxyType(dict(values))


@dataclass(frozen=True, slots=True)
class MarketDataConfig:
    provider: str
    market_type: str
    instruments: Mapping[str, str]

    def canonical_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "market_type": self.market_type,
            "instruments": dict(self.instruments),
        }


@dataclass(frozen=True, slots=True)
class ScheduleJob:
    id: str
    cron: str
    strategy: str
    market_data: str
    notifications: str
    misfire_policy: str
    misfire_grace_seconds: int
    overlap_policy: str
    maximum_runtime_seconds: int
    startup_delay_seconds: int

    def canonical_dict(self) -> dict[str, object]:
        return {field: getattr(self, field) for field in self.__dataclass_fields__}


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    enabled: bool
    timezone: str
    jobs: tuple[ScheduleJob, ...]

    def canonical_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "timezone": self.timezone,
            "jobs": [job.canonical_dict() for job in self.jobs],
        }


@dataclass(frozen=True, slots=True)
class SecretRef:
    kind: str
    name: str

    def redacted_dict(self) -> dict[str, str]:
        return {self.kind: self.name}


@dataclass(frozen=True, slots=True)
class RetryConfig:
    maximum_attempts: int
    backoff_seconds: tuple[int, ...]


@dataclass(frozen=True, slots=True)
class DeduplicationConfig:
    window_seconds: int
    key_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class BatchingConfig:
    maximum_events: int
    flush_seconds: int


@dataclass(frozen=True, slots=True)
class RedactionConfig:
    headers: tuple[str, ...]
    query_parameters: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class NotificationDestination:
    adapter: str
    enabled: bool
    enabled_events: tuple[str, ...]
    endpoint: SecretRef
    timeout_seconds: int
    retries: RetryConfig
    deduplication: DeduplicationConfig
    batching: BatchingConfig
    redaction: RedactionConfig
    failure_policy: str

    def redacted_dict(self) -> dict[str, object]:
        return {
            "adapter": self.adapter,
            "enabled": self.enabled,
            "enabled_events": list(self.enabled_events),
            "endpoint": self.endpoint.redacted_dict(),
            "timeout_seconds": self.timeout_seconds,
            "retries": {
                "maximum_attempts": self.retries.maximum_attempts,
                "backoff_seconds": list(self.retries.backoff_seconds),
            },
            "deduplication": {
                "window_seconds": self.deduplication.window_seconds,
                "key_fields": list(self.deduplication.key_fields),
            },
            "batching": {
                "maximum_events": self.batching.maximum_events,
                "flush_seconds": self.batching.flush_seconds,
            },
            "redaction": {
                "headers": list(self.redaction.headers),
                "query_parameters": list(self.redaction.query_parameters),
            },
            "failure_policy": self.failure_policy,
        }


@dataclass(frozen=True, slots=True)
class NotificationConfig:
    enabled: bool
    destinations: Mapping[str, NotificationDestination]

    def redacted_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "destinations": {
                name: destination.redacted_dict() for name, destination in self.destinations.items()
            },
        }


@dataclass(frozen=True, slots=True)
class SignalConfig:
    id: str
    role: str
    depends_on: tuple[str, ...]
    parameters: Mapping[str, CanonicalScalar]
    required: bool
    effect: str
    order: int

    def canonical_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "role": self.role,
            "depends_on": list(self.depends_on),
            "parameters": dict(self.parameters),
            "required": self.required,
            "effect": self.effect,
            "order": self.order,
        }


@dataclass(frozen=True, slots=True)
class StrategyConfig:
    name: str
    version: str
    instruments: tuple[str, ...]
    history_minutes: int
    roles: Mapping[str, str]
    signals: tuple[SignalConfig, ...]

    def canonical_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "instruments": list(self.instruments),
            "history_minutes": self.history_minutes,
            "roles": dict(self.roles),
            "signals": [signal.canonical_dict() for signal in self.signals],
        }
