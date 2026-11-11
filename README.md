# satoshi-watcher

A free Solana price watcher for Satoshi, an AI assistant that paper-trades meme coins. GitHub Actions runs it every 5 minutes. It reads `watchlist.json`, checks the public DexScreener API, and sends **one** webhook POST when a threshold is crossed. Quiet runs do not call the webhook.

Satoshi keeps the watchlist current by committing `watchlist.json` on `main`. This repo does not commit price checks to `main`. Dedupe memory lives on a separate `state` branch.

## Secrets

The workflow reads two repository secrets. Add them under **Settings > Secrets and variables > Actions > New repository secret**.

| Secret | Value |
| --- | --- |
| `SATOSHI_WEBHOOK_URL` | HTTPS URL that receives the JSON POST. |
| `SATOSHI_WEBHOOK_AUTH` | The full `Authorization` header value, for example `Bearer <token>`. |

If either secret is missing, the run logs the alerts and exits successfully without posting. If there are no alerts, it does not POST at all.

The POST body looks like this:

```json
{
  "source": "satoshi-watcher",
  "checked_at": "2026-11-11T12:00:00Z",
  "alerts": [
    {
      "type": "near_stop",
      "symbol": "GATO",
      "mint": "HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump",
      "price": 0.00705,
      "detail": "P1 price 0.00705 within 2% of stop 0.006952"
    }
  ]
}
```

## What it alerts on

Prices come from `https://api.dexscreener.com/tokens/v1/solana/<mints>` (up to 30 mints per request). When a mint has several pairs, the watcher uses the pair with the highest `liquidity.usd`.

Boosted tokens come from `https://api.dexscreener.com/token-boosts/top/v1`. Only Solana boosts are kept, and only after their deepest pair passes the scan filters.

| `type` | When it fires |
| --- | --- |
| `stop` | An open trade's price is at or below its stop. |
| `near_stop` | Price is above the stop but within 2% of it. |
| `take_profit` | Price is at or above the take-profit. |
| `near_take_profit` | Price is below the take-profit but within 2% of it. |
| `time_stop` | The trade has been open for more than 24 hours. |
| `alert_above` | A watched mint's price crosses up through `alert_above`. |
| `alert_below` | A watched mint's price crosses down through `alert_below`. |
| `trending` | A boosted Solana mint has liquidity ≥ `scan.min_liquidity_usd` and a 1h change ≥ `scan.min_h1_change_pct`. |
| `dip` | A watched mint's 6h change is a drop of at least `scan.min_dip_pct`. |

`alert_above` and `alert_below` need the previous price, stored on the `state` branch. The first time a level is seen, the watcher records the price and stays quiet. `null` disables that level.

The same coin and condition is not sent again for 60 minutes. A stop warning and the later stop cross are different conditions, so both can wake Satoshi. A failed POST is not recorded, so the next run tries again.

## watchlist.json

Satoshi edits this file on `main`. The seeded file tracks the open GATO paper trade. `watch` starts empty.

```json
{
  "open_trades": [
    {
      "id": "P1",
      "symbol": "GATO",
      "chain": "solana",
      "mint": "HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump",
      "entry": 0.007724,
      "stop": 0.006952,
      "take_profit": 0.010814,
      "opened_at": "2026-11-11T06:13:26Z"
    }
  ],
  "watch": [
    {
      "symbol": "POP",
      "mint": "<solana mint>",
      "alert_above": null,
      "alert_below": null
    }
  ],
  "scan": {
    "min_liquidity_usd": 250000,
    "min_h1_change_pct": 15,
    "min_dip_pct": 20
  }
}
```

- `open_trades`: paper positions. `chain` must be `solana`. `stop` and `take_profit` are USD prices. `opened_at` is ISO-8601 UTC.
- `watch`: mints to level-alert and dip-scan. Leave `alert_above` or `alert_below` as `null` to skip that side. A dip uses the DexScreener 6h change and does not need a level.
- `scan`: trending and dip thresholds. Missing keys fall back to the numbers above.

## State branch

Alert timestamps and the last price of level-watched mints are stored in `state.json` on the `state` branch. The workflow creates that branch and adds a commit only when the file changes. It never commits state to `main`. Overlapping runs are blocked by the `satoshi-watcher` concurrency group.

## Run it

The schedule is `3-58/5 * * * *` (every 5 minutes, starting at minute 3) on the default branch. GitHub may start a scheduled run a few minutes late. The job uses Python from the runner, with no extra packages, and is capped at 2 minutes.

**Manually:** open the **Actions** tab, choose **watch**, and use **Run workflow**.

**Locally**, with a mocked or real webhook:

```bash
python3 -m unittest discover -s tests
SATOSHI_WEBHOOK_URL="https://example.test/hook" \
SATOSHI_WEBHOOK_AUTH="Bearer $TOKEN" \
python3 watcher.py --watchlist watchlist.json --state state.json
```

`state.json` is gitignored. Omit the secrets to print alerts without posting.
