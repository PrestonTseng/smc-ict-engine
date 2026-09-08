# Strategy composition examples

These examples describe composition, not formulas. They are not a profitability claim and do not authorize live trading.

## Context, setup, and execution

Each timeframe has a logical role. A `regime` role describes the broad market state. A `context`
role identifies a location to observe. An `execution` role waits for a more precise event.

Every role receives completed bars only. A configured plugin can read its role's bars and the observations from its declared dependencies. It cannot read another plugin's hidden state. The graph stops invalid configuration before market or database access.

The checked-in strategy assigns swing structure to `4h`. It assigns equal-high/low and order-block
context to `1h`. It assigns the ordered liquidity, market-structure, fair-value-gap, and risk-level
chain to `15m`. This composition is not an equivalence claim about another execution flow.

The checked-in strategy uses this fixed graph:

1. `smc.swing_structure` has no dependency.
2. `smc.equal_high_low` depends on `smc.swing_structure`.
3. `smc.order_block` depends on `smc.swing_structure`.
4. `ict.clustered_liquidity` depends on `smc.equal_high_low`.
5. `ict.market_structure` depends on `ict.clustered_liquidity`.
6. `ict.fair_value_gap` depends on `ict.market_structure`.
7. `project.risk_levels` depends on `ict.clustered_liquidity` and `ict.fair_value_gap`.

The YAML file records plugin IDs, dependencies, order, and parameters. The strict loader requires
the registered V1 role, timeframe, and dependency contract for each plugin. V1 does not support
arbitrary graph rearrangement.

## Configuration changes

Configuration owns supported plugin parameters, instruments, strategy metadata, and history. Changes
to these values update the related configuration hashes. An author cannot delete a required
dependency, change a fixed role or timeframe, or add an unknown plugin ID.

## Risk boundary

A risk plugin is separate from source-aligned indicators. It may return configured entry, stop, and target levels only when its declared evidence is available. The ordered decision policy copies those canonical decimal strings; it does not invent missing levels, position size, fees, expected return, or fallback execution.

A failed required gate yields `NO_TRADE`. Missing required evidence yields `UNAVAILABLE`. A `READY` research decision still does not place an order. Risk limits and thresholds belong to validated strategy configuration and plugin contracts, never to generic graph or decision code.
