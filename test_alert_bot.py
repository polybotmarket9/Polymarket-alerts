"""Offline tests with made-up data: python3 -m unittest test_alert_bot"""
import json
import unittest

import alert_bot as ab

NOW = 1_800_000_000
NOBODY = lambda w: {"all": {"profit": 0, "volume": 0}, "month": {"profit": 0, "volume": 0}}
WINNER = lambda w: {"all": {"profit": 85000, "volume": 500000}, "month": {"profit": 4000, "volume": 60000}}


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


US_EVENTS = [
    {"slug": "nfl-dal-phi-2026-10-26", "title": "DAL Cowboys vs. PHI Eagles", "markets": [
        {"slug": "aec-nfl-dal-phi-2026-10-26", "title": "DAL Cowboys vs PHI Eagles",
         "sportsMarketTypeV2": "SPORTS_MARKET_TYPE_MONEYLINE", "outcomes": '["Cowboys","Eagles"]'}]},
    {"slug": "btc-150k", "title": "When will Bitcoin hit $150k?", "markets": [
        {"slug": "btc-150k-jun", "title": "Before July 2026", "outcomes": '["Yes","No"]'},
        {"slug": "btc-150k-dec", "title": "Before January 2027", "outcomes": '["Yes","No"]'}]},
    {"slug": "btc-200k", "title": "When will Bitcoin hit $200k?", "markets": [
        {"slug": "btc-200k-dec", "title": "Before February 2027", "outcomes": '["Yes","No"]'}]},
    {"slug": "usp-2028", "title": "2028 US Presidential Election Winner", "markets": [
        {"slug": "usp-pb", "title": "Pete Buttigieg", "outcomes": '["Yes","No"]'}]},
    {"slug": "nba-champ-2027", "title": "2027 NBA Champion", "markets": [
        {"slug": "nba-ind", "title": "Indiana Pacers", "outcomes": '["Yes","No"]'}]},
    {"slug": "meet-trump", "title": "Who will Trump meet in 2026?", "markets": [
        {"slug": "meet-putin", "title": "Vladimir Putin", "outcomes": '["Yes","No"]'}]},
]


