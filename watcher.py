#!/usr/bin/env python3
"""Price watcher that wakes Satoshi only when a threshold is crossed.

Stdlib only. Reads watchlist.json, fetches Solana prices from the public
DexScreener API, and POSTs one JSON body when something new is worth a look.
Repeat alerts for the same coin and condition are suppressed for 60 minutes.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

TOKENS_URL = "https://api.dexscreener.com/tokens/v1/solana/{mints}"
BOOSTS_URL = "https://api.dexscreener.com/token-boosts/top/v1"
BATCH_SIZE = 30
COOLDOWN = timedelta(minutes=60)
TIME_STOP = timedelta(hours=24)
NEAR_FRACTION = 0.02
SOURCE = "satoshi-watcher"
USER_AGENT = "satoshi-watcher"
MINT_RE = re.compile(r"^[1-9A-HJ-NP-Za-km-z]{32,44}$")
DEFAULT_SCAN = {
    "min_liquidity_usd": 250_000.0,
    "min_h1_change_pct": 15.0,
    "min_dip_pct": 20.0,
}


class WatcherError(Exception):
    """A failure the workflow should surface, without a traceback."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def iso_z(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_time(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        moment = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"invalid timestamp {value!r}") from exc
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def as_float(value: object) -> float | None:
    if value is None or isinstance(value, bool) or value == "":
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number or number in (float("inf"), float("-inf")):
        return None
    return number


def format_number(value: float) -> str:
    return format(value, ".12g")


def valid_mint(mint: str) -> bool:
    return bool(MINT_RE.fullmatch(mint))


def unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item in seen:
            continue
        seen.add(item)
        ordered.append(item)
    return ordered


def is_solana(item: dict) -> bool:
    return str(item.get("chain") or "solana").lower() == "solana"


def expect_list(data: dict, key: str) -> list:
    value = data.get(key, [])
    if value is None:
        return []
    if not isinstance(value, list):
        raise WatcherError(f"watchlist {key} must be a list")
    return value


def load_watchlist(path: str | Path) -> dict:
    file_path = Path(path)
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise WatcherError(f"watchlist not found: {file_path}") from exc
    except json.JSONDecodeError as exc:
        raise WatcherError(f"watchlist is not valid JSON: {file_path}") from exc
    if not isinstance(data, dict):
        raise WatcherError("watchlist must be a JSON object")
    expect_list(data, "open_trades")
    expect_list(data, "watch")
    scan = data.get("scan", {})
    if scan is None:
        scan = {}
    if not isinstance(scan, dict):
        raise WatcherError("watchlist scan must be an object")
    return data


def empty_state() -> dict:
    return {"last_alerts": {}, "last_prices": {}}


def load_state(path: str | Path) -> dict:
    file_path = Path(path)
    if not file_path.exists():
        return empty_state()
    try:
        data = json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise WatcherError(f"state file is not valid JSON: {file_path}") from exc
    if not isinstance(data, dict):
        raise WatcherError("state file must be a JSON object")
    last_alerts = data.get("last_alerts", {})
    last_prices = data.get("last_prices", {})
    if not isinstance(last_alerts, dict) or not isinstance(last_prices, dict):
        raise WatcherError("state file has an unexpected shape")
    return {"last_alerts": last_alerts, "last_prices": last_prices}


def save_state(path: str | Path, state: dict) -> None:
    file_path = Path(path)
    payload = {
        "last_alerts": dict(sorted(state.get("last_alerts", {}).items())),
        "last_prices": dict(sorted(state.get("last_prices", {}).items())),
    }
    text = json.dumps(payload, indent=2) + "\n"
    temporary = file_path.with_suffix(file_path.suffix + ".tmp")
    temporary.write_text(text, encoding="utf-8")
    temporary.replace(file_path)


def scan_config(watchlist: dict) -> dict[str, float]:
    raw = watchlist.get("scan") or {}
    config: dict[str, float] = {}
    for key, default in DEFAULT_SCAN.items():
        value = as_float(raw.get(key)) if isinstance(raw, dict) else None
        config[key] = default if value is None else value
    return config


def watchlist_mints(watchlist: dict) -> list[str]:
    mints: list[str] = []
    for collection in ("open_trades", "watch"):
        for item in expect_list(watchlist, collection):
            if not isinstance(item, dict) or not is_solana(item):
                continue
            mint = str(item.get("mint") or "")
            if valid_mint(mint):
                mints.append(mint)
    return unique(mints)


def boost_mints(boosts: object) -> list[str]:
    if not isinstance(boosts, list):
        raise WatcherError("token boosts response was not a list")
    mints: list[str] = []
    for item in boosts:
        if not isinstance(item, dict) or item.get("chainId") != "solana":
            continue
        mint = str(item.get("tokenAddress") or "")
        if valid_mint(mint):
            mints.append(mint)
    return unique(mints)


