from __future__ import annotations

import ast
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest


def test_provider_port_models_are_frozen_and_page_progress_is_explicit() -> None:
    from smc_ict.application.ports import KlinePage, KlineProvider, KlineRequest

    request = KlineRequest(
        provider_id="binance_usdm",
        market_type="LINEAR_PERPETUAL",
        instrument_id="BTC-USDT-PERP",
        provider_symbol="BTCUSDT",
        interval="1m",
        start_open_time_ms=0,
        end_open_time_ms=60_000,
    )
    with pytest.raises(FrozenInstanceError):
        request.provider_id = "changed"

    for invalid_symbol in ("", "x" * 65, 123, True):
        with pytest.raises((TypeError, ValueError)):
            KlineRequest(
                provider_id="binance_usdm",
                market_type="LINEAR_PERPETUAL",
                instrument_id="BTC-USDT-PERP",
                provider_symbol=invalid_symbol,
                interval="1m",
                start_open_time_ms=0,
                end_open_time_ms=60_000,
            )

    page = KlinePage(candles=(), next_start_open_time_ms=None, complete=True)
    assert page.complete is True

    class StructuralProvider:
        provider_id = "fixture"

        def validate_instrument(self, mapping: object) -> None:
            return None

        def server_time_ms(self) -> int:
            return 0

        def latest_closed_open_time_ms(self) -> int:
            return 0

        def fetch_page(self, incoming: KlineRequest) -> KlinePage:
            assert incoming is request
            return page

    assert isinstance(StructuralProvider(), KlineProvider)


def test_notifier_repository_and_plugin_ports_are_provider_neutral() -> None:
    from smc_ict.application.ports import (
        IndicatorPlugin,
        NotificationEvent,
        Notifier,
        Repository,
    )

    event = NotificationEvent(
        event_type="run_started",
        run_id="run_1",
        instrument_id=None,
        strategy_id="source-aligned-research",
        payload_schema_version=1,
        payload={},
    )
    assert event.event_type == "run_started"
    assert Notifier.__module__.startswith("smc_ict.application")
    assert Repository.__module__.startswith("smc_ict.application")
    assert IndicatorPlugin.__module__.startswith("smc_ict.application")


def test_foundation_registries_are_closed_and_reject_unknown_or_unavailable_ids() -> None:
    from smc_ict.composition import (
        UnavailableComponentError,
        UnknownComponentError,
        foundation_composition_root,
    )
    from smc_ict.configuration import DEFERRED_PLUGIN_IDS

    root = foundation_composition_root()
    assert root.providers.ids == ("binance_usdm", "okx_swap")
    assert root.notifiers.ids == ("discord_webhook", "generic_webhook")
    assert root.repositories.ids == ("sqlite",)
    assert root.plugins.ids == tuple(sorted(DEFERRED_PLUGIN_IDS))

    with pytest.raises(UnknownComponentError, match="unknown provider"):
        root.providers.resolve("binance")
    with pytest.raises(UnknownComponentError, match="unknown plugin"):
        root.plugins.resolve("smc.missing")
    with pytest.raises(UnavailableComponentError, match="does not install concrete plugins"):
        root.plugins.resolve("smc.swing_structure")
    with pytest.raises(UnavailableComponentError, match="implementation"):
        root.providers.resolve("binance_usdm")


def test_notification_registry_installs_generic_and_discord_adapters_and_fails_closed() -> None:
    from smc_ict.adapters.notifications import DiscordWebhookNotifier, GenericWebhookNotifier
    from smc_ict.composition import UnknownComponentError, notification_composition_root

    root = notification_composition_root()

    assert root.notifiers.ids == ("discord_webhook", "generic_webhook")
    assert root.notifiers.resolve("generic_webhook") is GenericWebhookNotifier
    assert root.notifiers.resolve("discord_webhook") is DiscordWebhookNotifier
    with pytest.raises(UnknownComponentError, match="unknown notifier"):
        root.notifiers.resolve("slack_webhook")


def test_architecture_has_one_way_dependencies_and_no_generic_utility_modules() -> None:
    package = Path(__file__).parents[1] / "src" / "smc_ict"
    forbidden_names = {"utils.py", "helpers.py"}
    assert not any(path.name in forbidden_names for path in package.rglob("*.py"))

    for boundary in (package / "domain", package / "application"):
        for path in boundary.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            imported_modules = {
                node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
            }
            imported_modules.update(
                alias.name
                for node in ast.walk(tree)
                if isinstance(node, ast.Import)
                for alias in node.names
            )
            assert not any(module.startswith("smc_ict.adapters") for module in imported_modules)
            assert not any(module.startswith("smc_ict.composition") for module in imported_modules)
            assert "requests" not in imported_modules
            assert "sqlite3" not in imported_modules
