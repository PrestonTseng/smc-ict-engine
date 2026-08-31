# Strategy authoring

Start with `docs/strategy-examples.md`. Keep the strategy file as the authority for the DAG.

For each role, set one completed-candle timeframe. For each signal, set one unique instance ID, one plugin ID, parameters, dependencies, and order.

Keep dependency order explicit. A signal can only use configured dependency evidence. The engine rejects missing dependencies, cycles, duplicate IDs, and timeframe mismatches.

Validate a strategy before deployment:

```sh
uv run smc-ict validate \
  --strategy strategies/source-aligned-research.yaml \
  --market-data config/market-data.yaml \
  --schedule config/schedule.yaml \
  --notifications config/notifications.yaml
```

The checked-in source-aligned strategy resolves exactly seven implemented registrations. Its role, timeframe, and dependency edges are fixed contracts; validation rejects a mismatch before any market-data request or database write.

The active ICT registrations use the pinned source defaults: configurable left pivot width 5 (allowed exact integers 3–10), fixed non-configurable right confirmation width 1, and liquidity margin ATR fraction 0.4 (allowed exact tenths 0.2–0.7, representing source margin input 2–7 divided by 10).
