"""Exact typed validators for each independent configuration authority."""

from __future__ import annotations

import os
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Never

from smc_ict.domain import Timeframe

from .errors import StrictConfigurationError
from .models import (
    BatchingConfig,
    DeduplicationConfig,
    MarketDataConfig,
    NotificationConfig,
    NotificationDestination,
    RedactionConfig,
    RetryConfig,
    ScheduleConfig,
    ScheduleJob,
    SecretRef,
    SignalConfig,
    StrategyConfig,
    frozen_mapping,
)
from .yaml_loader import CanonicalValue, load_yaml_12

PROVIDERS = frozenset({"binance_usdm", "okx_swap"})
MARKET_TYPE = "LINEAR_PERPETUAL"
TIMEFRAMES = frozenset(Timeframe.allowed_values())
EVENTS = frozenset({"run_started", "run_succeeded", "run_failed", "decision_found", "no_decision"})
NOTIFICATION_ADAPTERS = frozenset({"discord_webhook", "generic_webhook"})
DEDUPE_KEYS = frozenset({"event_type", "run_id", "instrument_id", "strategy_id"})
HEADERS = frozenset({"authorization", "cookie", "set-cookie", "proxy-authorization"})
QUERY_PARAMETERS = frozenset({"token", "key", "signature", "secret"})
INSTRUMENT_RE = re.compile(r"[A-Z0-9]+(?:-[A-Z0-9]+){2,5}\Z")
NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,62}[a-z0-9]\Z")
ROLE_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
DESTINATION_RE = re.compile(r"[a-z][a-z0-9_]{0,31}\Z")
ENV_RE = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
SECRET_FILE_RE = re.compile(r"/run/secrets/[A-Za-z0-9_.-]{1,128}\Z")
DECIMAL_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?\Z")
SIGNAL_FIELDS = frozenset({"id", "role", "depends_on", "parameters", "required", "effect", "order"})
IMPLEMENTED_PLUGIN_IDS = (
    "smc.swing_structure",
    "smc.equal_high_low",
    "smc.order_block",
    "ict.clustered_liquidity",
    "ict.market_structure",
    "ict.fair_value_gap",
    "project.risk_levels",
)
# Backwards-compatible export retained for callers compiled against the foundation release.
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


def fail(field: str, message: str) -> Never:
    raise StrictConfigurationError(f"{field}: {message}")


def exact_dict(
    value: object, field: str, keys: set[str] | frozenset[str] | None = None
) -> dict[str, CanonicalValue]:
    if type(value) is not dict:
        fail(field, f"expected object, got {type(value).__name__}")
    result = value
    if keys is not None and set(result) != set(keys):
        unknown = sorted(set(result) - set(keys))
        missing = sorted(set(keys) - set(result))
        fail(field, f"unknown fields={unknown}; missing fields={missing}")
    return result


def exact_list(value: object, field: str) -> list[CanonicalValue]:
    if type(value) is not list:
        fail(field, f"expected array, got {type(value).__name__}")
    return value


def exact_str(value: object, field: str) -> str:
    if type(value) is not str:
        fail(field, f"expected string, got {type(value).__name__}")
    if "${" in value:
        fail(field, "environment expansion is not allowed in ordinary strings")
    return value


def exact_bool(value: object, field: str) -> bool:
    if type(value) is not bool:
        fail(field, f"expected Boolean, got {type(value).__name__}")
    return value


def bounded_int(value: object, field: str, minimum: int, maximum: int) -> int:
    if type(value) is not int:
        fail(field, f"expected integer (Boolean is not integer), got {type(value).__name__}")
    if not minimum <= value <= maximum:
        fail(field, f"expected {minimum}..{maximum}, got {value!r}")
    return value


def unique_strings(
    value: object, field: str, allowed: frozenset[str] | None = None, *, empty: bool = False
) -> tuple[str, ...]:
    items = exact_list(value, field)
    result = tuple(exact_str(item, f"{field}[{index}]") for index, item in enumerate(items))
    if not empty and not result:
        fail(field, "must not be empty")
    if len(set(result)) != len(result):
        fail(field, "must not contain duplicates")
    if allowed is not None and any(item not in allowed for item in result):
        fail(field, f"allowed values are {sorted(allowed)}")
    return result


