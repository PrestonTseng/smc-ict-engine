"""Strict, deeply immutable Pydantic configuration models."""

from __future__ import annotations

import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import PurePosixPath
from types import MappingProxyType
from typing import Annotated, Any, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    ValidationInfo,
    field_validator,
    model_validator,
)

from smc_ict.domain import Timeframe

CanonicalScalar = None | bool | int | str
CanonicalData = CanonicalScalar | list["CanonicalData"] | dict[str, "CanonicalData"]

TIMEFRAMES = frozenset(Timeframe.allowed_values())
IMPLEMENTED_PLUGIN_IDS = (
    "smc.swing_structure",
    "smc.equal_high_low",
    "smc.order_block",
    "ict.clustered_liquidity",
    "ict.market_structure",
    "ict.fair_value_gap",
    "project.risk_levels",
)
DEFERRED_PLUGIN_IDS = IMPLEMENTED_PLUGIN_IDS
PLUGIN_CONTRACTS = {
    "smc.swing_structure": ("regime", frozenset({"4h"}), ()),
    "smc.equal_high_low": ("context", frozenset({"1h"}), ("smc.swing_structure",)),
    "smc.order_block": ("context", frozenset({"1h"}), ("smc.swing_structure",)),
    "ict.clustered_liquidity": (
        "execution",
        frozenset({"5m", "15m"}),
        ("smc.equal_high_low",),
    ),
    "ict.market_structure": (
        "execution",
        frozenset({"5m", "15m"}),
        ("ict.clustered_liquidity",),
    ),
    "ict.fair_value_gap": (
        "execution",
        frozenset({"5m", "15m"}),
        ("ict.market_structure",),
    ),
    "project.risk_levels": (
        "execution",
        frozenset({"5m", "15m"}),
        ("ict.clustered_liquidity", "ict.fair_value_gap"),
    ),
}

STRICT_MODEL = ConfigDict(strict=True, frozen=True, extra="forbid")
Identifier = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}[a-z0-9]$")]
Role = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,31}$")]
DestinationId = Role
InstrumentId = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Z0-9]+(?:-[A-Z0-9]+){2,5}$")
]
ProviderSymbol = Annotated[
    str, StringConstraints(min_length=1, max_length=64, pattern=r"^[A-Z0-9-]+$")
]


def frozen_mapping[T](values: Mapping[str, T]) -> Mapping[str, T]:
    return MappingProxyType(dict(values))


def _tuple(value: object) -> object:
    if type(value) is list:
        return tuple(value)
    return value


def _plain_string(value: str) -> str:
    if "${" in value:
        raise ValueError("environment expansion is not allowed in ordinary strings")
    return value


def _unique(values: tuple[str, ...], *, empty: bool = False) -> tuple[str, ...]:
    if not empty and not values:
        raise ValueError("must not be empty")
    if len(set(values)) != len(values):
        raise ValueError("must not contain duplicates")
    return values


def _canonical_decimal(
    value: str, *, minimum: str = "0", maximum: str | None = None, positive: bool = False
) -> str:
    if len(value) > 64 or re.fullmatch(r"(0|[1-9][0-9]*)(\.[0-9]+)?", value) is None:
        raise ValueError("expected a quoted non-negative fixed-point decimal string")
    try:
        number = Decimal(value)
    except InvalidOperation as exc:
        raise ValueError("invalid decimal string") from exc
    if number < Decimal(minimum) or (positive and number <= 0):
        raise ValueError(f"decimal must be {'positive' if positive else f'>= {minimum}'}")
    if maximum is not None and number > Decimal(maximum):
        raise ValueError(f"decimal must be <= {maximum}")
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def _cron_atom(atom: str, minimum: int, maximum: int) -> None:
    base, slash, step_text = atom.partition("/")
    if slash:
        if (
            not step_text.isascii()
            or not step_text.isdigit()
            or not 1 <= int(step_text) <= maximum - minimum + 1
        ):
            raise ValueError("cron step must be a positive integer within the field span")
    if base == "*":
        return
    if "-" in base:
        start_text, separator, end_text = base.partition("-")
        if not separator or not start_text.isdigit() or not end_text.isdigit():
            raise ValueError("invalid cron range")
        start, end = int(start_text), int(end_text)
        if start >= end or not minimum <= start <= maximum or not minimum <= end <= maximum:
            raise ValueError("cron range must be ascending and in bounds")
        return
    if not base.isascii() or not base.isdigit() or not minimum <= int(base) <= maximum:
        raise ValueError("cron number is out of bounds")


