"""Strict YAML 1.2 configuration loading at the public error boundary."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

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
    MarketDataConfig,
    MarketDataDocument,
    NotificationConfig,
    NotificationDocument,
    ScheduleConfig,
    ScheduleDocument,
    StrategyConfig,
)
from .yaml_loader import load_yaml_12


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