def token_endpoint_urls(mints: list[str]) -> list[str]:
    urls: list[str] = []
    for start in range(0, len(mints), BATCH_SIZE):
        batch = ",".join(mints[start : start + BATCH_SIZE])
        urls.append(TOKENS_URL.format(mints=batch))
    return urls


def tracked_price_mints(watchlist: dict) -> list[str]:
    """Mints whose previous price is required to detect a level cross."""
    mints: list[str] = []
    for item in expect_list(watchlist, "watch"):
        if not isinstance(item, dict) or not is_solana(item):
            continue
        if item.get("alert_above") is None and item.get("alert_below") is None:
            continue
        mint = str(item.get("mint") or "")
        if valid_mint(mint):
            mints.append(mint)
    return unique(mints)


def quotes_by_mint(pairs: list[dict]) -> dict[str, dict]:
    """Keep the deepest-liquidity Solana pair for each base mint."""
    best: dict[str, dict] = {}
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        chain = pair.get("chainId")
        if chain not in (None, "solana"):
            continue
        base = pair.get("baseToken") or {}
        if not isinstance(base, dict):
            continue
        mint = str(base.get("address") or "")
        if not valid_mint(mint):
            continue
        liquidity_block = pair.get("liquidity")
        if not isinstance(liquidity_block, dict):
            liquidity_block = {}
        liquidity = as_float(liquidity_block.get("usd"))
        change = pair.get("priceChange") or {}
        if not isinstance(change, dict):
            change = {}
        quote = {
            "symbol": str(base.get("symbol") or ""),
            "price": as_float(pair.get("priceUsd")),
            "liquidity_usd": 0.0 if liquidity is None else liquidity,
            "h1_change_pct": as_float(change.get("h1")),
            "h6_change_pct": as_float(change.get("h6")),
        }
        price = quote["price"]
        if price is not None and price <= 0:
            quote["price"] = None
        current = best.get(mint)
        if current is None or quote["liquidity_usd"] > current["liquidity_usd"]:
            best[mint] = quote
        elif (
            quote["liquidity_usd"] == current["liquidity_usd"]
            and current["price"] is None
            and quote["price"] is not None
        ):
            best[mint] = quote
    return best


def display_symbol(configured: object, quote: dict | None, mint: str) -> str:
    pair_symbol = (quote or {}).get("symbol") or ""
    for value in (configured, pair_symbol, mint):
        if value:
            return str(value)
    return mint


def _alert(
    kind: str,
    symbol: str,
    mint: str,
    price: float | None,
    detail: str,
    trade_id: str = "",
) -> dict:
    return {
        "type": kind,
        "symbol": symbol,
        "mint": mint,
        "price": price,
        "detail": detail,
        "trade_id": trade_id,
    }