def instrument(value: object, field: str) -> str:
    result = exact_str(value, field)
    if not 1 <= len(result) <= 64 or INSTRUMENT_RE.fullmatch(result) is None:
        fail(field, "expected canonical uppercase instrument identifier")
    return result


def canonical_decimal(
    value: object,
    field: str,
    minimum: str = "0",
    maximum: str | None = None,
    *,
    positive: bool = False,
) -> str:
    text = exact_str(value, field)
    if len(text) > 64 or DECIMAL_RE.fullmatch(text) is None:
        fail(field, "expected a quoted non-negative fixed-point decimal string")
    try:
        number = Decimal(text)
    except InvalidOperation as exc:
        raise StrictConfigurationError(f"{field}: invalid decimal string") from exc
    if number < Decimal(minimum) or (positive and number <= 0):
        fail(field, f"decimal must be {'positive' if positive else f'>= {minimum}'}")
    if maximum is not None and number > Decimal(maximum):
        fail(field, f"decimal must be <= {maximum}")
    normalized = format(number, "f")
    if "." in normalized:
        normalized = normalized.rstrip("0").rstrip(".")
    return normalized or "0"


def ict_margin_fraction(value: object, field: str) -> str:
    result = canonical_decimal(value, field)
    allowed = {Decimal(number) / Decimal(10) for number in range(2, 8)}
    if Decimal(result) not in allowed:
        fail(field, "expected a source-representable tenth from 0.2 through 0.7")
    return result


def _root(text: str, authority: str) -> dict[str, CanonicalValue]:
    loaded = exact_dict(load_yaml_12(text), "$", {authority})
    return exact_dict(loaded[authority], authority)


def load_market_data_text(text: str) -> MarketDataConfig:
    raw = exact_dict(
        _root(text, "market_data"), "market_data", {"provider", "market_type", "instruments"}
    )
    provider = exact_str(raw["provider"], "market_data.provider")
    if provider not in PROVIDERS:
        fail("market_data.provider", f"unknown provider; allowed values are {sorted(PROVIDERS)}")
    market_type = exact_str(raw["market_type"], "market_data.market_type")
    if market_type != MARKET_TYPE:
        fail("market_data.market_type", f"expected {MARKET_TYPE}")
    source = exact_dict(raw["instruments"], "market_data.instruments")
    if not source:
        fail("market_data.instruments", "must not be empty")
    mappings: dict[str, str] = {}
    for key, value in source.items():
        canonical = instrument(key, f"market_data.instruments.{key}")
        symbol = exact_str(value, f"market_data.instruments.{key}")
        if not 1 <= len(symbol) <= 64 or re.fullmatch(r"[A-Z0-9-]+", symbol) is None:
            fail(f"market_data.instruments.{key}", "invalid provider symbol")
        mappings[canonical] = symbol
    return MarketDataConfig(provider, market_type, frozen_mapping(mappings))


def load_market_data(path: str | Path) -> MarketDataConfig:
    return load_market_data_text(Path(path).read_text(encoding="utf-8"))


def _cron_atom(atom: str, minimum: int, maximum: int, field: str) -> None:
    base, slash, step_text = atom.partition("/")
    if slash:
        if not step_text.isascii() or not step_text.isdigit():
            fail(field, "cron step must be a positive integer")
        step = int(step_text)
        if not 1 <= step <= maximum - minimum + 1:
            fail(field, "cron step exceeds field span")
    if base == "*":
        return
    if "-" in base:
        start_text, separator, end_text = base.partition("-")
        if not separator or not start_text.isdigit() or not end_text.isdigit():
            fail(field, "invalid cron range")
        start, end = int(start_text), int(end_text)
        if start >= end or not minimum <= start <= maximum or not minimum <= end <= maximum:
            fail(field, "cron range must be ascending and in bounds")
        return
    if not base.isascii() or not base.isdigit() or not minimum <= int(base) <= maximum:
        fail(field, "cron number is out of bounds")


def validate_cron(value: object, field: str) -> str:
    cron = exact_str(value, field)
    fields = cron.split()
    if len(fields) != 5:
        fail(field, "expected exactly five cron fields")
    for index, (part, bounds) in enumerate(
        zip(fields, ((0, 59), (0, 23), (1, 31), (1, 12), (0, 6)), strict=True)
    ):
        members = part.split(",")
        if not members or any(not member for member in members):
            fail(field, f"cron field {index + 1} contains an empty member")
        for member in members:
            _cron_atom(member, *bounds, field)
    return cron


