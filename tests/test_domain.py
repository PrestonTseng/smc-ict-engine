from __future__ import annotations

from datetime import UTC, datetime

import pytest


def test_decimal_text_normalizes_fixed_point_without_float_rounding() -> None:
    from smc_ict.domain import DecimalText

    assert str(DecimalText("001.0")) == "1"
    assert str(DecimalText("0.1000")) == "0.1"
    assert str(DecimalText("12345678901234567890.000000000000000001")) == (
        "12345678901234567890.000000000000000001"
    )
    for invalid in ("-1", "+1", "1e3", ".1", "nan", "inf", 1, True):
        with pytest.raises((TypeError, ValueError)):
            DecimalText(invalid)


def test_identifiers_and_utc_timestamp_are_canonical() -> None:
    from smc_ict.domain import EventType, InstrumentId, Timeframe, UtcTimestamp

    assert str(InstrumentId("BTC-USDT-PERP")) == "BTC-USDT-PERP"
    assert str(Timeframe("15m")) == "15m"
    assert Timeframe("1m").duration_minutes == 1
    assert Timeframe("5m").duration_minutes == 5
    assert Timeframe("15m").duration_minutes == 15
    assert Timeframe("1h").duration_minutes == 60
    assert Timeframe("4h").duration_minutes == 240
    assert str(EventType("decision_found")) == "decision_found"
    stamp = UtcTimestamp.from_milliseconds(1_800_000)
    assert stamp.milliseconds == 1_800_000
    assert stamp.datetime == datetime(1970, 1, 1, 0, 30, tzinfo=UTC)
    assert UtcTimestamp.parse("1970-01-01T00:30:00Z") == stamp

    for invalid in ("btc-usdt-perp", "BTCUSDT", "BTC/USDT"):
        with pytest.raises(ValueError):
            InstrumentId(invalid)
    for invalid in ("15M", "015m", "900s", "quarter-hour", " 15m"):
        with pytest.raises(ValueError):
            Timeframe(invalid)
    for invalid_type in (15, True, None):
        with pytest.raises(TypeError):
            Timeframe(invalid_type)
    with pytest.raises(ValueError):
        EventType("unknown")
    with pytest.raises(ValueError):
        UtcTimestamp(datetime(2026, 1, 1))


def test_closed_candle_enforces_alignment_closure_ohlc_and_immutable_extensions() -> None:
    from smc_ict.domain import ClosedCandle

    candle = ClosedCandle(
        provider_id="binance_usdm",
        market_type="LINEAR_PERPETUAL",
        instrument_id="BTC-USDT-PERP",
        provider_symbol="BTCUSDT",
        interval="1m",
        open_time_ms=0,
        close_time_ms=59_999,
        open="100.0",
        high="110",
        low="90",
        close="105.00",
        base_volume="12.50",
        quote_volume=None,
        source_fields={
            "trade_count": 4,
            "taker_buy_base_volume": "2.5",
            "taker_buy_quote_volume": "250",
        },
    )
    assert candle.open == "100"
    assert candle.close == "105"
    with pytest.raises(TypeError):
        candle.source_fields["trade_count"] = 5

    base = {
        "provider_id": "binance_usdm",
        "market_type": "LINEAR_PERPETUAL",
        "instrument_id": "BTC-USDT-PERP",
        "provider_symbol": "BTCUSDT",
        "interval": "1m",
        "open_time_ms": 0,
        "close_time_ms": 59_999,
        "open": "100",
        "high": "110",
        "low": "90",
        "close": "105",
        "base_volume": "12.5",
        "quote_volume": None,
        "source_fields": {
            "trade_count": 4,
            "taker_buy_base_volume": "2.5",
            "taker_buy_quote_volume": "250",
        },
    }
    for key, bad in (
        ("open_time_ms", True),
        ("open_time_ms", 1),
        ("close_time_ms", 60_000),
        ("high", "99"),
        ("base_volume", "-1"),
    ):
        values = {**base, key: bad}
        with pytest.raises((TypeError, ValueError)):
            ClosedCandle(**values)

    for price_field in ("open", "high", "low", "close"):
        values = {
            **base,
            "open": "1",
            "high": "1",
            "low": "1",
            "close": "1",
            price_field: "0",
        }
        if price_field in {"open", "close"}:
            values["low"] = "0"
        with pytest.raises(ValueError, match="prices must be positive"):
            ClosedCandle(**values)


def test_candle_hash_is_domain_separated_and_order_deterministic() -> None:
    from smc_ict.domain import ClosedCandle, hash_candles

    def candle(instrument: str) -> ClosedCandle:
        return ClosedCandle(
            provider_id="okx_swap",
            market_type="LINEAR_PERPETUAL",
            instrument_id=instrument,
            provider_symbol=instrument.replace("-PERP", "-SWAP"),
            interval="1m",
            open_time_ms=0,
            close_time_ms=59_999,
            open="1",
            high="1",
            low="1",
            close="1",
            base_volume="0",
            quote_volume="0",
            source_fields={"contract_volume": "0"},
        )

    btc, eth = candle("BTC-USDT-PERP"), candle("ETH-USDT-PERP")
    assert hash_candles((eth, btc)) == hash_candles((btc, eth))
    assert len(hash_candles((btc, eth))) == 64
