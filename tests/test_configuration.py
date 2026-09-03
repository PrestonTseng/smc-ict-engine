from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import BaseModel, ValidationError

MARKET = """\
market_data:
  provider: binance_usdm
  market_type: LINEAR_PERPETUAL
  instruments:
    BTC-USDT-PERP: BTCUSDT
    ETH-USDT-PERP: ETHUSDT
"""

SCHEDULE = """\
schedule:
  enabled: true
  timezone: UTC
  jobs:
    - id: source-aligned-research
      cron: "7 */1 * * *"
      strategy: /config/strategies/source-aligned-research.yaml
      market_data: /config/market-data.yaml
      notifications: /config/notifications.yaml
      misfire_policy: skip
      misfire_grace_seconds: 120
      overlap_policy: skip
      maximum_runtime_seconds: 900
      startup_delay_seconds: 5
"""

NOTIFICATIONS = """\
notifications:
  enabled: true
  destinations:
    discord_1:
      adapter: generic_webhook
      enabled: true
      enabled_events: [run_started, run_succeeded, run_failed, decision_found, no_decision]
      endpoint: {env: DISCORD_1_WEBHOOK_URL}
      timeout_seconds: 5
      retries: {maximum_attempts: 3, backoff_seconds: [1, 2]}
      deduplication:
        window_seconds: 300
        key_fields: [event_type, run_id, instrument_id]
      batching: {maximum_events: 20, flush_seconds: 2}
      redaction:
        headers: [authorization, cookie, set-cookie]
        query_parameters: [token, key, signature]
      failure_policy: warning
    discord_2:
      adapter: generic_webhook
      enabled: true
      enabled_events: [decision_found]
      endpoint: {file: /run/secrets/discord_2_webhook_url}
      timeout_seconds: 5
      retries: {maximum_attempts: 2, backoff_seconds: [1]}
      deduplication:
        window_seconds: 300
        key_fields: [event_type, run_id, instrument_id]
      batching: {maximum_events: 10, flush_seconds: 2}
      redaction:
        headers: [authorization, cookie, set-cookie]
        query_parameters: [token, key, signature]
      failure_policy: warning
"""

STRATEGY = """\
name: source-aligned-research
version: "1"
instruments: [BTC-USDT-PERP, ETH-USDT-PERP]
history_minutes: 129600
roles: {regime: 4h, context: 1h, execution: 5m}
signals:
  - id: smc.swing_structure
    role: regime
    depends_on: []
    parameters: {swing_length: 50, show_labels: true}
    required: true
    effect: REJECT
    order: 10
  - id: smc.equal_high_low
    role: context
    depends_on: [smc.swing_structure]
    parameters: {confirmation_bars: 3, threshold_atr_fraction: "0.10"}
    required: true
    effect: REJECT
    order: 20
  - id: smc.order_block
    role: context
    depends_on: [smc.swing_structure]
    parameters: {scope: swing, volatility_filter: atr, mitigation_source: close, maximum_blocks: 5}
    required: true
    effect: REJECT
    order: 30
  - id: ict.clustered_liquidity
    role: execution
    depends_on: [smc.equal_high_low]
    parameters: {pivot_width: 5, minimum_pivots: 3, margin_atr_fraction: "0.4"}
    required: true
    effect: REJECT
    order: 40
  - id: ict.market_structure
    role: execution
    depends_on: [ict.clustered_liquidity]
    parameters: {pivot_width: 5, emit_mss: true, emit_bos: true}
    required: true
    effect: REJECT
    order: 50
  - id: ict.fair_value_gap
    role: execution
    depends_on: [ict.market_structure]
    parameters:
      kind: ordinary
      require_displacement: true
      displacement_length: 20
      mitigation: full_traversal
    required: true
    effect: REJECT
    order: 60
  - id: project.risk_levels
    role: execution
    depends_on: [ict.clustered_liquidity, ict.fair_value_gap]
    parameters: {minimum_reward_risk: "2.0"}
    required: true
    effect: LEVELS
    order: 70
"""


def api():
    from smc_ict.configuration import (
        DeferredPluginError,
        StrictConfigurationError,
        hash_market_data,
        hash_notifications,
        hash_schedule,
        hash_strategy,
        load_market_data_text,
        load_notifications_text,
        load_schedule_text,
        load_strategy_text,
    )

    return {
        "DeferredPluginError": DeferredPluginError,
        "StrictConfigurationError": StrictConfigurationError,
        "hash_market_data": hash_market_data,
        "hash_notifications": hash_notifications,
        "hash_schedule": hash_schedule,
        "hash_strategy": hash_strategy,
        "load_market_data_text": load_market_data_text,
        "load_notifications_text": load_notifications_text,
        "load_schedule_text": load_schedule_text,
        "load_strategy_text": load_strategy_text,
    }


