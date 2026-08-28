# Strategy composition examples

These examples describe composition, not formulas. They are not a profitability claim and do not authorize live trading.

## Context, setup, and execution

Give each timeframe a logical role. A `context` role can describe the broad market state. A `setup` role can identify a location worth observing. An `execution` role can wait for a more precise event. Configuration owns the timeframe bound to each role, so changing a timeframe does not change graph code.

Every role receives completed bars only. A configured plugin can read its role's bars and the observations from its declared dependencies. It cannot read another plugin's hidden state. The graph stops invalid configuration before market or database access.

A human author can arrange a graph like this:

1. A context observation has no dependency.
2. A setup observation depends on context.
3. An execution observation depends on setup.
4. A project-owned risk plugin depends on the observations that provide candidate levels.

The YAML file owns plugin IDs, dependencies, order, and parameters. The engine core owns none of those choices.

## Independent confirmation roles

A strategy can also use two logical roles that confirm different facts without implying a fixed timeframe hierarchy. Both observations can feed a later decision gate. Deleting one configured instance removes only that node and any dependency that names it; changing one instance's parameters changes its parameter and graph hashes without changing unrelated plugin code.

## Risk boundary

A risk plugin is separate from source-aligned indicators. It may return configured entry, stop, and target levels only when its declared evidence is available. The ordered decision policy copies those canonical decimal strings; it does not invent missing levels, position size, fees, expected return, or fallback execution.

A failed required gate yields `NO_TRADE`. Missing required evidence yields `UNAVAILABLE`. A `READY` research decision still does not place an order. Risk limits and thresholds belong to validated strategy configuration and plugin contracts, never to generic graph or decision code.
