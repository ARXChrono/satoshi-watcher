"""Unit tests for the Satoshi price watcher. Network and webhook are mocked."""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError

import watcher

ROOT = Path(__file__).resolve().parents[1]
GATO = "HsprHxBRgiVaQdRf3TnKNuZZtaX9JUSPgMFdAkk6pump"
WATCHED = "B" * 32
BOOSTED = "C" * 32
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


def at(text: str) -> datetime:
    return watcher.parse_time(text)


def pair(mint, price, liquidity, h1=0, h6=0, symbol="COIN"):
    return {
        "chainId": "solana",
        "baseToken": {"address": mint, "symbol": symbol},
        "priceUsd": str(price),
        "liquidity": {"usd": liquidity},
        "priceChange": {"h1": h1, "h6": h6},
    }


def trade(**overrides):
    data = {
        "id": "P1",
        "symbol": "GATO",
        "chain": "solana",
        "mint": GATO,
        "entry": 0.007724,
        "stop": 1.0,
        "take_profit": 2.0,
        "opened_at": "2026-11-11T06:13:26Z",
    }
    data.update(overrides)
    return data


def watchlist(trades=None, watch=None, scan=None):
    return {
        "open_trades": [] if trades is None else trades,
        "watch": [] if watch is None else watch,
        "scan": {
            "min_liquidity_usd": 250000,
            "min_h1_change_pct": 15,
            "min_dip_pct": 20,
            **(scan or {}),
        },
    }


def quote(price, liquidity=300000, h1=0, h6=0, symbol="GATO"):
    return {
        "symbol": symbol,
        "price": price,
        "liquidity_usd": liquidity,
        "h1_change_pct": h1,
        "h6_change_pct": h6,
    }


