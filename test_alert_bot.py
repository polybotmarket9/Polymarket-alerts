"""Offline tests with made-up data: python3 -m unittest test_alert_bot"""
import json
import unittest

import alert_bot as ab

NOW = 1_800_000_000


def fake_get(url, params=None):
    if url.endswith("/events"):
        if params["offset"] > 0:
            return []
        return [
            {"slug": "btc-150k", "title": "BTC 150k", "markets": [
                {"conditionId": "0xA", "question": "Will Bitcoin hit $150k by Dec 31?",
                 "outcomes": '["Yes","No"]', "outcomePrices": '["0.30","0.70"]', "volume24hr": 40000}]},
            {"slug": "btc-updown", "title": "BTC up or down", "markets": [
                {"conditionId": "0xB", "question": "Bitcoin Up or Down - 3:15PM", "outcomes": '["Up","Down"]',
                 "outcomePrices": '["0.5","0.5"]', "volume24hr": 9e6}]},
        ]
    raise AssertionError(url)


def trade(usd, wallet="0xw1", side="BUY", ts=NOW - 30, tx="0xt"):
    return {"proxyWallet": wallet, "side": side, "asset": "1", "conditionId": "0xA", "size": usd / 0.3,
            "price": 0.3, "timestamp": ts, "title": "Will Bitcoin hit $150k by Dec 31?", "outcome": "Yes",
            "eventSlug": "btc-150k", "transactionHash": tx}


class Tests(unittest.TestCase):
    def setUp(self):
        self.cfg = ab.Config()
        self.markets = ab.fetch_markets(self.cfg, get=fake_get)

    def test_markets_skip_up_or_down(self):
        self.assertEqual(list(self.markets), ["0xA"])
        self.assertAlmostEqual(self.markets["0xA"]["price"], 0.30)

    def test_big_trade(self):
        alerts = ab.check_trades(self.cfg, [trade(12000)], self.markets, {}, NOW, wallet_count=lambda w: 100)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Big trade", alerts[0])

    def test_fresh_wallet(self):
        alerts = ab.check_trades(self.cfg, [trade(3000)], self.markets, {}, NOW, wallet_count=lambda w: 2)
        self.assertIn("Fresh wallet: only 1 earlier", alerts[0])

    def test_small_or_experienced_is_quiet(self):
        trades = [trade(3000, tx="a"), trade(1000, tx="b")]
        self.assertEqual(ab.check_trades(self.cfg, trades, self.markets, {}, NOW, wallet_count=lambda w: 50), [])

    def test_no_repeat_and_old_trades_ignored(self):
        state = {}
        ab.check_trades(self.cfg, [trade(12000)], self.markets, state, NOW, wallet_count=lambda w: 100)
        self.assertEqual(ab.check_trades(self.cfg, [trade(12000)], self.markets, state, NOW,
                                         wallet_count=lambda w: 100), [])
        old = trade(12000, ts=NOW - 7200, tx="old")
        self.assertEqual(ab.check_trades(self.cfg, [old], self.markets, {}, NOW, wallet_count=lambda w: 100), [])

    def test_price_jump_on_quiet_market(self):
        state = {}
        self.assertEqual(ab.check_price_moves(self.cfg, self.markets, state, NOW - 1800), [])
        self.markets["0xA"]["price"] = 0.45
        alerts = ab.check_price_moves(self.cfg, self.markets, state, NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("+15 points", alerts[0])
        # cooldown: no second alert straight away
        self.assertEqual(ab.check_price_moves(self.cfg, self.markets, state, NOW + 60), [])

    def test_price_jump_ignored_on_busy_market(self):
        state = {}
        self.markets["0xA"]["volume24hr"] = 5e6
        ab.check_price_moves(self.cfg, self.markets, state, NOW - 1800)
        self.markets["0xA"]["price"] = 0.45
        self.assertEqual(ab.check_price_moves(self.cfg, self.markets, state, NOW), [])

    def test_price_jump_ignored_on_dead_market(self):
        state = {}
        self.markets["0xA"]["volume24hr"] = 0
        ab.check_price_moves(self.cfg, self.markets, state, NOW - 1800)
        self.markets["0xA"]["price"] = 0.45
        self.assertEqual(ab.check_price_moves(self.cfg, self.markets, state, NOW), [])

    def test_telegram_payload(self):
        sent = []
        self.cfg.telegram_token, self.cfg.telegram_chat_id = "T", "123"
        ab.send_telegram(self.cfg, "hi", post=lambda url, p: sent.append((url, p)))
        self.assertEqual(sent[0][0], "https://api.telegram.org/botT/sendMessage")
        self.assertEqual(sent[0][1]["chat_id"], "123")

    def test_discover_chat_id(self):
        sent, state = [], {}
        self.cfg.telegram_token, self.cfg.telegram_chat_id = "T", ""
        upd = {"result": [{"message": {"chat": {"id": 42}}}]}
        orig = ab.send_telegram
        ab.send_telegram = lambda cfg, text: sent.append(text)
        try:
            ab.discover_chat_id(self.cfg, state, get=lambda url: upd)
        finally:
            ab.send_telegram = orig
        self.assertEqual(self.cfg.telegram_chat_id, "42")
        self.assertEqual(state["chat_id"], 42)
        self.assertIn("connected", sent[0])


if __name__ == "__main__":
    unittest.main()