def config_path(value: object, field: str) -> str:
    text = exact_str(value, field)
    path = PurePosixPath(text)
    if not path.is_absolute() or path.suffix not in {".yaml", ".yml"} or ".." in path.parts:
        fail(field, "expected a normalized YAML path below /config")
    try:
        path.relative_to("/config")
    except ValueError:
        fail(field, "path must be below /config")
    if str(path) != text:
        fail(field, "path must be normalized")
    return text


def load_schedule_text(text: str) -> ScheduleConfig:
    raw = exact_dict(_root(text, "schedule"), "schedule", {"enabled", "timezone", "jobs"})
    enabled = exact_bool(raw["enabled"], "schedule.enabled")
    timezone = exact_str(raw["timezone"], "schedule.timezone")
    if timezone != "UTC":
        fail("schedule.timezone", "only UTC is allowed in v1")
    jobs_raw = exact_list(raw["jobs"], "schedule.jobs")
    if len(jobs_raw) > 32:
        fail("schedule.jobs", "at most 32 jobs are allowed")
    if enabled and not jobs_raw:
        fail("schedule.jobs", "enabled schedule requires at least one job")
    keys = {
        "id",
        "cron",
        "strategy",
        "market_data",
        "notifications",
        "misfire_policy",
        "misfire_grace_seconds",
        "overlap_policy",
        "maximum_runtime_seconds",
        "startup_delay_seconds",
    }
    jobs: list[ScheduleJob] = []
    seen: set[str] = set()
    for index, item in enumerate(jobs_raw):
        field = f"schedule.jobs[{index}]"
        job = exact_dict(item, field, keys)
        job_id = exact_str(job["id"], f"{field}.id")
        if NAME_RE.fullmatch(job_id) is None or job_id in seen:
            fail(f"{field}.id", "invalid or duplicate job identifier")
        seen.add(job_id)
        misfire = exact_str(job["misfire_policy"], f"{field}.misfire_policy")
        overlap = exact_str(job["overlap_policy"], f"{field}.overlap_policy")
        if misfire != "skip" or overlap != "skip":
            fail(field, "both v1 policies must be skip")
        jobs.append(
            ScheduleJob(
                job_id,
                validate_cron(job["cron"], f"{field}.cron"),
                config_path(job["strategy"], f"{field}.strategy"),
                config_path(job["market_data"], f"{field}.market_data"),
                config_path(job["notifications"], f"{field}.notifications"),
                misfire,
                bounded_int(
                    job["misfire_grace_seconds"], f"{field}.misfire_grace_seconds", 1, 3600
                ),
                overlap,
                bounded_int(
                    job["maximum_runtime_seconds"], f"{field}.maximum_runtime_seconds", 1, 86400
                ),
                bounded_int(job["startup_delay_seconds"], f"{field}.startup_delay_seconds", 0, 300),
            )
        )
    return ScheduleConfig(enabled, timezone, tuple(jobs))


def load_schedule(path: str | Path) -> ScheduleConfig:
    return load_schedule_text(Path(path).read_text(encoding="utf-8"))


def _secret_ref(value: object, field: str) -> SecretRef:
    raw = exact_dict(value, field)
    if set(raw) not in ({"env"}, {"file"}):
        fail(field, "exactly one of env or file is required; literal URLs are forbidden")
    kind = next(iter(raw))
    name = exact_str(raw[kind], f"{field}.{kind}")
    pattern = ENV_RE if kind == "env" else SECRET_FILE_RE
    if pattern.fullmatch(name) is None:
        fail(f"{field}.{kind}", "invalid secret reference")
    return SecretRef(kind, name)


