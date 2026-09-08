"""Strict YAML 1.2 configuration loading at the public error boundary."""

from __future__ import annotations

import re
from calendar import timegm
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path, PurePosixPath
from typing import Never

from pydantic import BaseModel, ValidationError
from pydantic_core import ErrorDetails

from .errors import StrictConfigurationError
from .models import (
    DEFERRED_PLUGIN_IDS as DEFERRED_PLUGIN_IDS,
)
from .models import (
    IMPLEMENTED_PLUGIN_IDS as IMPLEMENTED_PLUGIN_IDS,
)
from .models import (
    BacktestCostConfig,
    BacktestEntryConfig,
    BacktestExecutionConfig,
    BacktestOutputConfig,
    BacktestPeriod,
    BacktestScenarioConfig,
    MarketDataConfig,
    MarketDataDocument,
    NotificationConfig,
    NotificationDocument,
    ScheduleConfig,
    ScheduleDocument,
    StrategyConfig,
)
from .yaml_loader import CanonicalValue, load_yaml_12

NAME_RE = re.compile(r"[a-z][a-z0-9-]{0,62}[a-z0-9]\Z")
DECIMAL_RE = re.compile(r"(0|[1-9][0-9]*)(\.[0-9]+)?\Z")


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


def _root(text: str, authority: str) -> dict[str, CanonicalValue]:
    loaded = exact_dict(load_yaml_12(text), "$", {authority})
    return exact_dict(loaded[authority], authority)


def _location(parts: tuple[str | int, ...]) -> str:
    result = ""
    for part in parts:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            result += ("." if result else "") + part
    return result or "$"


def _message(error: ErrorDetails) -> str:
    error_type = error["type"]
    if error_type == "missing":
        return "required field is missing"
    if error_type in {"extra_forbidden", "unexpected_keyword_argument"}:
        return "unknown field"
    context = error.get("ctx")
    if isinstance(context, dict) and "error" in context:
        return str(context["error"])
    message = str(error["msg"])
    if message.startswith("Input should be a valid "):
        return "expected " + message.removeprefix("Input should be a valid ")
    return message


def _validate[ModelT: BaseModel](model: type[ModelT], value: object) -> ModelT:
    try:
        return model.model_validate(value, context={"external": True})
    except ValidationError as exc:
        first = exc.errors(include_url=False, include_input=False)[0]
        location = _location(first["loc"])
        message = _message(first)
        if message.startswith("batching.maximum_events: "):
            location += ".batching.maximum_events"
            message = message.removeprefix("batching.maximum_events: ")
        raise StrictConfigurationError(f"{location}: {message}") from exc


def load_market_data_text(text: str) -> MarketDataConfig:
    return _validate(MarketDataDocument, load_yaml_12(text)).market_data


def load_market_data(path: str | Path) -> MarketDataConfig:
    return load_market_data_text(Path(path).read_text(encoding="utf-8"))


def load_schedule_text(text: str) -> ScheduleConfig:
    return _validate(ScheduleDocument, load_yaml_12(text)).schedule


def load_schedule(path: str | Path) -> ScheduleConfig:
    return load_schedule_text(Path(path).read_text(encoding="utf-8"))


def load_notifications_text(
    text: str,
    *,
    environ: Mapping[str, str] | None = None,
    secret_files: Mapping[str, str] | None = None,
) -> NotificationConfig:
    del environ, secret_files
    return _validate(NotificationDocument, load_yaml_12(text)).notifications


def load_notifications(
    path: str | Path,
    *,
    environ: Mapping[str, str] | None = None,
    secret_files: Mapping[str, str] | None = None,
) -> NotificationConfig:
    return load_notifications_text(
        Path(path).read_text(encoding="utf-8"), environ=environ, secret_files=secret_files
    )


def load_strategy_text(text: str, *, allow_deferred: bool = False) -> StrategyConfig:
    del allow_deferred
    return _validate(StrategyConfig, load_yaml_12(text))


def load_strategy(path: str | Path, *, allow_deferred: bool = False) -> StrategyConfig:
    return load_strategy_text(Path(path).read_text(encoding="utf-8"), allow_deferred=allow_deferred)


def _utc_minute(value: object, field: str) -> int:
    text = exact_str(value, field)
    match = re.fullmatch(r"([0-9]{4})-([0-9]{2})-([0-9]{2})T([0-9]{2}):([0-9]{2}):00Z", text)
    if match is None:
        fail(field, "expected a canonical UTC minute timestamp")
    try:
        year, month, day, hour, minute = (int(part) for part in match.groups())
        value_utc = datetime(year, month, day, hour, minute, tzinfo=UTC)
    except ValueError as exc:
        raise StrictConfigurationError(f"{field}: invalid UTC minute timestamp") from exc
    return timegm(value_utc.utctimetuple()) * 1000


