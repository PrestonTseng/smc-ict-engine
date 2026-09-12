# Configuration

The engine loads strict YAML. Unknown keys, duplicate keys, wrong types, unknown registry IDs, and invalid ranges cause an error.

Use these authorities:

- `strategies/source-aligned-research.yaml` defines the strategy DAG.
- `config/market-data.yaml` actively selects OKX swap.
- `config/market-data.okx-swap.yaml` is an identical, explicitly named OKX example.
- `config/market-data.binance-usdm.yaml` is the inactive Binance USD-M alternate.
- `config/schedule.yaml` defines UTC jobs.
- `config/notifications.yaml` defines destination filters and secret references.

Runtime commands read only `${CONFIG_FOLDER}/market-data.yaml`. Copy the selected market-data
example to that path. Keep its instrument IDs aligned with the strategy.

Do not put a resolved webhook endpoint in YAML or `.env`. The one active `discord_debug`
destination reads `/run/secrets/discord_webhook_url`; Compose mounts that value from the ignored
host file `secrets/discord_webhook_url`. The destination subscribes to all five neutral engine
events and uses the native `discord_webhook` adapter.

Copy `.env.example` to a local ignored `.env` only when your Compose workflow loads that file. It
contains only the required absolute `DATA_FOLDER` configuration. Do not commit resolved values.

Backtest scenarios live under `backtests/<strategy-id>/`, not under global `config/`. Each scenario
must name one strategy leaf under `strategies/`. It also defines UTC minute boundaries, holding
time, costs, and the fixed V1 settings.

V1 accepts only these execution and output values:

- `entry.mode: touch_limit` waits for a one-minute candle to touch the requested entry.
- `entry.expiry_execution_bars` limits how many execution bars can contain an entry touch.
- `execution.intrabar_conflict: stop_first` selects the stop when one candle touches both exit levels.
- `execution.allow_same_minute_target: false` prevents a target exit during the entry minute. A stop remains active during that minute.
- `output.existing_result: fail` prevents replacement. The publisher reuses a byte-identical result and rejects all other existing content.

The fee and slippage values must be quoted canonical decimal strings. The simulator applies adverse
slippage at entry and exit. It applies the taker fee at both points.

A scenario cannot select a provider, market-data file, instrument subset, database, lock, report root, capital, leverage, balance, risk percentage, or quantity. `CONFIG_FOLDER/market-data.yaml` remains the single provider and mapping authority. `DATA_FOLDER` remains the single storage authority; reports derive as `DATA_FOLDER/backtests/<backtest-id>/`.