def _notification_destination(
    name: str, value: object, environ: Mapping[str, str], secret_files: Mapping[str, str] | None
) -> NotificationDestination:
    field = f"notifications.destinations.{name}"
    keys = {
        "adapter",
        "enabled",
        "enabled_events",
        "endpoint",
        "timeout_seconds",
        "retries",
        "deduplication",
        "batching",
        "redaction",
        "failure_policy",
    }
    raw = exact_dict(value, field, keys)
    adapter = exact_str(raw["adapter"], f"{field}.adapter")
    if adapter not in NOTIFICATION_ADAPTERS:
        fail(
            f"{field}.adapter",
            f"unknown adapter; allowed values are {sorted(NOTIFICATION_ADAPTERS)}",
        )
    enabled = exact_bool(raw["enabled"], f"{field}.enabled")
    events = unique_strings(
        raw["enabled_events"], f"{field}.enabled_events", EVENTS, empty=not enabled
    )
    if enabled and not events:
        fail(f"{field}.enabled_events", "enabled destination requires events")
    if not enabled and events:
        fail(f"{field}.enabled_events", "disabled destination requires an empty list")
    endpoint = _secret_ref(raw["endpoint"], f"{field}.endpoint")

    retries_raw = exact_dict(
        raw["retries"], f"{field}.retries", {"maximum_attempts", "backoff_seconds"}
    )
    attempts = bounded_int(
        retries_raw["maximum_attempts"], f"{field}.retries.maximum_attempts", 1, 5
    )
    backoff_values = exact_list(retries_raw["backoff_seconds"], f"{field}.retries.backoff_seconds")
    backoffs = tuple(
        bounded_int(item, f"{field}.retries.backoff_seconds[{index}]", 1, 300)
        for index, item in enumerate(backoff_values)
    )
    if len(backoffs) != attempts - 1 or tuple(sorted(set(backoffs))) != backoffs:
        fail(f"{field}.retries.backoff_seconds", "requires one strictly increasing value per retry")
    dedupe_raw = exact_dict(
        raw["deduplication"], f"{field}.deduplication", {"window_seconds", "key_fields"}
    )
    dedupe_keys = unique_strings(
        dedupe_raw["key_fields"], f"{field}.deduplication.key_fields", DEDUPE_KEYS
    )
    if not {"event_type", "run_id"}.issubset(dedupe_keys):
        fail(f"{field}.deduplication.key_fields", "must include event_type and run_id")
    batching_raw = exact_dict(
        raw["batching"], f"{field}.batching", {"maximum_events", "flush_seconds"}
    )
    redaction_raw = exact_dict(
        raw["redaction"], f"{field}.redaction", {"headers", "query_parameters"}
    )
    policy = exact_str(raw["failure_policy"], f"{field}.failure_policy")
    if policy != "warning":
        fail(f"{field}.failure_policy", "only warning is allowed in v1")
    return NotificationDestination(
        adapter,
        enabled,
        events,
        endpoint,
        bounded_int(raw["timeout_seconds"], f"{field}.timeout_seconds", 1, 60),
        RetryConfig(attempts, backoffs),
        DeduplicationConfig(
            bounded_int(
                dedupe_raw["window_seconds"], f"{field}.deduplication.window_seconds", 1, 86400
            ),
            dedupe_keys,
        ),
        BatchingConfig(
            bounded_int(
                batching_raw["maximum_events"], f"{field}.batching.maximum_events", 1, 1000
            ),
            bounded_int(batching_raw["flush_seconds"], f"{field}.batching.flush_seconds", 1, 300),
        ),
        RedactionConfig(
            unique_strings(redaction_raw["headers"], f"{field}.redaction.headers", HEADERS),
            unique_strings(
                redaction_raw["query_parameters"],
                f"{field}.redaction.query_parameters",
                QUERY_PARAMETERS,
            ),
        ),
        policy,
    )


def load_notifications_text(
    text: str,
    *,
    environ: Mapping[str, str] | None = None,
    secret_files: Mapping[str, str] | None = None,
) -> NotificationConfig:
    raw = exact_dict(_root(text, "notifications"), "notifications", {"enabled", "destinations"})
    enabled = exact_bool(raw["enabled"], "notifications.enabled")
    destinations_raw = exact_dict(raw["destinations"], "notifications.destinations")
    if enabled and not 1 <= len(destinations_raw) <= 16:
        fail("notifications.destinations", "enabled notifications require 1..16 destinations")
    if not enabled and destinations_raw:
        fail("notifications.destinations", "disabled notifications require an empty map")
    environment = os.environ if environ is None else environ
    destinations: dict[str, NotificationDestination] = {}
    for name, value in destinations_raw.items():
        if DESTINATION_RE.fullmatch(name) is None:
            fail(f"notifications.destinations.{name}", "invalid destination identifier")
        destinations[name] = _notification_destination(name, value, environment, secret_files)
    return NotificationConfig(enabled, frozen_mapping(destinations))


