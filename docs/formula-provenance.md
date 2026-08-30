# Formula provenance

`provenance/sources.yaml` records each external source, revision, access date, license state, source hash, and Python implementation state. `LICENSES/README.md` records the unresolved license boundary.

The repository contains no Pine source and does not execute TradingView. The Python plugins are bounded closed-candle translations of the exact pinned source bytes, not a claim that chart rendering or a remote Pine runtime is equivalent.

The active source locators are:

- SMC swing structure: lines 337–361, 409–457, and 551–612 (confirmed leg pivots, HH/HL/LH/LL, un-crossed close breaks, BOS/CHoCH).
- SMC equal highs/lows: lines 384–402 and 409–457 (confirmation latency and strict threshold × ATR(200)).
- SMC order blocks: lines 310–323 and 478–525, integrated at 551–612 and executed at 782–798 (volatility parsing, source-extreme selection, mitigation, and retention).
- ICT clustered liquidity: settings lines 23–25 and 61–64, formulas lines 344–463, and traversal lines 821–856. `pivot_width` is the configurable left width, constrained to the source's exact integer range 3–10 with default 5; the right confirmation width is fixed at one and is not configurable. `margin_atr_fraction` represents source input margin ÷ 10, so only exact tenths 0.2–0.7 are valid and the source default is 0.4. The traversal state uses strict close boundaries and freezes the first full-traversal candle high for buyside or low for sellside.
- ICT market structure: settings lines 23–25 and formulas lines 344–364 and 465–517 (source-valid left width 3–10/default 5, fixed right width one, previous zigzag level selection, MSS direction changes, and unique same-direction BOS).
- ICT ordinary FVG: lines 547–566, 597–644, and 699–724 (large-body displacement, ordinary three-candle gap, consecutive extension, and strict full traversal). IFVG and BPR are not implemented.
- Project risk levels: `strategies/source-aligned-research.yaml` version 1 (FVG midpoint entry, first full-traversal candle extreme ± 0.1 × execution ATR(14), frozen opposing liquidity target, strict liquidity-known-before-FVG ordering, retained compatible FVG→market-structure dependency evidence, and exact configured reward/risk).

Changes must preserve exact Decimal and UTC time behavior, completed-candle and no-lookahead evaluation, explicit pivot confirmation latency, warm-up policy, fixed role/timeframe/dependency identity, and deterministic replay hashes.

Do not infer formulas from screenshots or prose, vendor Pine bodies, or treat the research result as an order or performance claim. Publication and commercial use remain subject to the recorded license boundary.