def evaluate_alerts(
    watchlist: dict,
    quotes: dict[str, dict],
    boosted: list[str],
    state: dict,
    now: datetime,
) -> list[dict]:
    scan = scan_config(watchlist)
    alerts: list[dict] = []
    last_prices = state.get("last_prices") or {}

    for trade in expect_list(watchlist, "open_trades"):
        if not isinstance(trade, dict) or not is_solana(trade):
            continue
        mint = str(trade.get("mint") or "")
        if not valid_mint(mint):
            continue
        trade_id = str(trade.get("id") or mint)
        quote = quotes.get(mint)
        price = None if quote is None else quote.get("price")
        symbol = display_symbol(trade.get("symbol"), quote, mint)
        stop = as_float(trade.get("stop"))
        take_profit = as_float(trade.get("take_profit"))
        if price is not None and stop is not None and stop > 0:
            if price <= stop:
                alerts.append(
                    _alert(
                        "stop",
                        symbol,
                        mint,
                        price,
                        f"{trade_id} price {format_number(price)} at or below stop {format_number(stop)}",
                        trade_id,
                    )
                )
            elif price - stop <= stop * NEAR_FRACTION:
                alerts.append(
                    _alert(
                        "near_stop",
                        symbol,
                        mint,
                        price,
                        f"{trade_id} price {format_number(price)} within 2% of stop {format_number(stop)}",
                        trade_id,
                    )
                )
        if price is not None and take_profit is not None and take_profit > 0:
            if price >= take_profit:
                alerts.append(
                    _alert(
                        "take_profit",
                        symbol,
                        mint,
                        price,
                        f"{trade_id} price {format_number(price)} at or above take-profit {format_number(take_profit)}",
                        trade_id,
                    )
                )
            elif take_profit - price <= take_profit * NEAR_FRACTION:
                alerts.append(
                    _alert(
                        "near_take_profit",
                        symbol,
                        mint,
                        price,
                        f"{trade_id} price {format_number(price)} within 2% of take-profit {format_number(take_profit)}",
                        trade_id,
                    )
                )
        opened_raw = trade.get("opened_at")
        if opened_raw:
            try:
                opened = parse_time(str(opened_raw))
            except ValueError:
                opened = None
            if opened is not None and now - opened > TIME_STOP:
                alerts.append(
                    _alert(
                        "time_stop",
                        symbol,
                        mint,
                        price,
                        f"{trade_id} open since {iso_z(opened)}, older than 24h",
                        trade_id,
                    )
                )

    for item in expect_list(watchlist, "watch"):
        if not isinstance(item, dict) or not is_solana(item):
            continue
        mint = str(item.get("mint") or "")
        if not valid_mint(mint):
            continue
        quote = quotes.get(mint)
        price = None if quote is None else quote.get("price")
        symbol = display_symbol(item.get("symbol"), quote, mint)
        previous = as_float(last_prices.get(mint))
        above = as_float(item.get("alert_above")) if "alert_above" in item else None
        below = as_float(item.get("alert_below")) if "alert_below" in item else None
        if (
            price is not None
            and previous is not None
            and above is not None
            and previous < above <= price
        ):
            alerts.append(
                _alert(
                    "alert_above",
                    symbol,
                    mint,
                    price,
                    f"crossed above {format_number(above)} ({format_number(previous)} -> {format_number(price)})",
                )
            )
        if (
            price is not None
            and previous is not None
            and below is not None
            and previous > below >= price
        ):
            alerts.append(
                _alert(
                    "alert_below",
                    symbol,
                    mint,
                    price,
                    f"crossed below {format_number(below)} ({format_number(previous)} -> {format_number(price)})",
                )
            )
        h6 = None if quote is None else quote.get("h6_change_pct")
        if h6 is not None and h6 <= -scan["min_dip_pct"]:
            alerts.append(
                _alert(
                    "dip",
                    symbol,
                    mint,
                    price,
                    f"6h change {format_number(h6)}% (dip threshold {format_number(scan['min_dip_pct'])}%)",
                )
            )

    for mint in boosted:
        quote = quotes.get(mint)
        if quote is None or quote.get("price") is None:
            continue
        liquidity = quote["liquidity_usd"]
        h1 = quote.get("h1_change_pct")
        if liquidity < scan["min_liquidity_usd"] or h1 is None:
            continue
        if h1 < scan["min_h1_change_pct"]:
            continue
        alerts.append(
            _alert(
                "trending",
                display_symbol(None, quote, mint),
                mint,
                quote["price"],
                (
                    f"boosted on Solana, liquidity {format_number(liquidity)} USD, "
                    f"h1 {format_number(h1)}%"
                ),
            )
        )
    return alerts


def alert_key(alert: dict) -> str:
    return f"{alert['mint']}|{alert['type']}|{alert.get('trade_id') or ''}"


def apply_dedupe(alerts: list[dict], state: dict, now: datetime) -> list[dict]:
    last = state.get("last_alerts") or {}
    kept: list[dict] = []
    seen: set[str] = set()
    for alert in alerts:
        key = alert_key(alert)
        if key in seen:
            continue
        stamp = last.get(key)
        if stamp:
            try:
                seen_at = parse_time(str(stamp))
            except ValueError:
                seen_at = None
            if seen_at is not None and now - seen_at < COOLDOWN:
                continue
        seen.add(key)
        kept.append(alert)
    return kept


def next_state(
    watchlist: dict,
    quotes: dict[str, dict],
    delivered: list[dict],
    state: dict,
    now: datetime,
) -> dict:
    kept: dict[str, str] = {}
    for key, stamp in (state.get("last_alerts") or {}).items():
        try:
            seen_at = parse_time(str(stamp))
        except ValueError:
            continue
        if now - seen_at < COOLDOWN:
            kept[str(key)] = iso_z(seen_at)
    for alert in delivered:
        kept[alert_key(alert)] = iso_z(now)

    prices: dict[str, float] = {}
    previous = state.get("last_prices") or {}
    for mint in tracked_price_mints(watchlist):
        quote = quotes.get(mint)
        price = None if quote is None else quote.get("price")
        if price is None:
            price = as_float(previous.get(mint))
        if price is not None:
            prices[mint] = price
    return {"last_alerts": kept, "last_prices": prices}


def build_payload(alerts: list[dict], now: datetime) -> dict:
    return {
        "source": SOURCE,
        "checked_at": iso_z(now),
        "alerts": [
            {
                "type": alert["type"],
                "symbol": alert["symbol"],
                "mint": alert["mint"],
                "price": alert["price"],
                "detail": alert["detail"],
            }
            for alert in alerts
        ],
    }