def _cron(value: str) -> str:
    fields = value.split()
    if len(fields) != 5:
        raise ValueError("expected exactly five cron fields")
    for part, bounds in zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6)), strict=True):
        members = part.split(",")
        if not members or any(not member for member in members):
            raise ValueError("cron field contains an empty member")
        for member in members:
            _cron_atom(member, *bounds)
    return value


def _config_path(value: str) -> str:
    path = PurePosixPath(value)
    permitted_roots = (PurePosixPath("/config"), PurePosixPath("/strategies"))
    if (
        not path.is_absolute()
        or path.suffix not in {".yaml", ".yml"}
        or ".." in path.parts
        or not any(path.is_relative_to(root) for root in permitted_roots)
        or str(path) != value
    ):
        raise ValueError("expected a normalized YAML path below /config or /strategies")
    return value


Cron = Annotated[str, AfterValidator(_cron)]
ConfigPath = Annotated[str, AfterValidator(_config_path)]
PlainString = Annotated[str, AfterValidator(_plain_string)]


class MarketDataConfig(BaseModel):
    model_config = STRICT_MODEL

    provider: Literal["binance_usdm", "okx_swap"]
    market_type: Literal["LINEAR_PERPETUAL"]
    instruments: Mapping[InstrumentId, ProviderSymbol]

    def __init__(
        self, provider: str, market_type: str, instruments: Mapping[str, str], **extra: object
    ) -> None:
        super().__init__(
            provider=provider, market_type=market_type, instruments=instruments, **extra
        )

    @field_validator("instruments", mode="after")
    @classmethod
    def freeze_instruments(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if not value:
            raise ValueError("must not be empty")
        return frozen_mapping(value)

    def canonical_dict(self) -> dict[str, object]:
        return {
            "provider": self.provider,
            "market_type": self.market_type,
            "instruments": dict(self.instruments),
        }


class ScheduleJob(BaseModel):
    model_config = STRICT_MODEL

    id: Identifier
    cron: Cron
    strategy: ConfigPath
    market_data: ConfigPath
    notifications: ConfigPath
    misfire_policy: Literal["skip"]
    misfire_grace_seconds: Annotated[int, Field(ge=1, le=3600)]
    overlap_policy: Literal["skip"]
    maximum_runtime_seconds: Annotated[int, Field(ge=1, le=86400)]
    startup_delay_seconds: Annotated[int, Field(ge=0, le=300)]

    def canonical_dict(self) -> dict[str, object]:
        return self.model_dump(mode="python")


class ScheduleConfig(BaseModel):
    model_config = STRICT_MODEL

    enabled: bool
    timezone: Literal["UTC"]
    jobs: Annotated[tuple[ScheduleJob, ...], Field(max_length=32)]

    def __init__(
        self, enabled: bool, timezone: str, jobs: tuple[ScheduleJob, ...], **extra: object
    ) -> None:
        super().__init__(enabled=enabled, timezone=timezone, jobs=jobs, **extra)

    @field_validator("jobs", mode="before")
    @classmethod
    def accept_yaml_jobs(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def validate_jobs(self) -> Self:
        if self.enabled and not self.jobs:
            raise ValueError("enabled schedule requires at least one job")
        ids = tuple(job.id for job in self.jobs)
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate job identifier")
        return self

    def canonical_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "timezone": self.timezone,
            "jobs": [job.canonical_dict() for job in self.jobs],
        }


class SecretRef(BaseModel):
    model_config = STRICT_MODEL

    kind: Literal["env", "file"]
    name: str

    def __init__(self, kind: str | None = None, name: str | None = None, **extra: object) -> None:
        if kind is None and name is None:
            super().__init__(**extra)
            return
        super().__init__(kind=kind, name=name, **extra)

    @model_validator(mode="before")
    @classmethod
    def expand_external_reference(cls, value: Any) -> Any:
        if type(value) is dict and set(value) in ({"env"}, {"file"}):
            kind = next(iter(value))
            return {"kind": kind, "name": value[kind]}
        return value

    @model_validator(mode="after")
    def validate_reference(self) -> Self:
        pattern = (
            r"[A-Z][A-Z0-9_]{0,127}"
            if self.kind == "env"
            else r"/run/secrets/[A-Za-z0-9_.-]{1,128}"
        )
        if re.fullmatch(pattern, self.name) is None:
            raise ValueError("invalid secret reference")
        return self

    def redacted_dict(self) -> dict[str, str]:
        return {self.kind: self.name}


class RetryConfig(BaseModel):
    model_config = STRICT_MODEL

    maximum_attempts: Annotated[int, Field(ge=1, le=5)]
    backoff_seconds: tuple[Annotated[int, Field(ge=0, le=300)], ...]

    def __init__(
        self, maximum_attempts: int, backoff_seconds: tuple[int, ...], **extra: object
    ) -> None:
        super().__init__(
            maximum_attempts=maximum_attempts, backoff_seconds=backoff_seconds, **extra
        )

    @field_validator("backoff_seconds", mode="before")
    @classmethod
    def accept_yaml_backoffs(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def validate_backoffs(self) -> Self:
        if (
            len(self.backoff_seconds) != self.maximum_attempts - 1
            or tuple(sorted(set(self.backoff_seconds))) != self.backoff_seconds
        ):
            raise ValueError("requires one strictly increasing value per retry")
        return self


class DeduplicationConfig(BaseModel):
    model_config = STRICT_MODEL

    window_seconds: Annotated[int, Field(ge=1, le=86400)]
    key_fields: tuple[Literal["event_type", "run_id", "instrument_id", "strategy_id"], ...]

    def __init__(self, window_seconds: int, key_fields: tuple[str, ...], **extra: object) -> None:
        super().__init__(window_seconds=window_seconds, key_fields=key_fields, **extra)

    @field_validator("key_fields", mode="before")
    @classmethod
    def accept_yaml_keys(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def validate_keys(self) -> Self:
        _unique(self.key_fields)
        if not {"event_type", "run_id"}.issubset(self.key_fields):
            raise ValueError("must include event_type and run_id")
        return self


class BatchingConfig(BaseModel):
    model_config = STRICT_MODEL

    maximum_events: Annotated[int, Field(ge=1, le=1000)]
    flush_seconds: Annotated[int, Field(ge=1, le=300)]

    def __init__(self, maximum_events: int, flush_seconds: int, **extra: object) -> None:
        super().__init__(maximum_events=maximum_events, flush_seconds=flush_seconds, **extra)


class RedactionConfig(BaseModel):
    model_config = STRICT_MODEL

    headers: tuple[Literal["authorization", "cookie", "set-cookie", "proxy-authorization"], ...]
    query_parameters: tuple[Literal["token", "key", "signature", "secret"], ...]

    def __init__(
        self, headers: tuple[str, ...], query_parameters: tuple[str, ...], **extra: object
    ) -> None:
        super().__init__(headers=headers, query_parameters=query_parameters, **extra)

    @field_validator("headers", "query_parameters", mode="before")
    @classmethod
    def accept_yaml_values(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("headers", "query_parameters", mode="after")
    @classmethod
    def validate_values(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, empty=True)


class NotificationDestination(BaseModel):
    model_config = STRICT_MODEL

    adapter: Literal["discord_webhook", "generic_webhook"]
    enabled: bool
    enabled_events: tuple[
        Literal["run_started", "run_succeeded", "run_failed", "decision_found", "no_decision"], ...
    ]
    endpoint: SecretRef
    timeout_seconds: Annotated[int, Field(ge=1, le=60)]
    retries: RetryConfig
    deduplication: DeduplicationConfig
    batching: BatchingConfig
    redaction: RedactionConfig
    failure_policy: Literal["warning"]

    def __init__(
        self,
        adapter: str,
        enabled: bool,
        enabled_events: tuple[str, ...],
        endpoint: SecretRef,
        timeout_seconds: int,
        retries: RetryConfig,
        deduplication: DeduplicationConfig,
        batching: BatchingConfig,
        redaction: RedactionConfig,
        failure_policy: str,
        **extra: object,
    ) -> None:
        super().__init__(
            adapter=adapter,
            enabled=enabled,
            enabled_events=enabled_events,
            endpoint=endpoint,
            timeout_seconds=timeout_seconds,
            retries=retries,
            deduplication=deduplication,
            batching=batching,
            redaction=redaction,
            failure_policy=failure_policy,
            **extra,
        )

    @field_validator("enabled_events", mode="before")
    @classmethod
    def accept_yaml_events(cls, value: object) -> object:
        return _tuple(value)

    @model_validator(mode="after")
    def validate_destination(self) -> Self:
        _unique(self.enabled_events, empty=not self.enabled)
        if self.enabled and not self.enabled_events:
            raise ValueError("enabled destination requires events")
        if not self.enabled and self.enabled_events:
            raise ValueError("disabled destination requires an empty list")
        maximum = 10 if self.adapter == "discord_webhook" else 1000
        if self.batching.maximum_events > maximum:
            value = self.batching.maximum_events
            raise ValueError(f"batching.maximum_events: expected 1..{maximum}, got {value}")
        return self

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


class NotificationConfig(BaseModel):
    model_config = STRICT_MODEL

    enabled: bool
    destinations: Mapping[DestinationId, NotificationDestination]

    def __init__(
        self, enabled: bool, destinations: Mapping[str, NotificationDestination], **extra: object
    ) -> None:
        super().__init__(enabled=enabled, destinations=destinations, **extra)

    @field_validator("destinations", mode="after")
    @classmethod
    def freeze_destinations(
        cls, value: Mapping[str, NotificationDestination]
    ) -> Mapping[str, NotificationDestination]:
        return frozen_mapping(value)

    @model_validator(mode="after")
    def validate_destinations(self) -> Self:
        if self.enabled and not 1 <= len(self.destinations) <= 16:
            raise ValueError("enabled notifications require 1..16 destinations")
        if not self.enabled and self.destinations:
            raise ValueError("disabled notifications require an empty map")
        return self

    def redacted_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "destinations": {
                name: destination.redacted_dict() for name, destination in self.destinations.items()
            },
        }


class _SwingStructureParameters(BaseModel):
    model_config = STRICT_MODEL
    swing_length: Annotated[int, Field(ge=10, le=10000)]
    show_labels: bool


class _EqualHighLowParameters(BaseModel):
    model_config = STRICT_MODEL
    confirmation_bars: Annotated[int, Field(ge=1, le=10000)]
    threshold_atr_fraction: str

    @field_validator("threshold_atr_fraction", mode="after")
    @classmethod
    def validate_threshold(cls, value: str) -> str:
        return _canonical_decimal(value, maximum="0.5")


class _OrderBlockParameters(BaseModel):
    model_config = STRICT_MODEL
    scope: Literal["swing", "internal"]
    volatility_filter: Literal["atr", "cumulative_range"]
    mitigation_source: Literal["close", "high_low"]
    maximum_blocks: Annotated[int, Field(ge=1, le=100)]


class _ClusteredLiquidityParameters(BaseModel):
    model_config = STRICT_MODEL
    pivot_width: Annotated[int, Field(ge=3, le=10)]
    minimum_pivots: Annotated[int, Field(ge=3, le=50)]
    margin_atr_fraction: str

    @field_validator("margin_atr_fraction", mode="after")
    @classmethod
    def validate_margin(cls, value: str) -> str:
        result = _canonical_decimal(value)
        allowed = {Decimal(number) / Decimal(10) for number in range(2, 8)}
        if Decimal(result) not in allowed:
            raise ValueError("expected a source-representable tenth from 0.2 through 0.7")
        return result


class _MarketStructureParameters(BaseModel):
    model_config = STRICT_MODEL
    pivot_width: Annotated[int, Field(ge=3, le=10)]
    emit_mss: bool
    emit_bos: bool

    @model_validator(mode="after")
    def validate_events(self) -> Self:
        if not self.emit_mss and not self.emit_bos:
            raise ValueError("at least one structure event must be enabled")
        return self


class _FairValueGapParameters(BaseModel):
    model_config = STRICT_MODEL
    kind: Literal["ordinary"]
    require_displacement: bool
    displacement_length: Annotated[int, Field(ge=1, le=10000)]
    mitigation: Literal["full_traversal"]


class _RiskLevelParameters(BaseModel):
    model_config = STRICT_MODEL
    minimum_reward_risk: str

    @field_validator("minimum_reward_risk", mode="after")
    @classmethod
    def validate_reward_risk(cls, value: str) -> str:
        return _canonical_decimal(value, positive=True)


PARAMETER_MODELS: dict[str, type[BaseModel]] = {
    "smc.swing_structure": _SwingStructureParameters,
    "smc.equal_high_low": _EqualHighLowParameters,
    "smc.order_block": _OrderBlockParameters,
    "ict.clustered_liquidity": _ClusteredLiquidityParameters,
    "ict.market_structure": _MarketStructureParameters,
    "ict.fair_value_gap": _FairValueGapParameters,
    "project.risk_levels": _RiskLevelParameters,
}


class SignalConfig(BaseModel):
    model_config = STRICT_MODEL

    id: Literal[
        "smc.swing_structure",
        "smc.equal_high_low",
        "smc.order_block",
        "ict.clustered_liquidity",
        "ict.market_structure",
        "ict.fair_value_gap",
        "project.risk_levels",
    ]
    role: Role
    depends_on: tuple[str, ...]
    parameters: Mapping[str, CanonicalScalar]
    required: bool
    effect: Literal["REJECT", "LEVELS"]
    order: Annotated[int, Field(ge=0, le=2**31 - 1)]

    def __init__(
        self,
        id: str,
        role: str,
        depends_on: tuple[str, ...],
        parameters: Mapping[str, CanonicalScalar],
        required: bool,
        effect: str,
        order: int,
        **extra: object,
    ) -> None:
        super().__init__(
            id=id,
            role=role,
            depends_on=depends_on,
            parameters=parameters,
            required=required,
            effect=effect,
            order=order,
            **extra,
        )

    @field_validator("depends_on", mode="before")
    @classmethod
    def accept_yaml_dependencies(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("depends_on", mode="after")
    @classmethod
    def validate_dependencies(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value, empty=True)

    @field_validator("parameters", mode="before")
    @classmethod
    def validate_parameters(
        cls, value: object, info: ValidationInfo
    ) -> Mapping[str, CanonicalScalar]:
        plugin_id = info.data.get("id")
        if plugin_id not in PARAMETER_MODELS:
            return value  # type: ignore[return-value]
        if isinstance(value, MappingProxyType):
            value = dict(value)
        parsed = PARAMETER_MODELS[plugin_id].model_validate(value)
        return frozen_mapping(parsed.model_dump(mode="python"))

    @field_validator("parameters", mode="after")
    @classmethod
    def freeze_parameters(
        cls, value: Mapping[str, CanonicalScalar]
    ) -> Mapping[str, CanonicalScalar]:
        return frozen_mapping(value)

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


class StrategyConfig(BaseModel):
    model_config = STRICT_MODEL

    name: Identifier
    version: Annotated[PlainString, Field(min_length=1, max_length=32)]
    instruments: tuple[InstrumentId, ...]
    history_minutes: Annotated[int, Field(ge=1, le=10_000_000)]
    roles: Mapping[Role, PlainString]
    signals: tuple[SignalConfig, ...]

    @field_validator("instruments", "signals", mode="before")
    @classmethod
    def accept_yaml_sequences(cls, value: object) -> object:
        return _tuple(value)

    @field_validator("instruments", mode="after")
    @classmethod
    def validate_instruments(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        return _unique(value)

    @field_validator("roles", mode="after")
    @classmethod
    def validate_roles(cls, value: Mapping[str, str]) -> Mapping[str, str]:
        if not value:
            raise ValueError("must not be empty")
        for timeframe in value.values():
            if timeframe not in TIMEFRAMES:
                raise ValueError(f"unknown timeframe; allowed values are {sorted(TIMEFRAMES)}")
        return frozen_mapping(value)

    @model_validator(mode="after")
    def validate_graph(self, info: ValidationInfo) -> Self:
        if not (info.context and info.context.get("external")):
            return self
        if not self.signals:
            raise ValueError("signals must not be empty")
        seen: set[str] = set()
        previous_order = -1
        for signal in self.signals:
            if signal.id in seen:
                raise ValueError("unknown or duplicate plugin ID")
            if signal.role not in self.roles:
                raise ValueError(f"signal {signal.id}: unknown role")
            expected_role, timeframes, dependencies = PLUGIN_CONTRACTS[signal.id]
            if signal.role != expected_role:
                raise ValueError(f"signal {signal.id}: configured role must be {expected_role}")
            if self.roles[signal.role] not in timeframes:
                raise ValueError(
                    f"role {signal.role}: configured timeframe must be one of {sorted(timeframes)}"
                )
            if signal.depends_on != dependencies:
                raise ValueError(
                    f"signal {signal.id}: configured dependencies must be {list(dependencies)}"
                )
            if any(dependency not in seen for dependency in signal.depends_on):
                raise ValueError(
                    f"signal {signal.id}: dependencies must name earlier configured signals"
                )
            if signal.order <= previous_order:
                raise ValueError(f"signal {signal.id}: orders must be strictly increasing")
            seen.add(signal.id)
            previous_order = signal.order
        return self

    def canonical_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "version": self.version,
            "instruments": list(self.instruments),
            "history_minutes": self.history_minutes,
            "roles": dict(self.roles),
            "signals": [signal.canonical_dict() for signal in self.signals],
        }


class MarketDataDocument(BaseModel):
    model_config = STRICT_MODEL
    market_data: MarketDataConfig


class ScheduleDocument(BaseModel):
    model_config = STRICT_MODEL
    schedule: ScheduleConfig


class NotificationDocument(BaseModel):
    model_config = STRICT_MODEL
    notifications: NotificationConfig

    @model_validator(mode="after")
    def validate_external_constraints(self) -> Self:
        for destination in self.notifications.destinations.values():
            redaction = destination.redaction
            if not redaction.headers or not redaction.query_parameters:
                raise ValueError("redaction lists must not be empty")
            if any(value < 1 for value in destination.retries.backoff_seconds):
                raise ValueError("backoff seconds must be in the range 1..300")
        return self