def load_notifications(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    secret_files: Mapping[str, str] | None = None,
) -> NotificationConfig:
    return load_notifications_text(
        Path(path).read_text(encoding="utf-8"), environ=environ, secret_files=secret_files
    )


def _signal_parameters(
    plugin_id: str, value: object, field: str
) -> Mapping[str, None | bool | int | str]:
    raw = exact_dict(value, field)
    result: dict[str, None | bool | int | str]
    if plugin_id == "smc.swing_structure":
        raw = exact_dict(raw, field, {"swing_length", "show_labels"})
        result = {
            "swing_length": bounded_int(raw["swing_length"], f"{field}.swing_length", 10, 10000),
            "show_labels": exact_bool(raw["show_labels"], f"{field}.show_labels"),
        }
    elif plugin_id == "smc.equal_high_low":
        raw = exact_dict(raw, field, {"confirmation_bars", "threshold_atr_fraction"})
        result = {
            "confirmation_bars": bounded_int(
                raw["confirmation_bars"], f"{field}.confirmation_bars", 1, 10000
            ),
            "threshold_atr_fraction": canonical_decimal(
                raw["threshold_atr_fraction"], f"{field}.threshold_atr_fraction", maximum="0.5"
            ),
        }
    elif plugin_id == "smc.order_block":
        raw = exact_dict(
            raw, field, {"scope", "volatility_filter", "mitigation_source", "maximum_blocks"}
        )
        scope = exact_str(raw["scope"], f"{field}.scope")
        volatility = exact_str(raw["volatility_filter"], f"{field}.volatility_filter")
        mitigation = exact_str(raw["mitigation_source"], f"{field}.mitigation_source")
        if (
            scope not in {"swing", "internal"}
            or volatility not in {"atr", "cumulative_range"}
            or mitigation not in {"close", "high_low"}
        ):
            fail(field, "unknown order-block parameter value")
        result = {
            "scope": scope,
            "volatility_filter": volatility,
            "mitigation_source": mitigation,
            "maximum_blocks": bounded_int(raw["maximum_blocks"], f"{field}.maximum_blocks", 1, 100),
        }
    elif plugin_id == "ict.clustered_liquidity":
        raw = exact_dict(raw, field, {"pivot_width", "minimum_pivots", "margin_atr_fraction"})
        result = {
            "pivot_width": bounded_int(raw["pivot_width"], f"{field}.pivot_width", 3, 10),
            "minimum_pivots": bounded_int(raw["minimum_pivots"], f"{field}.minimum_pivots", 3, 50),
            "margin_atr_fraction": ict_margin_fraction(
                raw["margin_atr_fraction"], f"{field}.margin_atr_fraction"
            ),
        }
    elif plugin_id == "ict.market_structure":
        raw = exact_dict(raw, field, {"pivot_width", "emit_mss", "emit_bos"})
        emit_mss = exact_bool(raw["emit_mss"], f"{field}.emit_mss")
        emit_bos = exact_bool(raw["emit_bos"], f"{field}.emit_bos")
        if not emit_mss and not emit_bos:
            fail(field, "at least one structure event must be enabled")
        result = {
            "pivot_width": bounded_int(raw["pivot_width"], f"{field}.pivot_width", 3, 10),
            "emit_mss": emit_mss,
            "emit_bos": emit_bos,
        }
    elif plugin_id == "ict.fair_value_gap":
        raw = exact_dict(
            raw, field, {"kind", "require_displacement", "displacement_length", "mitigation"}
        )
        kind = exact_str(raw["kind"], f"{field}.kind")
        mitigation = exact_str(raw["mitigation"], f"{field}.mitigation")
        if kind != "ordinary" or mitigation != "full_traversal":
            fail(field, "unsupported fair-value-gap parameter")
        result = {
            "kind": kind,
            "require_displacement": exact_bool(
                raw["require_displacement"], f"{field}.require_displacement"
            ),
            "displacement_length": bounded_int(
                raw["displacement_length"], f"{field}.displacement_length", 1, 10000
            ),
            "mitigation": mitigation,
        }
    elif plugin_id == "project.risk_levels":
        raw = exact_dict(raw, field, {"minimum_reward_risk"})
        result = {
            "minimum_reward_risk": canonical_decimal(
                raw["minimum_reward_risk"], f"{field}.minimum_reward_risk", positive=True
            )
        }
    else:
        fail(field, f"unknown plugin ID {plugin_id!r}")
    return frozen_mapping(result)


