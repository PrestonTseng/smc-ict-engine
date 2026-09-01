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
contains only the immutable image revision variable. Do not commit resolved values.
