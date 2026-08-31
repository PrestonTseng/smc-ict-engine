from __future__ import annotations

from pathlib import Path

from smc_ict.configuration import (
    load_market_data,
    load_notifications,
    load_schedule,
    load_strategy,
)

ROOT = Path(__file__).parents[1]


def test_active_market_config_selects_okx_swap_and_keeps_binance_as_an_alternate() -> None:
    from smc_ict.adapters.market_data.binance_usdm import BinanceUsdmProvider
    from smc_ict.adapters.market_data.okx_swap import OkxSwapProvider
    from smc_ict.composition import build_market_provider, market_data_composition_root

    root = market_data_composition_root()
    active = load_market_data(ROOT / "config/market-data.yaml")
    alternate = load_market_data(ROOT / "config/market-data.binance-usdm.yaml")

    assert active.canonical_dict() == {
        "provider": "okx_swap",
        "market_type": "LINEAR_PERPETUAL",
        "instruments": {
            "BTC-USDT-PERP": "BTC-USDT-SWAP",
            "ETH-USDT-PERP": "ETH-USDT-SWAP",
        },
    }
    assert alternate.canonical_dict() == {
        "provider": "binance_usdm",
        "market_type": "LINEAR_PERPETUAL",
        "instruments": {"BTC-USDT-PERP": "BTCUSDT", "ETH-USDT-PERP": "ETHUSDT"},
    }
    assert isinstance(build_market_provider(active, root), OkxSwapProvider)
    assert isinstance(build_market_provider(alternate, root), BinanceUsdmProvider)


def test_checked_in_examples_cross_real_loader_boundaries() -> None:
    assert load_market_data(ROOT / "config/market-data.yaml").provider == "okx_swap"
    assert (
        load_market_data(ROOT / "config/market-data.binance-usdm.yaml").provider == "binance_usdm"
    )
    assert load_market_data(ROOT / "config/market-data.okx-swap.yaml").provider == "okx_swap"
    assert load_schedule(ROOT / "config/schedule.yaml").timezone == "UTC"

    notifications = load_notifications(
        ROOT / "config/notifications.yaml",
        environ={"DISCORD_1_WEBHOOK_URL": "https://endpoint.invalid/one"},
        secret_files={"/run/secrets/discord_2_webhook_url": "https://endpoint.invalid/two"},
    )
    assert tuple(notifications.destinations) == ("discord_1", "discord_2")

    assert (
        load_strategy(ROOT / "strategies/source-aligned-research.yaml").name
        == "source-aligned-research"
    )


def test_examples_contain_references_but_no_literal_endpoint_or_secret() -> None:
    example_paths = [
        *sorted((ROOT / "config").glob("*.yaml")),
        *sorted((ROOT / "strategies").glob("*.yaml")),
    ]
    assert example_paths
    content = "\n".join(path.read_text(encoding="utf-8") for path in example_paths)
    assert "https://" not in content
    assert "http://" not in content
    assert "endpoint.invalid" not in content
    assert "DISCORD_1_WEBHOOK_URL" in content
    assert "/run/secrets/discord_2_webhook_url" in content
