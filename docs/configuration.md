# Configuration

The engine loads strict YAML. Unknown keys, duplicate keys, wrong types, unknown registry IDs, and invalid ranges cause an error.

Use these authorities:

- `strategies/source-aligned-research.yaml` defines the strategy DAG.
- `config/market-data.yaml` actively selects OKX swap.
- `config/market-data.binance-usdm.yaml` is the inactive Binance USD-M alternate.
- `config/schedule.yaml` defines UTC jobs.
- `config/notifications.yaml` defines destination filters and secret references.

Keep instrument IDs aligned between the strategy and market-data files. Use one market-data file for each run.

Do not put resolved webhook URLs in YAML. Set `DISCORD_1_WEBHOOK_URL` in the environment. Put the second URL in `secrets/discord_2_webhook_url`.

Copy `.env.example` to a local ignored `.env` only when your Compose workflow loads that file. Do not commit the resolved values.
