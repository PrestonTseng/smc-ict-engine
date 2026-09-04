"""Strict YAML 1.2 loading and immutable configuration contracts."""

from .errors import DeferredPluginError, StrictConfigurationError
from .hashing import (
    hash_backtest,
    hash_market_data,
    hash_notifications,
    hash_schedule,
    hash_strategy,
)
from .loaders import (
    DEFERRED_PLUGIN_IDS,
    IMPLEMENTED_PLUGIN_IDS,
    load_backtest,
    load_backtest_text,
    load_market_data,
    load_market_data_text,
    load_notifications,
    load_notifications_text,
    load_schedule,
    load_schedule_text,
    load_strategy,
    load_strategy_text,
    resolve_backtest_strategy_path,
)
from .models import (
    BacktestScenarioConfig,
    MarketDataConfig,
    NotificationConfig,
    ScheduleConfig,
    StrategyConfig,
)

__all__ = [
    "DEFERRED_PLUGIN_IDS",
    "IMPLEMENTED_PLUGIN_IDS",
    "BacktestScenarioConfig",
    "DeferredPluginError",
    "MarketDataConfig",
    "NotificationConfig",
    "ScheduleConfig",
    "StrategyConfig",
    "StrictConfigurationError",
    "hash_backtest",
    "hash_market_data",
    "hash_notifications",
    "hash_schedule",
    "hash_strategy",
    "load_backtest",
    "load_backtest_text",
    "load_market_data",
    "load_market_data_text",
    "load_notifications",
    "load_notifications_text",
    "load_schedule",
    "load_schedule_text",
    "load_strategy",
    "load_strategy_text",
    "resolve_backtest_strategy_path",
]