def notification_kwargs() -> dict[str, object]:
    return {
        "environ": {"DISCORD_1_WEBHOOK_URL": "https://endpoint.invalid/one"},
        "secret_files": {"/run/secrets/discord_2_webhook_url": "https://endpoint.invalid/two"},
    }


def test_configuration_models_are_strict_frozen_pydantic_models() -> None:
    from smc_ict.configuration.models import MarketDataConfig

    assert issubclass(MarketDataConfig, BaseModel)
    assert MarketDataConfig.model_config["strict"] is True
    assert MarketDataConfig.model_config["frozen"] is True
    assert MarketDataConfig.model_config["extra"] == "forbid"

    with pytest.raises(ValidationError):
        MarketDataConfig.model_validate(
            {
                "provider": "binance_usdm",
                "market_type": "LINEAR_PERPETUAL",
                "instruments": {"BTC-USDT-PERP": 1},
            }
        )
    with pytest.raises(ValidationError):
        MarketDataConfig.model_validate(
            {
                "provider": "binance_usdm",
                "market_type": "LINEAR_PERPETUAL",
                "instruments": {"BTC-USDT-PERP": "BTCUSDT"},
                "endpoint": "https://example.invalid",
            }
        )


def test_schedule_job_and_strategy_config_accept_parent_positional_constructors() -> None:
    from smc_ict.configuration.models import ScheduleJob, SignalConfig, StrategyConfig

    job_values = (
        "research",
        "7 */1 * * *",
        "/config/strategies/research.yaml",
        "/config/market-data.yaml",
        "/config/notifications.yaml",
        "skip",
        120,
        "skip",
        900,
        5,
    )
    job = ScheduleJob(*job_values)
    keyword_job = ScheduleJob(
        id="research",
        cron="7 */1 * * *",
        strategy="/config/strategies/research.yaml",
        market_data="/config/market-data.yaml",
        notifications="/config/notifications.yaml",
        misfire_policy="skip",
        misfire_grace_seconds=120,
        overlap_policy="skip",
        maximum_runtime_seconds=900,
        startup_delay_seconds=5,
    )
    signal = SignalConfig(
        "smc.swing_structure",
        "regime",
        (),
        {"swing_length": 50, "show_labels": True},
        True,
        "REJECT",
        10,
    )
    strategy = StrategyConfig("research", "1", ("BTC-USDT-PERP",), 60, {"regime": "4h"}, (signal,))
    keyword_strategy = StrategyConfig(
        name="research",
        version="1",
        instruments=("BTC-USDT-PERP",),
        history_minutes=60,
        roles={"regime": "4h"},
        signals=(signal,),
    )

    assert job.id == "research"
    assert keyword_job == job
    assert strategy.name == "research"
    assert keyword_strategy == strategy


def test_schedule_job_and_strategy_config_reject_wrong_positional_argument_counts() -> None:
    from smc_ict.configuration.models import ScheduleJob, StrategyConfig

    job_values = (
        "research",
        "7 */1 * * *",
        "/config/strategies/research.yaml",
        "/config/market-data.yaml",
        "/config/notifications.yaml",
        "skip",
        120,
        "skip",
        900,
        5,
    )

    with pytest.raises(TypeError):
        ScheduleJob("research")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ScheduleJob(*(["research"] * 11))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        StrategyConfig("research")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        StrategyConfig(*(["research"] * 7))  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        ScheduleJob(*job_values, id="other")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        StrategyConfig(*(["research"] * 6), name="other")  # type: ignore[call-arg]


def test_loaded_configuration_is_deeply_immutable() -> None:
    a = api()
    market = a["load_market_data_text"](MARKET)
    strategy = a["load_strategy_text"](STRATEGY)
    notifications = a["load_notifications_text"](NOTIFICATIONS, **notification_kwargs())

    with pytest.raises(ValidationError):
        market.provider = "okx_swap"
    with pytest.raises(TypeError):
        market.instruments["BTC-USDT-PERP"] = "changed"
    with pytest.raises(TypeError):
        strategy.roles["execution"] = "15m"
    with pytest.raises(TypeError):
        strategy.signals[0].parameters["swing_length"] = 10
    with pytest.raises(TypeError):
        notifications.destinations["new"] = notifications.destinations["discord_1"]


