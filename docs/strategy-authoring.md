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

The checked-in source-aligned strategy returns `DEFERRED_PLUGIN`. Do not remove this gate. Promotion requires pinned expected-output vectors and an independent implementation.