class LogicTest(unittest.TestCase):
    def test_live_watchlist_is_well_formed(self):
        # watchlist.json changes as paper trades open and close, so only check its shape.
        data = watcher.load_watchlist(ROOT / "watchlist.json")
        self.assertIsInstance(data["open_trades"], list)
        self.assertIsInstance(data["watch"], list)
        self.assertEqual(data["scan"]["min_liquidity_usd"], 250000)

    def test_deepest_liquidity_pair_wins(self):
        quotes = watcher.quotes_by_mint(
            [
                pair(GATO, 0.5, 1_000, h1=90, symbol="THIN"),
                pair(GATO, 1.25, 400_000, h1=3, symbol="DEEP"),
                pair("not-a-mint", 9, 9_000_000, symbol="NOPE"),
            ]
        )
        self.assertEqual(set(quotes), {GATO})
        self.assertEqual(quotes[GATO]["price"], 1.25)
        self.assertEqual(quotes[GATO]["symbol"], "DEEP")
        self.assertEqual(quotes[GATO]["h1_change_pct"], 3)

    def test_stop_cross_and_two_percent_band(self):
        def kinds(price):
            alerts = watcher.evaluate_alerts(
                watchlist(trades=[trade(stop=100, take_profit=1000)]),
                {GATO: quote(price)},
                [],
                watcher.empty_state(),
                NOW,
            )
            return [alert["type"] for alert in alerts]

        self.assertEqual(kinds(100), ["stop"])
        self.assertEqual(kinds(99), ["stop"])
        self.assertEqual(kinds(102), ["near_stop"])
        self.assertEqual(kinds(102.01), [])
        self.assertEqual(kinds(150), [])

    def test_seeded_gato_trade_is_quiet_near_entry(self):
        # Live price on 2026-11-11 was about 0.00761, between stop and take-profit.
        alerts = watcher.evaluate_alerts(
            watcher.load_watchlist(ROOT / "watchlist.json"),
            {GATO: quote(0.00761, liquidity=290047, h1=-7.69, h6=-6.13, symbol="GATO")},
            [],
            watcher.empty_state(),
            NOW,
        )
        self.assertEqual(alerts, [])

    def test_take_profit_cross_and_two_percent_band(self):
        def kinds(price):
            alerts = watcher.evaluate_alerts(
                watchlist(trades=[trade(stop=1, take_profit=100)]),
                {GATO: quote(price)},
                [],
                watcher.empty_state(),
                NOW,
            )
            return [alert["type"] for alert in alerts]

        self.assertEqual(kinds(100), ["take_profit"])
        self.assertEqual(kinds(140), ["take_profit"])
        self.assertEqual(kinds(98), ["near_take_profit"])
        self.assertEqual(kinds(97.9), [])

    def test_time_stop_is_strictly_older_than_24h(self):
        opened = "2026-11-10T12:00:00Z"
        board = watchlist(trades=[trade(opened_at=opened, stop=1, take_profit=9)])
        quiet = {GATO: quote(5)}
        exactly = watcher.evaluate_alerts(
            board, quiet, [], watcher.empty_state(), at("2026-11-11T12:00:00Z")
        )
        later = watcher.evaluate_alerts(
            board, quiet, [], watcher.empty_state(), at("2026-11-11T12:00:01Z")
        )
        self.assertEqual(exactly, [])
        self.assertEqual(later[0]["type"], "time_stop")
        self.assertIn("older than 24h", later[0]["detail"])
        self.assertEqual(later[0]["price"], 5)

    def test_level_cross_needs_a_previous_price(self):
        item = {
            "symbol": "POP",
            "mint": WATCHED,
            "alert_above": 10,
            "alert_below": 4,
        }
        board = watchlist(watch=[item])
        first = watcher.evaluate_alerts(
            board, {WATCHED: quote(11, symbol="POP")}, [], watcher.empty_state(), NOW
        )
        self.assertEqual(first, [])
        crossed = watcher.evaluate_alerts(
            board,
            {WATCHED: quote(10, symbol="POP")},
            [],
            {"last_alerts": {}, "last_prices": {WATCHED: 9.5}},
            NOW,
        )
        self.assertEqual([alert["type"] for alert in crossed], ["alert_above"])
        self.assertIn("crossed above 10", crossed[0]["detail"])
        already_above = watcher.evaluate_alerts(
            board,
            {WATCHED: quote(12, symbol="POP")},
            [],
            {"last_alerts": {}, "last_prices": {WATCHED: 11}},
            NOW,
        )
        self.assertEqual(already_above, [])
        dipped = watcher.evaluate_alerts(
            board,
            {WATCHED: quote(4, symbol="POP")},
            [],
            {"last_alerts": {}, "last_prices": {WATCHED: 4.2}},
            NOW,
        )
        self.assertEqual(dipped[0]["type"], "alert_below")

    def test_watched_coin_dip_uses_six_hour_change(self):
        board = watchlist(watch=[{"symbol": "DIP", "mint": WATCHED, "alert_above": None, "alert_below": None}])
        no_dip = watcher.evaluate_alerts(
            board, {WATCHED: quote(1, h6=-19.9, symbol="DIP")}, [], watcher.empty_state(), NOW
        )
        dip = watcher.evaluate_alerts(
            board, {WATCHED: quote(1, h6=-20, symbol="DIP")}, [], watcher.empty_state(), NOW
        )
        self.assertEqual(no_dip, [])
        self.assertEqual(dip[0]["type"], "dip")
        self.assertEqual(dip[0]["symbol"], "DIP")

    def test_trending_filters_liquidity_and_h1_on_the_deepest_pair(self):
        thin = pair(BOOSTED, 1, 1_000, h1=80, symbol="HOT")
        deep_flat = pair(BOOSTED, 2, 300_000, h1=14.9, symbol="HOT")
        quotes = watcher.quotes_by_mint([thin, deep_flat])
        quiet = watcher.evaluate_alerts(
            watchlist(), quotes, [BOOSTED], watcher.empty_state(), NOW
        )
        self.assertEqual(quiet, [])
        deep_hot = pair(BOOSTED, 2, 250_000, h1=15, symbol="HOT")
        alerts = watcher.evaluate_alerts(
            watchlist(),
            watcher.quotes_by_mint([thin, deep_hot]),
            [BOOSTED],
            watcher.empty_state(),
            NOW,
        )
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["type"], "trending")
        self.assertEqual(alerts[0]["symbol"], "HOT")
        self.assertEqual(alerts[0]["price"], 2)
        self.assertIn("liquidity 250000 USD", alerts[0]["detail"])

    def test_non_solana_boosts_are_ignored(self):
        boosts = [
            {"chainId": "ethereum", "tokenAddress": BOOSTED},
            {"chainId": "solana", "tokenAddress": "not valid"},
            {"chainId": "solana", "tokenAddress": BOOSTED},
        ]
        self.assertEqual(watcher.boost_mints(boosts), [BOOSTED])

    def test_token_batches_split_at_30(self):
        alphabet = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
        mints = [("D" * 31) + alphabet[i] for i in range(31)]
        self.assertTrue(all(watcher.valid_mint(mint) for mint in mints))
        urls = watcher.token_endpoint_urls(mints)
        self.assertEqual(len(urls), 2)
        self.assertIn(",".join(mints[:30]), urls[0])
        self.assertTrue(urls[1].endswith("/" + mints[30]))
        self.assertNotIn(mints[30], urls[0])

    def test_dedupe_is_per_coin_and_condition_for_60_minutes(self):
        alerts = watcher.evaluate_alerts(
            watchlist(trades=[trade(stop=100, take_profit=1000)]),
            {GATO: quote(90)},
            [],
            watcher.empty_state(),
            NOW,
        )
        state = watcher.next_state(watchlist(), {}, alerts, watcher.empty_state(), NOW)
        suppressed = watcher.apply_dedupe(alerts, state, NOW + timedelta(minutes=59, seconds=59))
        allowed = watcher.apply_dedupe(alerts, state, NOW + timedelta(minutes=60))
        self.assertEqual(suppressed, [])
        self.assertEqual([alert["type"] for alert in allowed], ["stop"])
        # A different condition on the same coin is not suppressed by the stop alert.
        aged = watchlist(trades=[trade(stop=1, take_profit=9, opened_at="2026-11-10T00:00:00Z")])
        both = watcher.evaluate_alerts(aged, {GATO: quote(5)}, [], state, NOW + timedelta(hours=1))
        fresh = watcher.apply_dedupe(both, state, NOW + timedelta(hours=1))
        self.assertEqual([alert["type"] for alert in fresh], ["time_stop"])

    def test_payload_shape_and_single_list(self):
        alerts = [
            watcher._alert("stop", "GATO", GATO, 0.9, "P1 hit stop", "P1"),
            watcher._alert("dip", "POP", WATCHED, 1.5, "6h change -25%", ""),
        ]
        payload = watcher.build_payload(alerts, NOW)
        self.assertEqual(payload["source"], "satoshi-watcher")
        self.assertEqual(payload["checked_at"], "2026-11-11T12:00:00Z")
        self.assertEqual(
            payload["alerts"],
            [
                {"type": "stop", "symbol": "GATO", "mint": GATO, "price": 0.9, "detail": "P1 hit stop"},
                {"type": "dip", "symbol": "POP", "mint": WATCHED, "price": 1.5, "detail": "6h change -25%"},
            ],
        )


