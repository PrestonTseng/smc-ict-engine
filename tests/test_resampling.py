from __future__ import annotations

import pytest


def one_minute_candles(
    count: int,
    *,
    start_minute: int = 0,
    provider_id: str = "binance_usdm",
    missing_quote_at: int | None = None,
):
    from smc_ict.domain import ClosedCandle

    provider_symbol = "BTCUSDT" if provider_id == "binance_usdm" else "BTC-USDT-SWAP"
    result = []
    for offset in range(count):
        minute = start_minute + offset
        source_fields: dict[str, int | str] = (
            {
                "trade_count": offset + 1,
                "taker_buy_base_volume": "0.25",
                "taker_buy_quote_volume": "25",
            }
            if provider_id == "binance_usdm"
            else {"contract_volume": "2"}
        )
        result.append(
            ClosedCandle(
                provider_id=provider_id,
                market_type="LINEAR_PERPETUAL",
                instrument_id="BTC-USDT-PERP",
                provider_symbol=provider_symbol,
                interval="1m",
                open_time_ms=minute * 60_000,
                close_time_ms=minute * 60_000 + 59_999,
                open=str(100 + offset),
                high=str(102 + offset),
                low=str(99 + offset),
                close=str(101 + offset),
                base_volume="1.20",
                quote_volume=None if offset == missing_quote_at else "120.00",
                source_fields=source_fields,
            )
        )
    return tuple(result)


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


def test_15m_resampling_requires_exact_aligned_complete_closed_buckets() -> None:
    from smc_ict.application.resampling import resample_complete

    complete = one_minute_candles(30)

    assert [bar.open_time_ms for bar in resample_complete(complete, "15m")] == [0, 900_000]
    assert resample_complete(complete[:14], "15m") == ()
    assert resample_complete(one_minute_candles(14, start_minute=1), "15m") == ()
    assert len(resample_complete(one_minute_candles(15, start_minute=15), "15m")) == 1

    with pytest.raises(ValueError, match="contiguous"):
        resample_complete(complete[:7] + complete[8:15], "15m")


def test_15m_resampling_excludes_developing_and_future_buckets_at_evaluation_time() -> None:
    from smc_ict.application.resampling import resample_complete

    candles = one_minute_candles(30)

    assert resample_complete(candles, "15m", evaluation_time_ms=899_998) == ()
    bars = resample_complete(candles, "15m", evaluation_time_ms=899_999)
    assert tuple(bar.open_time_ms for bar in bars) == (0,)
    assert tuple(
        bar.open_time_ms for bar in resample_complete(candles, "15m", evaluation_time_ms=1_799_999)
    ) == (0, 900_000)

    for unsafe in (True, "899999"):
        with pytest.raises(TypeError, match="evaluation time"):
            resample_complete(candles, "15m", evaluation_time_ms=unsafe)


def test_15m_resampling_preserves_ohlcv_quote_nullability_and_ignores_provider_extensions() -> None:
    from smc_ict.application.resampling import resample_complete

    expected = resample_complete(one_minute_candles(15), "15m")[0]
    okx = resample_complete(one_minute_candles(15, provider_id="okx_swap"), "15m")[0]
    without_quote = resample_complete(one_minute_candles(15, missing_quote_at=7), "15m")[0]

    assert expected == okx
    assert expected.canonical_dict() == {
        "instrument_id": "BTC-USDT-PERP",
        "interval": "15m",
        "open_time_ms": 0,
        "close_time_ms": 899_999,
        "open": "100",
        "high": "116",
        "low": "99",
        "close": "115",
        "base_volume": "18",
        "quote_volume": "1800",
    }
    assert without_quote.quote_volume is None


def test_derived_candle_hash_is_deterministic_and_domain_separated() -> None:
    from dataclasses import replace

    from smc_ict.application.resampling import hash_derived_candles, resample_complete

    bars = resample_complete(one_minute_candles(30), "15m")

    assert hash_derived_candles(bars) == hash_derived_candles(tuple(reversed(bars)))
    assert len(hash_derived_candles(bars)) == 64
    assert hash_derived_candles(bars) != hash_derived_candles(
        (replace(bars[0], close="114"), bars[1])
    )

    same_key_changed_value = replace(bars[0], close="114")
    assert hash_derived_candles((bars[0], same_key_changed_value)) == hash_derived_candles(
        (same_key_changed_value, bars[0])
    )