class Tests(unittest.TestCase):
    def setUp(self):
        self.cfg = ab.Config()
        self.cfg.us_only = False
        self.markets = ab.fetch_markets(self.cfg, get=fake_get)

    def test_markets_skip_up_or_down(self):
        self.assertEqual(list(self.markets), ["0xA"])
        self.assertAlmostEqual(self.markets["0xA"]["price"], 0.30)

    def test_big_trade(self):
        alerts = ab.check_trades(self.cfg, [trade(12000)], self.markets, {}, NOW, wallet_count=lambda w: 100, record=NOBODY)
        self.assertEqual(len(alerts), 1)
        self.assertIn("Big bet", alerts[0])

    def test_fresh_wallet(self):
        alerts = ab.check_trades(self.cfg, [trade(3000)], self.markets, {}, NOW, wallet_count=lambda w: 2, record=NOBODY)
        self.assertIn("New account: only 1 earlier", alerts[0])

    def test_small_or_experienced_is_quiet(self):
        trades = [trade(3000, tx="a"), trade(1000, tx="b")]
        self.assertEqual(ab.check_trades(self.cfg, trades, self.markets, {}, NOW, wallet_count=lambda w: 50, record=NOBODY), [])

    def test_no_repeat_and_old_trades_ignored(self):
        state = {}
        ab.check_trades(self.cfg, [trade(12000)], self.markets, state, NOW, wallet_count=lambda w: 100, record=NOBODY)
        self.assertEqual(ab.check_trades(self.cfg, [trade(12000)], self.markets, state, NOW,
                                         wallet_count=lambda w: 100, record=NOBODY), [])
        old = trade(12000, ts=NOW - 7200, tx="old")
        self.assertEqual(ab.check_trades(self.cfg, [old], self.markets, {}, NOW, wallet_count=lambda w: 100, record=NOBODY), [])

    def test_price_jump_on_quiet_market(self):
        state = {}
        self.assertEqual(ab.check_price_moves(self.cfg, self.markets, state, NOW - 1800), [])
        self.markets["0xA"]["price"] = 0.45
        alerts = ab.check_price_moves(self.cfg, self.markets, state, NOW)
        self.assertEqual(len(alerts), 1)
        self.assertIn("jumped from 30¢ to 45¢", alerts[0])
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

    def test_signal_side(self):
        a = ab.check_trades(self.cfg, [trade(15000)], self.markets, {}, NOW, wallet_count=lambda w: 1, record=NOBODY)[0]
        self.assertIn("Signal strength: 🟢 strong", a)
        self.assertIn("tap <b>Yes</b>", a)
        self.assertNotIn("bet Yes", a)
        sell = trade(12000, side="SELL", tx="s")
        a = ab.check_trades(self.cfg, [sell], self.markets, {}, NOW, wallet_count=lambda w: 100, record=NOBODY)[0]
        self.assertIn("Signal strength: 🟡 medium", a)
        self.assertIn("tap <b>No</b>", a)

    def test_price_drop_signals_other_side(self):
        state = {}
        ab.check_price_moves(self.cfg, self.markets, state, NOW - 1800)
        self.markets["0xA"]["price"] = 0.15
        a = ab.check_price_moves(self.cfg, self.markets, state, NOW)[0]
        self.assertIn("Signal strength: 🟠 weak", a)
        self.assertIn("tap <b>No</b>", a)

    def test_fetch_trades_keeps_our_markets_and_stops_at_window(self):
        calls = []
        def get(url, params):
            calls.append(params["offset"])
            ts = NOW - 60 if params["offset"] == 0 else NOW - 9999
            return [dict(trade(5000, ts=ts), conditionId=("0xA" if i % 2 else "0xZ")) for i in range(500)]
        out = ab.fetch_trades(["0xA"], 2000, since=NOW - 3600, get=get)
        self.assertEqual(len(out), 500)  # 250 per page from 2 pages
        self.assertEqual(calls, [0, 500])

    def test_http_get_retries_on_429(self):
        import io, urllib.error
        attempts = []
        def fake_open(req, timeout):
            attempts.append(1)
            if len(attempts) < 3:
                raise urllib.error.HTTPError(req.full_url, 429, "slow down", {"Retry-After": "0"}, io.BytesIO())
            return io.BytesIO(b'[1]')
        orig = ab.urllib.request.urlopen
        ab.urllib.request.urlopen = fake_open
        try:
            self.assertEqual(ab.http_get("https://x"), [1])
        finally:
            ab.urllib.request.urlopen = orig
        self.assertEqual(len(attempts), 3)

    def test_proven_winner_alerts_on_smaller_bet(self):
        state = {}
        a = ab.check_trades(self.cfg, [trade(3000)], self.markets, state, NOW,
                            wallet_count=lambda w: 100, record=WINNER)
        self.assertEqual(len(a), 1)
        self.assertIn("Proven winner", a[0])
        self.assertIn("made $85k on Polymarket (earns 17¢ per $1 bet), up $4,000 this month", a[0])
        self.assertIn("strength: 🟢 strong", a[0])
        self.assertEqual(state["scorecard"][0]["strength"], "strong")
        self.assertEqual(state["scorecard"][0]["side"], "Yes")

    def test_losing_trader_with_small_bet_is_quiet(self):
        loser = lambda w: {"all": {"profit": 30000, "volume": 2e6}, "month": {"profit": -100, "volume": 1e5}}
        self.assertEqual(ab.check_trades(self.cfg, [trade(3000)], self.markets, {}, NOW,
                                         wallet_count=lambda w: 100, record=loser), [])

    def test_trader_record_reads_leaderboard(self):
        def get(url, params):
            return [{"pnl": 1234.5, "vol": 10000}] if params["timePeriod"] == "all" else [{"pnl": -5, "vol": 50}]
        ab._records.clear()
        rec = ab.trader_record("0xabc", get=get)
        self.assertEqual(rec, {"all": {"profit": 1234.5, "volume": 10000}, "month": {"profit": -5, "volume": 50}})
        self.assertFalse(ab.is_proven_winner(self.cfg, rec))

    def test_near_certain_and_repeat_bets_are_quiet(self):
        sure = dict(trade(15000), price=0.97, size=15000 / 0.97)
        self.assertEqual(ab.check_trades(self.cfg, [sure], self.markets, {}, NOW,
                                         wallet_count=lambda w: 100, record=NOBODY), [])
        state = {}
        first = ab.check_trades(self.cfg, [trade(15000, tx="r1")], self.markets, state, NOW,
                                wallet_count=lambda w: 100, record=NOBODY)
        again = ab.check_trades(self.cfg, [trade(15000, tx="r2", ts=NOW + 60)], self.markets, state, NOW + 90,
                                wallet_count=lambda w: 100, record=NOBODY)
        self.assertEqual((len(first), len(again)), (1, 0))

    def test_scorecard_settles_and_summarises(self):
        state = {"scorecard": [
            {"t": NOW, "cid": "0xA", "title": "x", "slug": "x", "side": "Yes", "price": 0.25, "strength": "strong"},
            {"t": NOW, "cid": "0xB", "title": "y", "slug": "y", "side": "No", "price": 0.5, "strength": "medium"},
            {"t": NOW, "cid": "0xC", "title": "z", "slug": "z", "side": "Yes", "price": 0.5, "strength": "weak"}]}
        def get(url, params):
            return [{"conditionId": "0xA", "closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]'},
                    {"conditionId": "0xB", "closed": True, "outcomes": '["Yes","No"]', "outcomePrices": '["1","0"]'},
                    {"conditionId": "0xC", "closed": False, "outcomes": '["Yes","No"]', "outcomePrices": '["0.6","0.4"]'}]
        ab.resolve_scorecard(state, get=get)
        self.assertEqual([a.get("result") for a in state["scorecard"]], ["won", "lost", None])
        text = ab.summary_text(state)
        self.assertIn("Settled: 1 won, 1 lost (50%)", text)
        self.assertIn("🟢 strong 1-0 · 🟡 medium 0-1", text)
        self.assertIn("If you'd bet $100 on each: +$200", text)  # +300 on the 25c win, -100 on the loss
        self.assertIn("Waiting to settle: 1 alert", text)

    def test_score_command_only_from_own_chat(self):
        sent, state = [], {}
        self.cfg.telegram_token, self.cfg.telegram_chat_id = "T", "42"
        upd = {"result": [{"update_id": 7, "message": {"chat": {"id": 42}, "text": "/score"}},
                          {"update_id": 8, "message": {"chat": {"id": 99}, "text": "/score"}}]}
        orig = ab.send_telegram
        ab.send_telegram = lambda cfg, text: sent.append(text)
        try:
            ab.handle_commands(self.cfg, state, get=lambda url, params: upd)
        finally:
            ab.send_telegram = orig
        self.assertEqual(len(sent), 1)
        self.assertIn("Scorecard", sent[0])
        self.assertEqual(state["tg_offset"], 8)

    def test_split_bet_from_new_wallets(self):
        state = {}
        trades = [dict(trade(2000, wallet=f"0xn{i}", tx=f"s{i}"), outcomeIndex=0) for i in range(3)]
        a = ab.check_split_bets(self.cfg, trades, self.markets, state, NOW, wallet_count=lambda w: 1)
        self.assertEqual(len(a), 1)
        self.assertIn("3 brand-new accounts bought <b>Yes</b>", a[0])
        self.assertIn("$6,000 total", a[0])
        self.assertEqual(state["scorecard"][0]["cat"], "crypto")
        # Same group again straight away: no repeat.
        self.assertEqual(ab.check_split_bets(self.cfg, trades, self.markets, state, NOW + 60,
                                             wallet_count=lambda w: 1), [])

    def test_split_bet_ignores_experienced_wallets(self):
        trades = [dict(trade(2000, wallet=f"0xo{i}", tx=f"o{i}"), outcomeIndex=0) for i in range(3)]
        self.assertEqual(ab.check_split_bets(self.cfg, trades, self.markets, {}, NOW,
                                             wallet_count=lambda w: 50), [])

    def test_late_alert_is_skipped(self):
        self.markets["0xA"]["price"] = 0.45  # Yes already 15 points above the 30c trade
        late = dict(trade(15000), outcomeIndex=0)
        self.assertEqual(ab.check_trades(self.cfg, [late], self.markets, {}, NOW,
                                         wallet_count=lambda w: 100, record=NOBODY), [])

    def test_sports_price_jumps_are_skipped(self):
        state = {}
        self.markets["0xA"]["cat"] = "sports"
        ab.check_price_moves(self.cfg, self.markets, state, NOW - 1800)
        self.markets["0xA"]["price"] = 0.45
        self.assertEqual(ab.check_price_moves(self.cfg, self.markets, state, NOW), [])

    def test_summary_by_category(self):
        row = lambda cat, res: {"t": NOW, "cid": "x", "title": "", "slug": "", "side": "Yes",
                                "price": 0.5, "strength": "medium", "cat": cat, "result": res}
        state = {"scorecard": [row("politics", "won"), row("politics", "won"), row("sports", "lost"),
                               row("sports", "won"), row("crypto", "lost")]}
        text = ab.summary_text(state)
        self.assertIn("Politics: 2-0 (100%) · +$200", text)
        self.assertIn("Sports: 1-1 (50%) · +$0", text)
        self.assertIn("Crypto: 0-1 (0%) · -$100", text)
        self.assertLess(text.index("Politics"), text.index("Sports"))
        self.assertLess(text.index("Sports"), text.index("Crypto"))

    def test_sports_needs_bigger_trade(self):
        self.markets["0xA"]["cat"] = "sports"
        quiet = ab.check_trades(self.cfg, [trade(15000, tx="sp1")], self.markets, {}, NOW,
                                wallet_count=lambda w: 100, record=NOBODY)
        loud = ab.check_trades(self.cfg, [trade(60000, tx="sp2")], self.markets, {}, NOW,
                               wallet_count=lambda w: 100, record=NOBODY)
        self.assertEqual((len(quiet), len(loud)), (0, 1))
        self.assertIn("over $50,000", loud[0])

    def test_spread_is_explained(self):
        self.assertEqual(ab.explain_market("Spread: Packers (-14.5)", ["Packers", "Cowboys"]),
                         "ℹ️ Packers must win by 15+ points for “Packers” to pay. “Cowboys” pays otherwise.")
        self.assertIn("above or below 47.5", ab.explain_market("Cowboys vs. Packers: O/U 47.5", ["Over", "Under"]))
        self.assertIsNone(ab.explain_market("Will Bitcoin hit $150k?", ["Yes", "No"]))

    def test_payout_line(self):
        self.assertIn("64¢ a share, pays $1 if right ($100 → $156)", ab.how_to(None, "Cowboys", "🟢 strong", 0.64, "x", "Cowboys vs. Packers"))

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

    # ---- Polymarket US

    def us_catalog(self):
        def get(url, params):
            return {"events": [] if params["offset"] else US_EVENTS}
        return ab.fetch_us_catalog(get)

    def test_us_game_matches_winner_only(self):
        cat = self.us_catalog()
        hit = ab.match_us(cat, "nfl-dal-phi-2026-10-26", "Cowboys vs. Eagles", "Eagles")
        self.assertEqual((hit["event"], hit["button"]), ("nfl-dal-phi-2026-10-26", "Eagles"))
        self.assertIsNone(ab.match_us(cat, "nfl-dal-phi-2026-10-26", "Spread: Eagles (-3.5)", "Eagles"))
        self.assertIsNone(ab.match_us(cat, "nfl-dal-phi-2026-10-26", "Cowboys vs. Eagles: O/U 47.5", "Over"))
        self.assertIsNone(ab.match_us(cat, "nfl-sf-sea-2026-10-26", "49ers vs. Seahawks", "49ers"))

    def test_us_non_game_by_title(self):
        cat = self.us_catalog()
        hit = ab.match_us(cat, "will-bitcoin-hit-150k", "Will Bitcoin reach $150,000 by December 31, 2026?", "Yes")
        self.assertEqual((hit["event"], hit["title"], hit["button"]), ("btc-150k", "Before January 2027", "Yes"))
        # Different price level or deadline: not the same bet.
        self.assertIsNone(ab.match_us(cat, "x", "Will Bitcoin reach $200,000 by December 31, 2026?", "Yes"))
        self.assertIsNone(ab.match_us(cat, "x", "Will Bitcoin reach $150,000 by March 31, 2026?", "Yes"))
        self.assertIsNone(ab.match_us(cat, "x", "Will Ethereum flip Bitcoin in 2026?", "Yes"))

    def test_us_rejects_near_misses(self):
        cat = self.us_catalog()
        self.assertIsNone(ab.match_us(cat, "x", "Will Pete Hegseth win the 2028 US Presidential Election?", "No"))
        self.assertIsNone(ab.match_us(cat, "x", "Will the Indiana Pacers make the 2027 NBA Playoffs?", "No"))
        self.assertIsNone(ab.match_us(cat, "x", "Will Trump and Putin not meet?", "No"))
        self.assertIsNone(ab.match_us(cat, "nfl-dal-phi-2026-10-26", "Cowboys vs. Eagles: Team to Score First", "Eagles"))
        hit = ab.match_us(cat, "x", "Will Pete Buttigieg win the 2028 US Presidential Election?", "No")
        self.assertEqual((hit["event"], hit["button"]), ("usp-2028", "No"))
        hit = ab.match_us(cat, "x", "Will the Indiana Pacers win the 2027 NBA Finals?", "Yes")
        self.assertEqual(hit["event"], "nba-champ-2027")

    def test_us_only_drops_and_steps(self):
        cat = self.us_catalog()
        us = lambda slug, q, side: ab.match_us(cat, slug, q, side)
        self.assertEqual(ab.check_trades(self.cfg, [trade(12000)], self.markets, {}, NOW,
                                         wallet_count=lambda w: 100, record=NOBODY, us=us), [])
        t = dict(trade(12000, tx="us"), title="Will Bitcoin reach $150,000 by December 31, 2026?")
        state = {}
        a = ab.check_trades(self.cfg, [t], self.markets, state, NOW, wallet_count=lambda w: 100,
                            record=NOBODY, us=us)[0]
        self.assertIn("https://polymarket.us/event/btc-150k", a)
        self.assertIn("Find “Before January 2027” and tap <b>Yes</b>", a)
        self.assertIn("1. Open the Polymarket US app", a)
        self.assertNotIn("polymarket.com/event", a)
        self.assertEqual(state["scorecard"][0]["us"], "btc-150k")

    def test_us_game_steps_skip_market_name(self):
        place = ab.match_us(self.us_catalog(), "nfl-dal-phi-2026-10-26", "Cowboys vs. Eagles", "Cowboys")
        text = ab.how_to(place, "Cowboys", "🟢 strong", 0.59, "nfl-dal-phi-2026-10-26", "Cowboys vs. Eagles")
        self.assertIn("3. Tap <b>Cowboys</b>", text)


if __name__ == "__main__":
    unittest.main()