class FakeResponse:
    def __init__(self, body, status=200):
        self.status = status
        self._body = body if isinstance(body, bytes) else json.dumps(body).encode()

    def read(self):
        return self._body

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class RunTest(unittest.TestCase):
    def test_mocked_api_posts_once_when_a_stop_is_crossed(self):
        calls = []

        def fake_open(request, timeout):
            calls.append(request)
            url = request.full_url
            if request.get_method() == "POST":
                return FakeResponse({"ok": True})
            if url == watcher.BOOSTS_URL:
                return FakeResponse(
                    [
                        {"chainId": "solana", "tokenAddress": BOOSTED},
                        {"chainId": "base", "tokenAddress": WATCHED},
                    ]
                )
            self.assertIn(GATO, url)
            self.assertIn(BOOSTED, url)
            self.assertNotIn(WATCHED, url)
            return FakeResponse(
                [
                    pair(GATO, 0.9, 290000, h1=-1, h6=-2, symbol="GATO"),
                    pair(BOOSTED, 3, 1000, h1=40, symbol="THIN"),
                ]
            )

        board = watchlist(trades=[trade(stop=1.0, take_profit=2.0)])
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch("watcher._open", side_effect=fake_open):
                code = watcher.run(
                    watchlist=board,
                    state=watcher.empty_state(),
                    state_path=state_path,
                    now=NOW,
                    webhook_url="https://hooks.example.test/satoshi",
                    webhook_auth="Bearer super-secret",
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        posts = [request for request in calls if request.get_method() == "POST"]
        gets = [request for request in calls if request.get_method() == "GET"]
        self.assertEqual(len(posts), 1)
        self.assertEqual(len(gets), 2)
        post = posts[0]
        self.assertEqual(post.full_url, "https://hooks.example.test/satoshi")
        self.assertEqual(post.get_header("Authorization"), "Bearer super-secret")
        body = json.loads(post.data.decode())
        self.assertEqual(body["source"], "satoshi-watcher")
        self.assertEqual(body["checked_at"], "2026-11-11T12:00:00Z")
        self.assertEqual(len(body["alerts"]), 1)
        self.assertEqual(body["alerts"][0]["type"], "stop")
        self.assertEqual(body["alerts"][0]["symbol"], "GATO")
        self.assertEqual(body["alerts"][0]["mint"], GATO)
        self.assertEqual(body["alerts"][0]["price"], 0.9)
        self.assertIn(watcher.alert_key(body["alerts"][0] | {"trade_id": "P1"}), saved["last_alerts"])
        self.assertNotIn("super-secret", json.dumps(saved))

    def test_no_post_when_nothing_crosses(self):
        def fake_open(request, timeout):
            if request.get_method() == "POST":
                raise AssertionError("webhook was called without an alert")
            if request.full_url == watcher.BOOSTS_URL:
                return FakeResponse([])
            return FakeResponse([pair(GATO, 1.5, 290000, h1=1, h6=-1, symbol="GATO")])

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with patch("watcher._open", side_effect=fake_open):
                code = watcher.run(
                    watchlist=watchlist(trades=[trade(stop=1, take_profit=2)]),
                    state=watcher.empty_state(),
                    state_path=state_path,
                    now=NOW,
                    webhook_url="https://hooks.example.test/satoshi",
                    webhook_auth="Bearer super-secret",
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertEqual(saved["last_alerts"], {})

    def test_main_missing_secrets_does_not_post(self):
        def fake_get(url, timeout=15):
            if "token-boosts" in url:
                return []
            return [pair(GATO, 0.5, 10, symbol="GATO")]

        with tempfile.TemporaryDirectory() as tmp:
            watch_path = Path(tmp) / "watchlist.json"
            state_path = Path(tmp) / "state.json"
            watch_path.write_text(
                json.dumps(watchlist(trades=[trade(stop=1, take_profit=3)])),
                encoding="utf-8",
            )
            with patch("watcher.http_get_json", side_effect=fake_get), patch(
                "watcher.http_post", side_effect=AssertionError("posted without secrets")
            ), patch.dict(
                "os.environ",
                {"SATOSHI_WEBHOOK_URL": "  ", "SATOSHI_WEBHOOK_AUTH": ""},
                clear=False,
            ):
                code = watcher.main(
                    [
                        "--watchlist",
                        str(watch_path),
                        "--state",
                        str(state_path),
                        "--now",
                        "2026-11-11T12:00:00Z",
                    ]
                )
            saved = json.loads(state_path.read_text(encoding="utf-8"))
        self.assertEqual(code, 0)
        self.assertTrue(any(key.endswith("|stop|P1") for key in saved["last_alerts"]))

    def test_failed_post_does_not_record_dedupe(self):
        def fake_get(url):
            if url == watcher.BOOSTS_URL:
                return []
            return [pair(GATO, 0.5, 10, symbol="GATO")]

        def fake_post(url, auth, payload):
            raise watcher.WatcherError("webhook returned HTTP 503")

        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            with self.assertRaises(watcher.WatcherError):
                watcher.run(
                    watchlist=watchlist(trades=[trade(stop=1, take_profit=3)]),
                    state=watcher.empty_state(),
                    state_path=state_path,
                    now=NOW,
                    webhook_url="https://hooks.example.test/satoshi",
                    webhook_auth="Bearer secret",
                    get_json=fake_get,
                    post=fake_post,
                )
            self.assertFalse(state_path.exists())

    def test_level_cross_is_posted_on_the_following_run(self):
        prices = {"price": 9.0}

        def fake_get(url):
            if url == watcher.BOOSTS_URL:
                return []
            return [pair(WATCHED, prices["price"], 10, h6=-1, symbol="POP")]

        posts = []

        def fake_post(url, auth, payload):
            posts.append(payload)

        board = watchlist(
            watch=[{"symbol": "POP", "mint": WATCHED, "alert_above": 10, "alert_below": None}]
        )
        with tempfile.TemporaryDirectory() as tmp:
            state_path = Path(tmp) / "state.json"
            watcher.run(
                board,
                watcher.empty_state(),
                state_path,
                NOW,
                "https://hooks.example.test/satoshi",
                "Bearer secret",
                get_json=fake_get,
                post=fake_post,
            )
            self.assertEqual(posts, [])
            prices["price"] = 10.5
            watcher.run(
                board,
                watcher.load_state(state_path),
                state_path,
                NOW + timedelta(minutes=5),
                "https://hooks.example.test/satoshi",
                "Bearer secret",
                get_json=fake_get,
                post=fake_post,
            )
        self.assertEqual(len(posts), 1)
        self.assertEqual(posts[0]["alerts"][0]["type"], "alert_above")
        self.assertEqual(posts[0]["alerts"][0]["price"], 10.5)

    def test_webhook_redirect_is_not_followed(self):
        def fake_open(request, timeout):
            raise HTTPError(request.full_url, 302, "Found", hdrs=None, fp=None)

        with patch("watcher._open", side_effect=fake_open):
            with self.assertRaises(watcher.WatcherError) as caught:
                watcher.http_post("https://hooks.example.test/satoshi", "Bearer secret", {"alerts": []})
        self.assertIn("HTTP 302", str(caught.exception))


class WorkflowTest(unittest.TestCase):
    def test_workflow_contract(self):
        text = (ROOT / ".github" / "workflows" / "watch.yml").read_text(encoding="utf-8")
        self.assertIn('cron: "3-58/5 * * * *"', text)
        self.assertIn("workflow_dispatch:", text)
        self.assertIn("contents: write", text)
        self.assertIn("group: satoshi-watcher", text)
        self.assertIn("cancel-in-progress: false", text)
        self.assertIn("SATOSHI_WEBHOOK_URL", text)
        self.assertIn("SATOSHI_WEBHOOK_AUTH", text)
        self.assertIn("refs/heads/state", text)
        self.assertIn("python3 watcher.py", text)
        self.assertNotIn("npm ", text)
        self.assertNotIn("pip ", text)


if __name__ == "__main__":
    unittest.main()