def load_strategy_text(text: str, *, allow_deferred: bool = False) -> StrategyConfig:
    raw = exact_dict(
        load_yaml_12(text),
        "strategy",
        {"name", "version", "instruments", "history_minutes", "roles", "signals"},
    )
    name = exact_str(raw["name"], "strategy.name")
    if NAME_RE.fullmatch(name) is None:
        fail("strategy.name", "invalid strategy identifier")
    version = exact_str(raw["version"], "strategy.version")
    if not 1 <= len(version) <= 32:
        fail("strategy.version", "expected 1..32 characters")
    instruments = tuple(
        instrument(item, f"strategy.instruments[{index}]")
        for index, item in enumerate(exact_list(raw["instruments"], "strategy.instruments"))
    )
    if not instruments or len(set(instruments)) != len(instruments):
        fail("strategy.instruments", "must be nonempty and duplicate-free")
    roles_raw = exact_dict(raw["roles"], "strategy.roles")
    if not roles_raw:
        fail("strategy.roles", "must not be empty")
    roles: dict[str, str] = {}
    for role, timeframe_value in roles_raw.items():
        if ROLE_RE.fullmatch(role) is None:
            fail(f"strategy.roles.{role}", "invalid role identifier")
        timeframe = exact_str(timeframe_value, f"strategy.roles.{role}")
        try:
            timeframe = str(Timeframe(timeframe))
        except ValueError:
            fail(
                f"strategy.roles.{role}",
                f"unknown timeframe; allowed values are {sorted(TIMEFRAMES)}",
            )
        roles[role] = timeframe
    signal_values = exact_list(raw["signals"], "strategy.signals")
    if not signal_values:
        fail("strategy.signals", "must not be empty")
    signals: list[SignalConfig] = []
    seen_ids: set[str] = set()
    previous_order = -1
    for index, value in enumerate(signal_values):
        field = f"strategy.signals[{index}]"
        signal = exact_dict(value, field, SIGNAL_FIELDS)
        plugin_id = exact_str(signal["id"], f"{field}.id")
        if plugin_id not in IMPLEMENTED_PLUGIN_IDS or plugin_id in seen_ids:
            fail(f"{field}.id", "unknown or duplicate plugin ID")
        role = exact_str(signal["role"], f"{field}.role")
        if role not in roles:
            fail(f"{field}.role", "unknown role")
        dependencies = unique_strings(signal["depends_on"], f"{field}.depends_on", empty=True)
        if any(dependency not in seen_ids for dependency in dependencies):
            fail(f"{field}.depends_on", "dependencies must name earlier configured signals")
        expected_role, expected_timeframes, expected_dependencies = PLUGIN_CONTRACTS[plugin_id]
        if role != expected_role:
            fail(f"{field}.role", f"configured role must be {expected_role}")
        if roles[role] not in expected_timeframes:
            fail(
                f"strategy.roles.{role}",
                f"configured timeframe must be one of {sorted(expected_timeframes)}",
            )
        if dependencies != expected_dependencies:
            fail(
                f"{field}.depends_on",
                f"configured dependencies must be {list(expected_dependencies)}",
            )
        order = bounded_int(signal["order"], f"{field}.order", 0, 2**31 - 1)
        if order <= previous_order:
            fail(f"{field}.order", "orders must be strictly increasing")
        effect = exact_str(signal["effect"], f"{field}.effect")
        if effect not in {"REJECT", "LEVELS"}:
            fail(f"{field}.effect", "allowed values are REJECT and LEVELS")
        signals.append(
            SignalConfig(
                plugin_id,
                role,
                dependencies,
                _signal_parameters(plugin_id, signal["parameters"], f"{field}.parameters"),
                exact_bool(signal["required"], f"{field}.required"),
                effect,
                order,
            )
        )
        seen_ids.add(plugin_id)
        previous_order = order
    config = StrategyConfig(
        name,
        version,
        instruments,
        bounded_int(raw["history_minutes"], "strategy.history_minutes", 1, 10_000_000),
        frozen_mapping(roles),
        tuple(signals),
    )
    return config


def load_strategy(path: str | Path, *, allow_deferred: bool = False) -> StrategyConfig:
    return load_strategy_text(Path(path).read_text(encoding="utf-8"), allow_deferred=allow_deferred)
