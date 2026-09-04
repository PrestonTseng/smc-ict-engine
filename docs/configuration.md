# Configuration

The engine loads strict YAML. Unknown keys, duplicate keys, wrong types, unknown registry IDs, and invalid ranges cause an error.

Use these authorities:

- `strategies/source-aligned-research.yaml` defines the strategy DAG.
- `config/market-data.yaml` actively selects OKX swap.
- `config/market-data.binance-usdm.yaml` is the inactive Binance USD-M alternate.
- `config/schedule.yaml` defines UTC jobs.
- `config/notifications.yaml` defines destination filters and secret references.

Keep instrument IDs aligned between the strategy and market-data files. Use one market-data file for each run.

Do not put a resolved webhook endpoint in YAML or `.env`. The one active `discord_debug`
destination reads `/run/secrets/discord_webhook_url`; Compose mounts that value from the ignored
host file `secrets/discord_webhook_url`. The destination subscribes to all five neutral engine
events and uses the native `discord_webhook` adapter.

Copy `.env.example` to a local ignored `.env` only when your Compose workflow loads that file. It
contains the immutable image revision and required absolute `DATA_FOLDER` configuration. Do not
commit resolved values.

Backtest scenarios live under `backtests/<strategy-id>/` rather than global `config/`. Each scenario must name one existing strategy leaf under `strategies/` and define canonical UTC minute boundaries, touch-limit expiry, stop-first execution, holding time, quoted canonical fee/slippage values, and `output.existing_result: fail`.

A scenario cannot select a provider, market-data file, instrument subset, database, lock, report root, capital, leverage, balance, risk percentage, or quantity. `CONFIG_FOLDER/market-data.yaml` remains the single provider and mapping authority. `DATA_FOLDER` remains the single storage authority; reports derive as `DATA_FOLDER/backtests/<backtest-id>/`.