def _canonical_backtest_decimal(value: object, field: str) -> str:
    text = exact_str(value, field)
    normalized = canonical_decimal(text, field)
    if text != normalized:
        fail(field, "expected a canonical decimal string")
    return text


def load_backtest_text(text: str) -> BacktestScenarioConfig:
    raw = exact_dict(
        _root(text, "backtest"),
        "backtest",
        {"name", "version", "strategy", "period", "entry", "execution", "costs", "output"},
    )
    name = exact_str(raw["name"], "backtest.name")
    if NAME_RE.fullmatch(name) is None:
        fail("backtest.name", "invalid backtest identifier")
    version = exact_str(raw["version"], "backtest.version")
    if not 1 <= len(version) <= 32:
        fail("backtest.version", "expected 1..32 characters")
    strategy = exact_str(raw["strategy"], "backtest.strategy")
    strategy_path = PurePosixPath(strategy)
    if strategy_path.name != strategy or strategy_path.suffix not in {".yaml", ".yml"}:
        fail("backtest.strategy", "expected one normalized strategy leaf filename")

    period_raw = exact_dict(raw["period"], "backtest.period", {"start", "end"})
    period = BacktestPeriod(
        _utc_minute(period_raw["start"], "backtest.period.start"),
        _utc_minute(period_raw["end"], "backtest.period.end"),
    )
    if period.start_ms >= period.end_ms:
        fail("backtest.period", "start must precede end")

    entry_raw = exact_dict(raw["entry"], "backtest.entry", {"mode", "expiry_execution_bars"})
    mode = exact_str(entry_raw["mode"], "backtest.entry.mode")
    if mode != "touch_limit":
        fail("backtest.entry.mode", "only touch_limit is allowed in v1")
    entry = BacktestEntryConfig(
        mode,
        bounded_int(
            entry_raw["expiry_execution_bars"], "backtest.entry.expiry_execution_bars", 1, 10_000
        ),
    )

    execution_raw = exact_dict(
        raw["execution"],
        "backtest.execution",
        {"maximum_holding_minutes", "intrabar_conflict", "allow_same_minute_target"},
    )
    conflict = exact_str(execution_raw["intrabar_conflict"], "backtest.execution.intrabar_conflict")
    if conflict != "stop_first":
        fail("backtest.execution.intrabar_conflict", "only stop_first is allowed in v1")
    same_minute = exact_bool(
        execution_raw["allow_same_minute_target"], "backtest.execution.allow_same_minute_target"
    )
    if same_minute:
        fail("backtest.execution.allow_same_minute_target", "must be false in v1")
    execution = BacktestExecutionConfig(
        bounded_int(
            execution_raw["maximum_holding_minutes"],
            "backtest.execution.maximum_holding_minutes",
            1,
            100_000,
        ),
        conflict,
        same_minute,
    )

    costs_raw = exact_dict(
        raw["costs"], "backtest.costs", {"taker_fee_bps", "adverse_slippage_bps"}
    )
    costs = BacktestCostConfig(
        _canonical_backtest_decimal(costs_raw["taker_fee_bps"], "backtest.costs.taker_fee_bps"),
        _canonical_backtest_decimal(
            costs_raw["adverse_slippage_bps"], "backtest.costs.adverse_slippage_bps"
        ),
    )
    output_raw = exact_dict(raw["output"], "backtest.output", {"existing_result"})
    existing_result = exact_str(output_raw["existing_result"], "backtest.output.existing_result")
    if existing_result != "fail":
        fail("backtest.output.existing_result", "only fail is allowed in v1")
    return BacktestScenarioConfig(
        name,
        version,
        strategy,
        period,
        entry,
        execution,
        costs,
        BacktestOutputConfig(existing_result),
    )


def load_backtest(path: str | Path) -> BacktestScenarioConfig:
    return load_backtest_text(Path(path).read_text(encoding="utf-8"))


def resolve_backtest_strategy_path(
    scenario_path: str | Path, scenario: BacktestScenarioConfig
) -> Path:
    scenario_file = Path(scenario_path)
    try:
        project_root = scenario_file.parents[2]
    except IndexError:
        fail("backtest.strategy", "scenario path must be below backtests/<strategy-id>")
    candidate = project_root / "strategies" / scenario.strategy
    strategies = (project_root / "strategies").resolve()
    if candidate.resolve().parent != strategies or not candidate.is_file():
        fail("backtest.strategy", "exact strategy file does not exist below strategies")
    return candidate
