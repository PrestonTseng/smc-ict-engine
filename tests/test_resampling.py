from __future__ import annotations

import pytest


def test_complete_only_utc_resampling_is_exact_and_provider_neutral() -> None:
    from smc_ict.application.resampling import resample_roles
    from smc_ict.domain import ClosedCandle

    def candles(provider_id: str) -> tuple[ClosedCandle, ...]:
        provider_symbol = "BTCUSDT" if provider_id == "binance_usdm" else "BTC-USDT-SWAP"
        result = []
        for minute in range(6):
            source_fields: dict[str, int | str]
            if provider_id == "binance_usdm":
                source_fields = {
                    "trade_count": minute + 1,
                    "taker_buy_base_volume": "0.25",
                    "taker_buy_quote_volume": "25",
                }
            else:
                source_fields = {"contract_volume": "2"}
            result.append(
                ClosedCandle(
                    provider_id=provider_id,
                    market_type="LINEAR_PERPETUAL",
                    instrument_id="BTC-USDT-PERP",
                    provider_symbol=provider_symbol,
                    interval="1m",
                    open_time_ms=minute * 60_000,
                    close_time_ms=minute * 60_000 + 59_999,
                    open=str(100 + minute),
                    high=str(102 + minute),
                    low=str(99 + minute),
                    close=str(101 + minute),
                    base_volume="1.20",
                    quote_volume="120.00",
                    source_fields=source_fields,
                )
            )
        return tuple(result)

    roles = {"regime": "1h", "context": "5m", "execution": "5m"}
    binance = resample_roles(candles("binance_usdm"), roles)
    okx = resample_roles(candles("okx_swap"), roles)

    assert binance == okx
    assert binance["regime"] == ()
    assert binance["context"] == binance["execution"]
    bar = binance["context"][0]
    assert bar.canonical_dict() == {
        "instrument_id": "BTC-USDT-PERP",
        "interval": "5m",
        "open_time_ms": 0,
        "close_time_ms": 299_999,
        "open": "100",
        "high": "106",
        "low": "99",
        "close": "105",
        "base_volume": "6",
        "quote_volume": "600",
    }

    with pytest.raises(ValueError, match="contiguous"):
        resample_roles(candles("binance_usdm")[::2], {"execution": "5m"})