def test_loader_adapts_pydantic_errors_to_stable_public_error() -> None:
    a = api()

    with pytest.raises(a["StrictConfigurationError"]) as caught:
        a["load_schedule_text"](
            SCHEDULE.replace("maximum_runtime_seconds: 900", 'maximum_runtime_seconds: "900"')
        )

    message = str(caught.value)
    assert isinstance(caught.value.__cause__, ValidationError)
    assert "schedule.jobs[0].maximum_runtime_seconds" in message
    assert "validation error for" not in message
    assert "errors.pydantic.dev" not in message


def test_market_configuration_is_frozen_and_hashes_canonical_json() -> None:
    a = api()
    config = a["load_market_data_text"](MARKET)
    assert config.provider == "binance_usdm"
    assert config.instruments["BTC-USDT-PERP"] == "BTCUSDT"
    with pytest.raises(TypeError):
        config.instruments["BTC-USDT-PERP"] = "changed"

    expected = hashlib.sha256(
        b"market-config-v1\0"
        + json.dumps(
            {
                "instruments": {
                    "BTC-USDT-PERP": "BTCUSDT",
                    "ETH-USDT-PERP": "ETHUSDT",
                },
                "market_type": "LINEAR_PERPETUAL",
                "provider": "binance_usdm",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    assert a["hash_market_data"](config) == expected


@pytest.mark.parametrize(
    "text",
    [
        "market_data: {provider: binance_usdm, provider: okx_swap, "
        "market_type: LINEAR_PERPETUAL, instruments: {BTC-USDT-PERP: BTCUSDT}}",
        "market_data: &m {provider: binance_usdm, market_type: LINEAR_PERPETUAL, "
        "instruments: {BTC-USDT-PERP: BTCUSDT}}",
        "market_data: !unsafe {provider: binance_usdm, market_type: LINEAR_PERPETUAL, "
        "instruments: {BTC-USDT-PERP: BTCUSDT}}",
        MARKET + "---\nmarket_data: {}\n",
        "market_data: {provider: binance_usdm, market_type: LINEAR_PERPETUAL, "
        "instruments: {1: BTCUSDT}}",
        "market_data: {provider: yes, market_type: LINEAR_PERPETUAL, "
        "instruments: {BTC-USDT-PERP: BTCUSDT}}",
    ],
)
def test_strict_yaml_rejects_unsafe_or_ambiguous_syntax(text: str) -> None:
    a = api()
    with pytest.raises(a["StrictConfigurationError"]):
        a["load_market_data_text"](text)


def test_market_rejects_unknown_fields_and_ids() -> None:
    a = api()
    bad = MARKET.replace("binance_usdm", "mystery")
    with pytest.raises(a["StrictConfigurationError"], match="provider"):
        a["load_market_data_text"](bad)
    with pytest.raises(a["StrictConfigurationError"], match="unknown"):
        a["load_market_data_text"](
            MARKET.replace("  instruments:", "  endpoint: x\n  instruments:")
        )


def test_schedule_accepts_only_utc_strict_bounds_paths_and_cron() -> None:
    a = api()
    config = a["load_schedule_text"](SCHEDULE)
    assert config.jobs[0].cron == "7 */1 * * *"
    assert len(a["hash_schedule"](config)) == 64

    replacements = [
        ("timezone: UTC", "timezone: Asia/Taipei"),
        ("maximum_runtime_seconds: 900", "maximum_runtime_seconds: true"),
        ("misfire_grace_seconds: 120", "misfire_grace_seconds: 0"),
        ("startup_delay_seconds: 5", "startup_delay_seconds: 301"),
        ('cron: "7 */1 * * *"', 'cron: "7 * * * * *"'),
        ('cron: "7 */1 * * *"', 'cron: "60 * * * *"'),
        (
            "strategy: /config/strategies/source-aligned-research.yaml",
            "strategy: /config/../secret.yaml",
        ),
    ]
    for old, new in replacements:
        with pytest.raises(a["StrictConfigurationError"]):
            a["load_schedule_text"](SCHEDULE.replace(old, new))


@pytest.mark.parametrize(
    "strategy_path",
    ["/config/strategy.yaml", "/strategies/strategy.yml"],
)
def test_schedule_accepts_each_supported_read_only_root(strategy_path: str) -> None:
    a = api()
    config = a["load_schedule_text"](
        SCHEDULE.replace("/config/strategies/source-aligned-research.yaml", strategy_path)
    )
    assert config.jobs[0].strategy == strategy_path


@pytest.mark.parametrize(
    "strategy_path",
    [
        "strategies/strategy.yaml",
        "/unsupported/strategy.yaml",
        "/config/../strategy.yaml",
        "/strategies/../strategy.yaml",
        "/config/strategy.json",
    ],
)
def test_schedule_rejects_relative_escape_and_unsupported_paths(strategy_path: str) -> None:
    a = api()
    with pytest.raises(a["StrictConfigurationError"]):
        a["load_schedule_text"](
            SCHEDULE.replace("/config/strategies/source-aligned-research.yaml", strategy_path)
        )


def test_notifications_load_two_destinations_without_retaining_secrets() -> None:
    a = api()
    config = a["load_notifications_text"](NOTIFICATIONS, **notification_kwargs())
    assert tuple(config.destinations) == ("discord_1", "discord_2")
    assert config.destinations["discord_2"].enabled_events == ("decision_found",)
    dumped = repr(config) + json.dumps(config.redacted_dict(), sort_keys=True)
    assert "endpoint.invalid" not in dumped
    assert config.destinations["discord_1"].endpoint.kind == "env"
    assert len(a["hash_notifications"](config)) == 64


def test_notifications_loader_accepts_native_discord_adapter() -> None:
    a = api()
    text = NOTIFICATIONS.replace("adapter: generic_webhook", "adapter: discord_webhook", 1).replace(
        "maximum_events: 20", "maximum_events: 10", 1
    )

    config = a["load_notifications_text"](text, **notification_kwargs())

    assert config.destinations["discord_1"].adapter == "discord_webhook"
    assert config.destinations["discord_2"].adapter == "generic_webhook"


@pytest.mark.parametrize("maximum_events", [11, 1000])
def test_notifications_loader_rejects_discord_batch_above_ten(maximum_events: int) -> None:
    a = api()
    text = NOTIFICATIONS.replace("adapter: generic_webhook", "adapter: discord_webhook", 1).replace(
        "maximum_events: 20", f"maximum_events: {maximum_events}", 1
    )

    with pytest.raises(
        a["StrictConfigurationError"],
        match=r"notifications\.destinations\.discord_1\.batching\.maximum_events: "
        rf"expected 1\.\.10, got {maximum_events}",
    ):
        a["load_notifications_text"](text, **notification_kwargs())


@pytest.mark.parametrize("maximum_events", [11, 1000])
def test_notifications_loader_preserves_generic_webhook_batch_bound(maximum_events: int) -> None:
    a = api()
    text = NOTIFICATIONS.replace("maximum_events: 20", f"maximum_events: {maximum_events}", 1)

    config = a["load_notifications_text"](text, **notification_kwargs())

    assert config.destinations["discord_1"].batching.maximum_events == maximum_events


@pytest.mark.parametrize(
    "old,new",
    [
        ("window_seconds: 300", "window_seconds: 0"),
        ("window_seconds: 300", "window_seconds: 86401"),
        ("window_seconds: 300", "window_seconds: true"),
        ("window_seconds: 300", 'window_seconds: "300"'),
        ("timeout_seconds: 5", "timeout_seconds: true"),
        ("backoff_seconds: [1, 2]", "backoff_seconds: [0, 2]"),
        ("adapter: generic_webhook", "adapter: unknown"),
        ("endpoint: {env: DISCORD_1_WEBHOOK_URL}", "endpoint: {url: https://literal.invalid/hook}"),
        ("enabled_events: [decision_found]", "enabled_events: []"),
        ("headers: [authorization, cookie, set-cookie]", "headers: []"),
        ("query_parameters: [token, key, signature]", "query_parameters: []"),
    ],
)
def test_notifications_reject_exact_type_bounds_and_closed_sets(old: str, new: str) -> None:
    a = api()
    with pytest.raises(a["StrictConfigurationError"]):
        a["load_notifications_text"](NOTIFICATIONS.replace(old, new, 1), **notification_kwargs())


def test_notifications_loader_retains_only_secret_references() -> None:
    a = api()
    config = a["load_notifications_text"](NOTIFICATIONS, environ={}, secret_files={})
    assert config.destinations["discord_1"].endpoint.kind == "env"
    assert config.destinations["discord_2"].endpoint.name == "/run/secrets/discord_2_webhook_url"


def test_strategy_normalizes_decimal_strings_for_implemented_plugins() -> None:
    a = api()
    config = a["load_strategy_text"](STRATEGY, allow_deferred=True)
    assert config.signals[1].parameters["threshold_atr_fraction"] == "0.1"
    assert len(a["hash_strategy"](config)) == 64
    assert a["load_strategy_text"](STRATEGY) == config


def test_strategy_accepts_exact_15m_execution_role_without_aliases_or_unsafe_types() -> None:
    a = api()
    exact = STRATEGY.replace("execution: 5m", "execution: 15m")

    assert a["load_strategy_text"](exact).roles["execution"] == "15m"

    for alias in ("15M", "015m", "900s", "quarter-hour"):
        with pytest.raises(a["StrictConfigurationError"], match="timeframe"):
            a["load_strategy_text"](STRATEGY.replace("execution: 5m", f"execution: {alias}"))
    for unsafe in ("15", "true", "null"):
        with pytest.raises(a["StrictConfigurationError"], match="expected string"):
            a["load_strategy_text"](STRATEGY.replace("execution: 5m", f"execution: {unsafe}"))


def test_strategy_rejects_wrong_authority_unknown_plugin_and_numeric_decimal() -> None:
    a = api()
    bads = [
        STRATEGY + "provider: binance_usdm\n",
        STRATEGY.replace("smc.swing_structure", "smc.unknown", 1),
        STRATEGY.replace('threshold_atr_fraction: "0.10"', "threshold_atr_fraction: 0.1"),
        STRATEGY.replace("swing_length: 50", "swing_length: true"),
    ]
    for bad in bads:
        with pytest.raises(a["StrictConfigurationError"]):
            a["load_strategy_text"](bad, allow_deferred=True)


def test_strategy_rejects_plugin_role_timeframe_and_dependency_contract_mismatches() -> None:
    a = api()

    with pytest.raises(a["StrictConfigurationError"], match="configured role"):
        a["load_strategy_text"](STRATEGY.replace("role: regime", "role: context", 1))
    with pytest.raises(a["StrictConfigurationError"], match="configured timeframe"):
        a["load_strategy_text"](
            STRATEGY.replace(
                "roles: {regime: 4h, context: 1h, execution: 5m}",
                "roles: {regime: 1h, context: 4h, execution: 5m}",
            )
        )
    with pytest.raises(a["StrictConfigurationError"], match="configured dependencies"):
        a["load_strategy_text"](
            STRATEGY.replace("depends_on: [smc.swing_structure]", "depends_on: []", 1)
        )


@pytest.mark.parametrize("width", [3, 5, 10])
def test_strategy_accepts_source_valid_ict_left_pivot_width_boundaries(width: int) -> None:
    a = api()
    text = STRATEGY.replace("pivot_width: 5", f"pivot_width: {width}")

    config = a["load_strategy_text"](text)

    assert config.signals[3].parameters["pivot_width"] == width
    assert config.signals[4].parameters["pivot_width"] == width


@pytest.mark.parametrize("width", [True, 2, 11])
def test_strategy_rejects_non_integer_or_out_of_range_ict_left_pivot_width(
    width: object,
) -> None:
    a = api()
    rendered = "true" if width is True else str(width)
    text = STRATEGY.replace("pivot_width: 5", f"pivot_width: {rendered}", 1)

    with pytest.raises(a["StrictConfigurationError"], match="pivot_width"):
        a["load_strategy_text"](text)


@pytest.mark.parametrize("margin", ["0.2", "0.4", "0.7"])
def test_strategy_accepts_only_source_representable_liquidity_margin_tenths(
    margin: str,
) -> None:
    a = api()
    text = STRATEGY.replace('margin_atr_fraction: "0.4"', f'margin_atr_fraction: "{margin}"')

    config = a["load_strategy_text"](text)

    assert config.signals[3].parameters["margin_atr_fraction"] == margin


@pytest.mark.parametrize("margin", ["0.1", "0.25", "0.8"])
def test_strategy_rejects_non_source_liquidity_margin_fraction(margin: str) -> None:
    a = api()
    text = STRATEGY.replace('margin_atr_fraction: "0.4"', f'margin_atr_fraction: "{margin}"')

    with pytest.raises(a["StrictConfigurationError"], match="margin_atr_fraction"):
        a["load_strategy_text"](text)


def test_checked_in_strategy_uses_source_default_ict_left_width_and_margin() -> None:
    a = api()
    path = Path(__file__).parents[1] / "strategies" / "source-aligned-research.yaml"

    config = a["load_strategy_text"](path.read_text(encoding="utf-8"))

    assert config.signals[3].parameters == {
        "pivot_width": 5,
        "minimum_pivots": 3,
        "margin_atr_fraction": "0.4",
    }
    assert config.signals[4].parameters["pivot_width"] == 5


def test_loader_file_boundary_reads_utf8_yaml(tmp_path: Path) -> None:
    from smc_ict.configuration import load_market_data

    path = tmp_path / "market-data.yaml"
    path.write_text(MARKET, encoding="utf-8")
    assert load_market_data(path).provider == "binance_usdm"