def normalize_secret(value: str) -> str:
    """Drop surrounding whitespace. Embedded newlines make the secret unusable."""
    cleaned = value.strip()
    if any(char in cleaned for char in "\r\n"):
        return ""
    return cleaned


def secrets_ready(url: str, auth: str) -> bool:
    url = normalize_secret(url)
    auth = normalize_secret(auth)
    if not url or not auth:
        return False
    parsed = urllib.parse.urlparse(url)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc)


def _open(request: urllib.request.Request, timeout: float):
    """Open a request. POSTs do not follow redirects, so the auth header stays put."""
    if request.get_method() == "POST":
        opener = urllib.request.build_opener(_NoRedirect)
        return opener.open(request, timeout=timeout)
    return urllib.request.urlopen(request, timeout=timeout)


def http_get_json(url: str, timeout: float = 15) -> object:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
    )
    kind = "boosts" if "token-boosts" in url else "tokens"
    try:
        with _open(request, timeout) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raise WatcherError(f"DexScreener {kind} request failed (HTTP {exc.code})") from None
    except urllib.error.URLError as exc:
        raise WatcherError(f"DexScreener {kind} request failed: {exc.reason}") from None
    try:
        return json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise WatcherError(f"DexScreener {kind} response was not valid JSON") from exc


def http_post(url: str, auth: str, payload: dict, timeout: float = 15) -> None:
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": auth,
            "User-Agent": USER_AGENT,
        },
    )
    try:
        with _open(request, timeout) as response:
            status = getattr(response, "status", None) or response.getcode()
            response.read()
    except urllib.error.HTTPError as exc:
        raise WatcherError(f"webhook returned HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        raise WatcherError(f"webhook request failed: {exc.reason}") from None
    if status >= 300:
        raise WatcherError(f"webhook returned HTTP {status}")


def run(
    watchlist: dict,
    state: dict,
    state_path: str | Path,
    now: datetime,
    webhook_url: str,
    webhook_auth: str,
    get_json=None,
    post=None,
    timeout: float = 15,
) -> int:
    def fetch(url: str):
        if get_json is None:
            return http_get_json(url, timeout)
        return get_json(url)

    def deliver(url: str, auth: str, body: dict) -> None:
        if post is None:
            http_post(url, auth, body, timeout)
            return
        post(url, auth, body)

    boosts = fetch(BOOSTS_URL)
    boosted = boost_mints(boosts)
    mints = unique(watchlist_mints(watchlist) + boosted)
    pairs: list[dict] = []
    for url in token_endpoint_urls(mints):
        payload = fetch(url)
        if not isinstance(payload, list):
            raise WatcherError("DexScreener tokens response was not a list")
        pairs.extend(item for item in payload if isinstance(item, dict))
    quotes = quotes_by_mint(pairs)
    alerts = evaluate_alerts(watchlist, quotes, boosted, state, now)
    fresh = apply_dedupe(alerts, state, now)
    print(f"checked {len(mints)} mints, {len(fresh)} alert(s) after dedupe", flush=True)
    if fresh:
        payload = build_payload(fresh, now)
        print(json.dumps(payload, indent=2), flush=True)
        url = normalize_secret(webhook_url)
        auth = normalize_secret(webhook_auth)
        if secrets_ready(url, auth):
            deliver(url, auth, payload)
            print(f"posted {len(fresh)} alert(s)", flush=True)
        else:
            print(
                "SATOSHI_WEBHOOK_URL or SATOSHI_WEBHOOK_AUTH is missing; logged alerts and skipped POST",
                flush=True,
            )
    else:
        print("no alerts; webhook not called", flush=True)
    save_state(state_path, next_state(watchlist, quotes, fresh, state, now))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wake Satoshi when a Solana threshold is crossed.")
    parser.add_argument("--watchlist", default="watchlist.json")
    parser.add_argument("--state", default="state.json")
    parser.add_argument("--now", help="Override the check time (ISO-8601).")
    parser.add_argument("--timeout", type=float, default=15)
    args = parser.parse_args(argv)
    try:
        now = parse_time(args.now) if args.now else datetime.now(timezone.utc)
        watchlist = load_watchlist(args.watchlist)
        state = load_state(args.state)
        return run(
            watchlist=watchlist,
            state=state,
            state_path=args.state,
            now=now,
            webhook_url=os.environ.get("SATOSHI_WEBHOOK_URL", ""),
            webhook_auth=os.environ.get("SATOSHI_WEBHOOK_AUTH", ""),
            timeout=args.timeout,
        )
    except WatcherError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
