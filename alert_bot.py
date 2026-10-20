#!/usr/bin/env python3
"""Polymarket crypto alert bot.

Watches public Polymarket data (Gamma API for markets, Data API for trades)
and sends Telegram alerts when trading looks like it
might be informed:

  1. BIG TRADE     - one trade worth at least BIG_TRADE_USD.
  2. FRESH WALLET  - a wallet with almost no trading history places a sizeable bet.
  3. PRICE JUMP    - a market's price moves a lot within an hour while it had
                     little trading volume before.
  4. PROVEN WINNER - a trader with a strong public track record places a bet.
  5. SPLIT BET     - several brand-new wallets back the same side within minutes,
                     which is often one person hiding a big bet.

Alerts are skipped when the price has already run well past what the trader paid.

Every alert is logged to a scorecard, settled when the market resolves, and
summarised weekly in Telegram (or any time you send the bot /score).

Only public market data is used. Standard library only, no pip install needed.

Usage:
  python3 alert_bot.py            # run forever, checking every POLL_SECONDS
  python3 alert_bot.py --once     # one check, then exit (for cron/schedulers)
  python3 alert_bot.py --find-chat-id   # print chat ids that messaged your bot
"""
import argparse
import calendar
import email.utils
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET

GAMMA = "https://gamma-api.polymarket.com"
DATA = "https://data-api.polymarket.com"


def env_float(name, default):
    return float(os.environ.get(name, default))


def env_int(name, default):
    return int(os.environ.get(name, default))


class Config:
    def __init__(self):
        self.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
        self.telegram_chat_id = os.environ.get("TELEGRAM_CHAT_ID", "")
        # Polymarket categories to watch, and the name each gets in alerts and the scorecard.
        self.tags = [t.strip() for t in os.environ.get(
            "MARKET_TAGS", os.environ.get("MARKET_TAG", "crypto,politics,sports,business")).split(",") if t.strip()]
        # Skip very short-term "up or down in 15 minutes" style markets; they are
        # dominated by bots and would flood you with noise.
        self.exclude = [k.strip().lower() for k in
                        os.environ.get("EXCLUDE_KEYWORDS", "up or down").split(",") if k.strip()]
        self.poll_seconds = env_int("POLL_SECONDS", 60)
        self.big_trade_usd = env_float("BIG_TRADE_USD", 10000)
        # Sports markets are far busier, so a $10k bet there is routine; use a higher bar.
        self.big_trade_usd_sports = env_float("BIG_TRADE_USD_SPORTS", 50000)
        self.fresh_wallet_min_usd = env_float("FRESH_WALLET_MIN_USD", 2000)
        # Only alert on big bets: new-account, proven-winner and split-bet alerts also need
        # BIG_TRADE_USD (BIG_TRADE_USD_SPORTS for sports) in total. They then only make it stronger.
        self.big_bets_only = os.environ.get("BIG_BETS_ONLY", "true").lower() not in ("0", "false", "no")
        self.fresh_wallet_max_prior_trades = env_int("FRESH_WALLET_MAX_PRIOR_TRADES", 5)
        self.price_move_points = env_float("PRICE_MOVE_POINTS", 10)  # 10 = 10 cents = 10%
        self.price_move_window_min = env_int("PRICE_MOVE_WINDOW_MIN", 60)
        self.price_move_max_volume_24h = env_float("PRICE_MOVE_MAX_VOLUME_24H", 100000)
        # Markets with almost no trading "move" on stale quotes alone; ignore those.
        self.price_move_min_volume_24h = env_float("PRICE_MOVE_MIN_VOLUME_24H", 1000)
        self.max_markets = env_int("MAX_MARKETS", 300)  # per category
        # Live sports odds swing constantly during games, so price jumps there are noise.
        self.price_move_skip = [c.strip() for c in
                                os.environ.get("PRICE_MOVE_SKIP_CATEGORIES", "sports").split(",") if c.strip()]
        # Split bets: this many new wallets, each betting at least cluster_min_usd on the same
        # side within cluster_window_min, adding up to at least cluster_total_usd.
        self.cluster_wallets = env_int("CLUSTER_WALLETS", 3)
        self.cluster_min_usd = env_float("CLUSTER_MIN_USD", 1000)
        self.cluster_total_usd = env_float("CLUSTER_TOTAL_USD", 5000)
        self.cluster_window_min = env_int("CLUSTER_WINDOW_MIN", 30)
        # Skip an alert if the price has already moved this many points past the trader's price.
        self.late_max_points = env_float("LATE_MAX_POINTS", 10)
        self.market_refresh_min = env_int("MARKET_REFRESH_MIN", 10)
        # Only alert on bets you can actually place on Polymarket US (polymarket.us).
        self.us_only = os.environ.get("US_ONLY", "true").lower() not in ("0", "false", "no")
        self.us_refresh_min = env_int("US_REFRESH_MIN", 30)
        # A "proven winner" has at least this many settled bets, this win rate and profit.
        self.winner_min_usd = env_float("WINNER_MIN_USD", 2000)
        self.winner_min_profit = env_float("WINNER_MIN_PROFIT", 10000)
        self.winner_min_return = env_float("WINNER_MIN_RETURN", 0.05)  # profit / amount traded
        # Bets on a side already priced 95c+ are near-certain and carry little information.
        self.max_signal_price = env_float("MAX_SIGNAL_PRICE", 0.95)
        # Only alert on fresh trades, and only once per trader per event every 12 hours.
        self.max_trade_age_min = env_int("MAX_TRADE_AGE_MIN", 15)
        self.repeat_hours = env_float("REPEAT_HOURS", 12)
        self.summary_days = env_float("SUMMARY_DAYS", 7)
        # Add recent news headlines (Google News) to alerts. A big bet with no news yet counts as
        # strong, except in sports, where small matches rarely make the news anyway.
        self.news = os.environ.get("NEWS", "true").lower() not in ("0", "false", "no")
        self.news_hours = env_float("NEWS_HOURS", 48)
        # Grok X check: on alerts scoring GROK_MIN_SCORE+, ask Grok (xAI) what X is saying about the
        # market. Off unless XAI_API_KEY is set. Capped per hour to keep the cost small.
        self.xai_key = os.environ.get("XAI_API_KEY", "").strip().strip("\"'")
        self.grok_model = os.environ.get("GROK_MODEL", "grok-4.7")
        self.grok_min_score = env_int("GROK_MIN_SCORE", 60)
        self.grok_max_per_hour = env_int("GROK_MAX_PER_HOUR", 10)
        # Watch profitable traders' X accounts (linked on their Polymarket profile) for posted picks.
        self.watch_traders = os.environ.get("WATCH_TRADERS", "true").lower() not in ("0", "false", "no")
        self.watch_every_min = env_int("WATCH_EVERY_MIN", 45)
        self.watch_top = env_int("WATCH_TOP", 500)  # leaderboard places to scan, per period
        self.watch_min_profit = env_float("WATCH_MIN_PROFIT", 50000)
        self.watch_extra = [h.strip().lstrip("@") for h in os.environ.get("EXTRA_X_HANDLES", "").split(",")
                            if h.strip()]
        self.scorecard_file = os.environ.get(
            "SCORECARD_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "scorecard.json"))
        self.state_file = os.environ.get(
            "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))


# ---------------------------------------------------------------- HTTP helpers

def http_get(url, params=None, timeout=20, retries=4):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-alert-bot/1.0"})
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as err:
            # Back off when rate-limited (429) or the server hiccups (5xx).
            if attempt == retries or (err.code != 429 and err.code < 500):
                raise
            wait = err.headers.get("Retry-After")
            time.sleep(float(wait) if wait and wait.isdigit() else 2 ** (attempt + 1))


def http_get_text(url, params=None, timeout=20):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 polymarket-alert-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def http_post(url, payload, timeout=20):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


# ---------------------------------------------------------------- Polymarket

def as_list(value):
    """Gamma returns some list fields as JSON-encoded strings."""
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            return []
    return value or []


CATEGORY_NAMES = {"crypto": "Crypto", "politics": "Politics", "sports": "Sports", "business": "Company news"}


def category_name(tag):
    return CATEGORY_NAMES.get(tag, tag.replace("-", " ").title())


def fetch_markets(cfg, get=http_get):
    """Active markets in the configured categories, keyed by conditionId."""
    markets = {}
    for tag in cfg.tags:
        fetch_category(cfg, tag, markets, get)
    return markets


def fetch_category(cfg, tag, markets, get):
    start = len(markets)
    offset = 0
    while len(markets) - start < cfg.max_markets:
        events = get(f"{GAMMA}/events", {"tag_slug": tag, "active": "true", "closed": "false",
                                         "limit": 100, "offset": offset,
                                         "order": "volume24hr", "ascending": "false"})
        if not events:
            break
        for ev in events:
            for m in ev.get("markets", []):
                if m.get("closed") or not m.get("conditionId") or m["conditionId"] in markets:
                    continue
                q = m.get("question") or ev.get("title", "")
                if any(k in q.lower() for k in cfg.exclude):
                    continue
                prices = [float(p) for p in as_list(m.get("outcomePrices"))]
                markets[m["conditionId"]] = {
                    "question": q,
                    "slug": ev.get("slug") or m.get("slug", ""),
                    "outcomes": as_list(m.get("outcomes")),
                    "price": prices[0] if prices else None,  # price of first outcome (usually "Yes")
                    "volume24hr": float(m.get("volume24hr") or 0),
                    "one_hour_change": m.get("oneHourPriceChange"),
                    "cat": tag,
                }
        if len(events) < 100:
            break
        offset += 100


def fetch_trades(condition_ids, min_usd, since, get=http_get, max_pages=10):
    """Trades of at least min_usd since `since` in the given markets, newest first.

    Reads Polymarket's site-wide feed of large trades (a few requests) and keeps the
    ones in our markets, instead of asking about every market separately."""
    wanted = set(condition_ids)
    trades = []
    for page in range(max_pages):
        batch = get(f"{DATA}/trades", {"limit": 500, "offset": page * 500, "takerOnly": "true",
                                       "filterType": "CASH", "filterAmount": int(min_usd)})
        trades += [t for t in batch if t.get("conditionId") in wanted]
        if len(batch) < 500 or int(batch[-1].get("timestamp", 0)) < since:
            break
    return trades


_wallet_counts = {}


def wallet_trade_count(wallet, limit, get=http_get):
    """How many trades this wallet has ever made, capped at limit (cached for an hour)."""
    hit = _wallet_counts.get(wallet)
    if hit and time.time() - hit[1] < 3600:
        return hit[0]
    n = len(get(f"{DATA}/trades", {"user": wallet, "limit": limit}))
    _wallet_counts[wallet] = (n, time.time())
    return n


_records = {}


def trader_record(wallet, get=http_get):
    """A wallet's public results from Polymarket's leaderboard: profit and amount traded,
    all-time and over the last month (cached 6h)."""
    hit = _records.get(wallet)
    if hit and time.time() - hit[1] < 6 * 3600:
        return hit[0]
    rec = {}
    for period in ("all", "month"):
        row = get(f"{DATA}/v1/leaderboard", {"timePeriod": period, "orderBy": "PNL", "user": wallet})
        row = row[0] if row else {}
        rec[period] = {"profit": float(row.get("pnl") or 0), "volume": float(row.get("vol") or 0)}
    _records[wallet] = (rec, time.time())
    return rec


def is_proven_winner(cfg, rec):
    """Big all-time profit, a solid return on what they've traded, and still winning lately."""
    total = rec["all"]
    return (total["profit"] >= cfg.winner_min_profit
            and total["volume"] > 0 and total["profit"] / total["volume"] >= cfg.winner_min_return
            and rec["month"]["profit"] > 0)


def short_money(x):
    a = abs(x)
    text = f"${a / 1e6:.1f}M" if a >= 1e6 else f"${a / 1e3:.0f}k" if a >= 1e4 else f"${a:,.0f}"
    return ("-" if x < 0 else "+") + text


def record_text(rec):
    total, month = rec["all"], rec["month"]
    if not total["volume"]:
        return "no trading record yet"
    cents = round(100 * total["profit"] / total["volume"])
    made = "made" if total["profit"] >= 0 else "lost"
    per = f"{'earns' if cents >= 0 else 'loses'} {abs(cents)}¢ per $1 bet"
    trend = "up" if month["profit"] >= 0 else "down"
    return (f"{made} {short_money(total['profit'])[1:]} on Polymarket ({per}), "
            f"{trend} {short_money(month['profit'])[1:]} this month")


# ---------------------------------------------------------------- rules

def trade_usd(t):
    return float(t.get("size", 0)) * float(t.get("price", 0))


def trade_id(t):
    return f'{t.get("transactionHash")}:{t.get("asset")}:{t.get("proxyWallet")}:{t.get("size")}'


def side_price_now(m, outcome_index):
    """Latest known price of one side of a market, or None."""
    if m.get("price") is None or outcome_index not in (0, 1):
        return None
    return m["price"] if outcome_index == 0 else 1 - m["price"]


def big_bar(cfg, m):
    """What counts as a big bet in this market's category."""
    return cfg.big_trade_usd_sports if m.get("cat") == "sports" else cfg.big_trade_usd


def is_late(cfg, m, outcome_index, paid):
    """True when the price has already run LATE_MAX_POINTS past what the trader paid."""
    now_price = side_price_now(m, outcome_index)
    return now_price is not None and (now_price - paid) * 100 > cfg.late_max_points


def check_trades(cfg, trades, markets, state, now, wallet_count=None, record=None, us=None, news=None, buzz=None):
    """Return a list of alert messages for trades not seen before. `us(event_slug, question, side)`
    says where to place the bet on Polymarket US; alerts it can't place are dropped."""
    wallet_count = wallet_count or (lambda w: wallet_trade_count(w, cfg.fresh_wallet_max_prior_trades + 2))
    record = record or trader_record
    seen = state.setdefault("seen_trades", {})
    repeats = state.setdefault("trader_alerted", {})
    alerts = []
    for t in trades:
        tid = trade_id(t)
        if tid in seen:
            continue
        seen[tid] = now
        # Ignore old trades (e.g. the backlog on the very first run).
        if now - int(t.get("timestamp", now)) > cfg.max_trade_age_min * 60:
            continue
        usd = trade_usd(t)
        m = markets.get(t.get("conditionId"), {})
        reasons, prior = [], None
        big = big_bar(cfg, m)
        if cfg.big_bets_only and usd < big:
            continue
        if usd >= big:
            reasons.append(f"💰 Big bet: over ${big:,.0f} in one go")
        if usd >= cfg.fresh_wallet_min_usd and t.get("side") == "BUY":
            n = wallet_count(t["proxyWallet"])
            # The trade itself counts, so "prior" = n - 1.
            if n - 1 <= cfg.fresh_wallet_max_prior_trades:
                prior = max(n - 1, 0)
                reasons.append(f"🆕 New account: only {prior} earlier trades on Polymarket")
        rec, winner = None, False
        if reasons or (usd >= cfg.winner_min_usd and t.get("side") == "BUY"):
            rec = record(t["proxyWallet"])
            winner = t.get("side") == "BUY" and usd >= cfg.winner_min_usd and is_proven_winner(cfg, rec)
            if winner:
                reasons.insert(0, "🏆 Proven winner (record below)")
        if reasons:
            side, price = follow_side(t, m)
            idx = t.get("outcomeIndex")
            side_idx = idx if t.get("side") != "SELL" else (1 - idx if idx in (0, 1) else None)
            if price >= cfg.max_signal_price:
                continue
            if is_late(cfg, m, side_idx, price):
                print(f"skipped late alert: {t.get('title')}", flush=True)
                continue
            key = f'{t["proxyWallet"]}|{t.get("eventSlug") or t.get("conditionId")}'
            if now - repeats.get(key, 0) < cfg.repeat_hours * 3600:
                continue
            title, slug = t.get("title") or m.get("question", ""), t.get("eventSlug") or m.get("slug", "")
            place = us(slug, title, side) if us else None
            if us and not place:
                print(f"skipped, not on Polymarket US: {title} ({side})", flush=True)
                continue
            repeats[key] = now
            headlines = news(title) if news else None
            score, why = trade_score(cfg, t, m, usd, price, rec, winner, prior, headlines)
            score, why, x_line = x_check(cfg, state, now, title, side, score, why, buzz)
            alerts.append(format_trade_alert(t, m, usd, score, why, rec, place, x_line))
            log_alert(state, now, t.get("conditionId"), title, slug, side, price,
                      band(score), m.get("cat"), place, score)
    # Forget old entries so the state file stays small.
    for d in (seen, repeats):
        for k in [k for k, v in d.items() if now - v > 86400]:
            del d[k]
    return alerts


def check_split_bets(cfg, trades, markets, state, now, wallet_count=None, us=None, news=None, buzz=None):
    """Alert when several brand-new wallets back the same side of a market in a short time."""
    wallet_count = wallet_count or (lambda w: wallet_trade_count(w, cfg.fresh_wallet_max_prior_trades + 2))
    window = cfg.cluster_window_min * 60
    buys = state.setdefault("recent_buys", {})
    seen = state.setdefault("cluster_seen", {})
    cooldown = state.setdefault("cluster_alerted", {})
    for t in trades:
        usd = trade_usd(t)
        if t.get("side") != "BUY" or usd < cfg.cluster_min_usd or now - int(t.get("timestamp", 0)) > window:
            continue
        tid = trade_id(t)
        if tid in seen:
            continue
        seen[tid] = now
        key = f'{t.get("conditionId")}|{t.get("outcomeIndex")}'
        buys.setdefault(key, []).append([int(t.get("timestamp", now)), t["proxyWallet"], usd,
                                         float(t.get("price", 0)), t.get("outcome", ""), t.get("title", "")])
    alerts = []
    for key, rows in list(buys.items()):
        rows[:] = [r for r in rows if now - r[0] <= window]
        if not rows:
            del buys[key]
            continue
        wallets = {}
        for r in rows:
            wallets[r[1]] = wallets.get(r[1], 0) + r[2]
        # Cheap checks first; only look wallets up when the group is big enough.
        if len(wallets) < cfg.cluster_wallets or sum(wallets.values()) < cfg.cluster_total_usd \
                or now - cooldown.get(key, 0) < window:
            continue
        fresh = {w: v for w, v in wallets.items()
                 if wallet_count(w) - 1 <= cfg.fresh_wallet_max_prior_trades}
        total = sum(fresh.values())
        cid, idx = key.split("|")
        m = markets.get(cid, {})
        need = max(cfg.cluster_total_usd, big_bar(cfg, m)) if cfg.big_bets_only else cfg.cluster_total_usd
        if len(fresh) < cfg.cluster_wallets or total < need:
            continue
        paid = sum(r[3] * r[2] for r in rows if r[1] in fresh) / total
        idx = int(idx) if idx.lstrip("-").isdigit() else None
        if paid >= cfg.max_signal_price or is_late(cfg, m, idx, paid):
            continue
        cooldown[key] = now
        side, title = rows[-1][4], rows[-1][5] or m.get("question", "")
        place = us(m.get("slug", ""), title, side) if us else None
        if us and not place:
            print(f"skipped, not on Polymarket US: {title} ({side})", flush=True)
            continue
        score, why = split_score(cfg, m, len(fresh), total, paid, news(title) if news else None)
        score, why, x_line = x_check(cfg, state, now, title, side, score, why, buzz)
        alerts.append(format_split_alert(m, title, side, len(fresh), total, paid, cfg, score, why, place, x_line))
        log_alert(state, now, cid, title, m.get("slug", ""), side, paid, band(score), m.get("cat"), place, score)
    for k in [k for k, v in seen.items() if now - v > 86400]:
        del seen[k]
    for k in [k for k, v in cooldown.items() if now - v > 86400]:
        del cooldown[k]
    return alerts


def check_price_moves(cfg, markets, state, now, us=None, news=None, buzz=None):
    """Alert when a low-volume market's price moves PRICE_MOVE_POINTS within the window."""
    history = state.setdefault("prices", {})
    cooldown = state.setdefault("price_alerted", {})
    window = cfg.price_move_window_min * 60
    alerts = []
    for cid, m in markets.items():
        if m["price"] is None or m.get("cat") in cfg.price_move_skip:
            continue
        points = history.setdefault(cid, [])
        points.append([now, m["price"]])
        points[:] = [p for p in points if now - p[0] <= window]
        oldest = points[0][1]
        move = (m["price"] - oldest) * 100
        if abs(move) < cfg.price_move_points and m.get("one_hour_change") is not None \
                and cfg.price_move_window_min == 60:
            move = float(m["one_hour_change"]) * 100  # use Polymarket's own 1h change if bigger
        if abs(move) < cfg.price_move_points or \
                not cfg.price_move_min_volume_24h <= m["volume24hr"] <= cfg.price_move_max_volume_24h:
            continue
        if now - cooldown.get(cid, 0) < window:
            continue
        cooldown[cid] = now
        side, price = price_move_side(m, move)
        place = us(m["slug"], m["question"], side) if us else None
        if us and not place:
            continue
        score, why = price_score(cfg, m, move, price, news(m["question"]) if news else None)
        score, why, x_line = x_check(cfg, state, now, m["question"], side, score, why, buzz)
        alerts.append(format_price_alert(m, move, cfg, score, why, place, x_line))
        log_alert(state, now, cid, m["question"], m["slug"], side, price, band(score), m.get("cat"), place, score)
    for cid in [c for c in history if c not in markets]:
        del history[cid]
    for cid in [c for c, v in cooldown.items() if now - v > 86400]:
        del cooldown[cid]
    return alerts


# ---------------------------------------------------------------- Polymarket US

US_GATEWAY = "https://gateway.polymarket.us"
US_SITE = "https://polymarket.us"
# Game sub-markets that Polymarket US mostly doesn't list (it only has the winner).
PROP_WORDS = re.compile(r"spread|o/u|over/under|total|\bhalf\b|\b1h\b|\b2h\b|quarter|\bmap \d|\bgame \d|"
                        r"\bset \d|\bround \d|first|both teams|exact score|player|points|rebounds|assists|"
                        r"handicap|corners|cards", re.I)
STOP_WORDS = {"will", "the", "a", "an", "of", "in", "on", "by", "be", "to", "for", "and", "or", "at", "is",
              "vs", "who", "what", "when", "which", "before", "after", "than", "above", "price", "s", "win"}
SYNONYMS = {"reach": "hit", "hits": "hit", "reaches": "hit", "btc": "bitcoin", "eth": "ethereum",
            "sol": "solana", "democrat": "democratic", "democrats": "democratic",
            "republicans": "republican", "gop": "republican", "us": "usa", "greater": "above",
            "dips": "dip", "wins": "win", "winner": "win", "champions": "champion", "finals": "champion",
            "championship": "champion"}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"])}
DATE = re.compile(r"\b(before|by|in|end of|on)?\s*(" + "|".join(MONTHS) + r")\b(?:\s+(\d{1,2})(?!\d))?"
                  r"(?:\s*(?:st|nd|rd|th))?,?(?:\s+(20\d\d))?")
TIME = re.compile(r"\b\d{1,2}(?::\d\d)?\s*[ap]\.?m\b|\bnoon\b|\bmidnight\b")
_us = {"at": 0, "catalog": None}


def plain(text):
    return (text or "").lower().replace(",", "").replace("’", "'")


def words(text):
    """Comparable words of a title: lower case, $150k -> 150000, synonyms merged. Dates are left out
    (they're compared separately with `deadline`)."""
    t = DATE.sub(" ", plain(text))
    t = re.sub(r"(\d+(?:\.\d+)?)\s*([km])\b",
               lambda g: f"{float(g.group(1)) * (1000 if g.group(2) == 'k' else 1e6):.0f}", t)
    t = re.sub(r"(\d)\.0+\b", r"\1", t)
    out = set()
    for w in re.findall(r"[a-z]+|\d+(?:\.\d+)?", t):
        w = SYNONYMS.get(w, w)
        if w not in STOP_WORDS:
            out.add(w)
    return out


def deadline(text):
    """(year, month, day) a market is about, any part None if unsaid; "before January 2027" ->
    (2026, 12, 31). None if no date."""
    g = DATE.search(plain(text))
    if not g:
        return None
    word, mo = g.group(1), MONTHS[g.group(2)]
    day = int(g.group(3)) if g.group(3) else None
    year = int(g.group(4)) if g.group(4) else None
    if word == "before" and day is None:
        if mo == 1:
            year, mo = (year - 1 if year else None), 12
        else:
            mo -= 1
        day = calendar.monthrange(year or 2001, mo)[1]
    return year, mo, day


def same_date(a, b):
    if a is None or b is None:
        return a is b
    if (a[2] is None) != (b[2] is None):  # a whole month vs one day
        return False
    return all(x is None or y is None or x == y for x, y in zip(a, b))


def has_time(text, slug=""):
    return bool(TIME.search(plain(text)) or re.search(r"\d{4}z$|-hr-", slug or ""))


def fetch_us_catalog(get=http_get):
    """Every open Polymarket US market: {"by_slug": {event slug: [market]}, "all": [market]}."""
    by_slug, everything, offset = {}, [], 0
    while offset < 50000:
        data = get(f"{US_GATEWAY}/v1/events", {"limit": 200, "offset": offset, "active": "true", "closed": "false"})
        events = data.get("events", []) if isinstance(data, dict) else data
        for ev in events:
            for m in ev.get("markets", []):
                if m.get("closed") or m.get("active") is False:
                    continue
                title = m.get("title") or m.get("question") or ev.get("title", "")
                both = f'{ev.get("title", "")} {title}'
                entry = {"event": ev.get("slug", ""), "event_title": ev.get("title", ""), "title": title,
                         "outcomes": [str(o) for o in as_list(m.get("outcomes"))],
                         "words": words(both), "title_words": words(title),
                         "deadline": deadline(title) or deadline(ev.get("title", "")),
                         "timed": has_time(both, ev.get("slug"))}
                by_slug.setdefault(entry["event"], []).append(entry)
                everything.append(entry)
        if len(events) < 200:
            break
        offset += 200
    return {"by_slug": by_slug, "all": everything}


def us_catalog(cfg, get=http_get):
    """The Polymarket US market list, refreshed every US_REFRESH_MIN; keeps the old one on errors."""
    if time.time() - _us["at"] >= cfg.us_refresh_min * 60:
        try:
            _us["catalog"], _us["at"] = fetch_us_catalog(get), time.time()
            print(f"Polymarket US: {len(_us['catalog']['all'])} open markets", flush=True)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as err:
            print(f"couldn't load Polymarket US markets: {err}", file=sys.stderr, flush=True)
    return _us["catalog"]


def pick_side(entry, side):
    """The button to tap on a US market for this side, or None if it can't be placed there."""
    outs = {o.lower(): o for o in entry["outcomes"]}
    s = str(side).lower()
    if s in outs:
        return outs[s]
    # A US "Yes/No" market about one team or person: backing them on .com means Yes there.
    if "yes" in outs and s not in ("yes", "no") and words(side) and words(side) <= entry["title_words"]:
        return outs["yes"]
    return None


def match_us(catalog, event_slug, question, side, loose=False):
    """Find where to place this bet on Polymarket US. Returns {"event", "event_title", "title",
    "button"} or None when Polymarket US doesn't clearly offer the same bet."""
    if not catalog or PROP_WORDS.search(question or ""):
        return None
    games = catalog["by_slug"].get(event_slug or "")
    q, due, timed = words(question), deadline(question), has_time(question)
    if games is not None:
        # Same game on both sites (sports events share slugs): only the game winner is listed.
        hits = [e for e in games if pick_side(e, side) and e["title_words"] <= q | words(side) | e["words"]
                and (not due or not e["deadline"] or same_date(due, e["deadline"]))]
    else:
        # Elsewhere, every word of the question (names, numbers, "playoffs", "not"...) must appear
        # on the US market, the US market's own words must all be in the question, and the dates
        # must agree. Anything looser picked the wrong person, team or deadline in testing.
        hits = [e for e in catalog["all"]
                if q <= e["words"] and (loose or e["title_words"] <= q) and len(q) >= 2
                and same_date(due, e["deadline"]) and timed == e["timed"] and pick_side(e, side)]
    if len(hits) != 1:
        return None  # nothing, or several equally good fits: don't guess
    return dict(hits[0], button=pick_side(hits[0], side))


def us_link(slug):
    return f"{US_SITE}/event/{slug}"

# ---------------------------------------------------------------- news

NEWS_URL = "https://news.google.com/rss/search"
NEWS_SKIP = {"will", "the", "a", "an", "vs", "yes", "no", "who", "what", "when", "which", "how", "spread",
             "o/u", "over", "under", "by", "in", "on", "of", "be", "is", "next", "up", "down"} | set(MONTHS)
_news_cache = {}


def news_query(title):
    """Search words for a market: its names and proper nouns ("Pete Buttigieg Presidential Election")."""
    t = re.sub(r"\(.*?\)|[?:!,\"“”]", " ", title or "")
    names = [w for w in t.split() if any(c.isupper() for c in w) and w.lower().strip(".'") not in NEWS_SKIP]
    return " ".join(names[:6])


def ago(seconds):
    if seconds < 3600:
        return f"{max(seconds // 60, 1):.0f} min ago"
    if seconds < 86400:
        return f"{seconds // 3600:.0f}h ago"
    return f"{seconds // 86400:.0f}d ago"


def key_terms(q):
    return {w for w in re.findall(r"[a-z0-9]+", q.lower()) if len(w) > 2}


def is_about(headline, q):
    terms = key_terms(q)
    return len(terms & set(re.findall(r"[a-z0-9]+", headline.lower()))) >= min(2, len(terms))


def fetch_news(title, hours=48, get_text=http_get_text, now=None):
    """Up to 3 newest headlines about a market from the last `hours`: [{"title", "source", "link",
    "age"}]. [] means no news found; None means the search didn't work (or had nothing to search)."""
    q = news_query(title)
    if len(key_terms(q)) < 2:
        return None  # one word like "Bitcoin" is always in the news, so news timing says nothing
    now = now or time.time()
    cached = _news_cache.get(q)
    if cached and now - cached[0] < 600:
        return cached[1]
    try:
        root = ET.fromstring(get_text(NEWS_URL, {"q": f"{q} when:{max(int(hours // 24), 1)}d",
                                                 "hl": "en-US", "gl": "US", "ceid": "US:en"}))
    except (urllib.error.URLError, TimeoutError, ET.ParseError, ValueError) as err:
        print(f"news search failed for {q!r}: {err}", file=sys.stderr, flush=True)
        return None
    items = []
    for it in root.iter("item"):
        try:
            age = now - email.utils.parsedate_to_datetime(it.findtext("pubDate", "")).timestamp()
        except (TypeError, ValueError):
            continue
        if age > hours * 3600:
            continue
        source = it.findtext("source", "")
        headline = it.findtext("title", "")
        if source and headline.endswith(" - " + source):
            headline = headline[: -len(source) - 3]
        # Google matches loosely; keep only headlines that actually name the market's subject.
        if not is_about(headline, q):
            continue
        items.append({"title": headline, "source": source, "link": it.findtext("link", ""), "age": age})
    items = sorted(items, key=lambda i: i["age"])[:3]
    _news_cache[q] = (now, items)
    return items


# ---------------------------------------------------------------- score

def band(score):
    """Scorecard group for a score: strong 70+, medium 45-69, weak under 45."""
    return "strong" if score >= 70 else "medium" if score >= 45 else "weak"


def score_lines(score, why):
    dot = {"strong": "🟢", "medium": "🟡", "weak": "🟠"}[band(score)]
    why = sorted(why, key=lambda f: -abs(f[0]))
    return "\n".join([f"📊 <b>Score: {dot} {score}/100</b>"] +
                      [f"{'+' if p > 0 else '−'}{abs(p)} {html.escape(text)}" for p, text in why])


def total_score(base, factors, cap=100):
    factors = [f for f in factors if f and f[0]]
    return max(0, min(cap, base + sum(p for p, _ in factors))), factors


def size_factor(usd, m):
    """A bet that is large next to the market's normal daily trading says more."""
    vol = m.get("volume24hr") or 0
    if vol <= 0:
        return None
    share = usd / vol
    pts = 25 if share >= 0.5 else 18 if share >= 0.2 else 10 if share >= 0.05 else 4 if share >= 0.01 else 0
    if share >= 1:
        return pts, "Bigger than a whole day of normal trading in this market"
    return pts, f"Bet is {share:.0%} of this market's daily trading"


def news_factor(headlines, cat):
    """Betting before any news is suspicious; betting right after news is just reacting."""
    if headlines is None:
        return None  # couldn't check
    if not headlines:
        # Small sports matches rarely make the news, so silence means little there.
        return None if cat == "sports" else (15, "No news about this yet: the trader may be ahead of it")
    if headlines[0]["age"] < 2 * 3600:
        return -10, f"News came out {ago(headlines[0]['age'])}: the trader may just be reacting"
    return None


def price_factor(price):
    """Big money on a long shot shows more conviction than on a heavy favorite."""
    if price <= 0.25:
        return 10, f"Long shot at {price * 100:.0f}¢: betting big on it shows conviction"
    if price <= 0.40:
        return 5, f"Underdog at {price * 100:.0f}¢"
    if price >= 0.80:
        return -5, f"Already the favorite at {price * 100:.0f}¢, so less to learn"
    return None


def trade_score(cfg, t, m, usd, price, rec, winner, prior, headlines):
    factors = [size_factor(usd, m), news_factor(headlines, m.get("cat")), price_factor(price)]
    big = big_bar(cfg, m)
    if usd >= big * 10:
        factors.append((10, f"Huge bet: ${usd:,.0f}"))
    elif usd >= big * 5:
        factors.append((6, f"Very big bet: ${usd:,.0f}"))
    elif usd >= big * 2.5:
        factors.append((3, f"Big bet: ${usd:,.0f}"))
    if prior is not None:
        factors.append((25, "Brand-new account: this is its first trade") if prior == 0
                       else (20, f"New account: only {prior} earlier trades"))
    if rec:
        profit, vol = rec["all"]["profit"], rec["all"]["volume"]
        if winner:
            factors.append((20, "Proven winner with a strong track record"))
            if vol and profit / vol >= 0.15:
                factors.append((5, f"Earns {profit / vol * 100:.0f}¢ per $1 bet"))
            if rec["month"]["profit"] >= 10000:
                factors.append((5, f"Up {short_money(rec['month']['profit']).lstrip('+')} this month"))
        elif profit <= -10000:
            factors.append((-10, f"This trader has lost {short_money(-profit).lstrip('+')} overall"))
    return total_score(25, factors)


def split_score(cfg, m, n, total, paid, headlines):
    return total_score(25, [(30, f"{n} brand-new accounts on the same side: often one person hiding a big bet"),
                            size_factor(total, m), news_factor(headlines, m.get("cat")), price_factor(paid)])


def price_score(cfg, m, move, price, headlines):
    jump = abs(move)
    return total_score(10, [(15, f"Quiet market: only ${m['volume24hr']:,.0f} traded a day, yet the price moved"),
                            (10 if jump >= 20 else 5, f"Moved {jump:.0f} points in {cfg.price_move_window_min} min"),
                            news_factor(headlines, m.get("cat")), price_factor(price)], cap=60)

# ---------------------------------------------------------------- Grok X check

XAI_URL = "https://api.x.ai/v1/responses"
GROK_PROMPT = """Use X search to look at posts from the last 24 hours about the topic of this question:
"{title}" (someone just bet "{side}").
Search for the people, teams or things it is about, not for the betting market itself (posts rarely
mention the market). Is anyone on X talking about this topic right now, and is there any breaking news,
rumor or leak that would explain a bet on "{side}"?
Reply with only JSON, no other text:
{{"buzz": "none" | "some" | "lots", "newest_minutes_ago": <minutes since the newest relevant post, or null>,
  "summary": "<one short plain sentence a beginner understands, max 25 words>"}}"""


def http_post_auth(url, payload, key, timeout=90):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def response_text(resp):
    """The model's text from an xAI Responses API reply."""
    if resp.get("output_text"):
        return resp["output_text"]
    parts = []
    for item in resp.get("output", []):
        for c in item.get("content", []) or []:
            if c.get("type") in ("output_text", "text") and c.get("text"):
                parts.append(c["text"])
    return "\n".join(parts)


def grok_json(cfg, prompt, tool, post=http_post_auth, debug=False):
    """Ask Grok with an X search tool and parse the JSON it replies with, or None."""
    try:
        resp = post(XAI_URL, {"model": cfg.grok_model, "input": prompt, "tools": [tool]}, cfg.xai_key)
        if debug:
            print("output items:", [i.get("type") for i in resp.get("output", [])],
                  "citations:", len(resp.get("citations") or []), flush=True)
        # No search actually ran: the answer would be a guess, so don't use it.
        if not any("tool_call" in (i.get("type") or "") for i in resp.get("output", [])):
            print("Grok answered without searching X; ignoring it", file=sys.stderr, flush=True)
            return None
        text = response_text(resp)
        return json.loads(text[text.index("{"): text.rindex("}") + 1])
    except urllib.error.HTTPError as err:
        print(f"Grok request failed: {err}: {err.read().decode(errors='replace')[:300]}", file=sys.stderr, flush=True)
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as err:
        print(f"Grok request failed: {err}", file=sys.stderr, flush=True)
    return None


def grok_buzz(cfg, title, side, now=None, post=http_post_auth, debug=False):
    """Ask Grok what X is saying about a market: {"buzz", "newest_minutes_ago", "summary"} or None."""
    day = time.strftime("%Y-%m-%d", time.gmtime((now or time.time()) - 86400))
    found = grok_json(cfg, GROK_PROMPT.format(title=title, side=side), {"type": "x_search", "from_date": day},
                      post, debug)
    if not isinstance(found, dict) or found.get("buzz") not in ("none", "some", "lots"):
        return None
    return found


def x_check(cfg, state, now, title, side, score, why, buzz=None):
    """On high-scoring alerts, add what X is saying. Returns (score, why, line for the alert or None)."""
    if buzz is None:
        if not cfg.xai_key:
            return score, why, None
        buzz = lambda title, side: grok_buzz(cfg, title, side, now)
    if score < cfg.grok_min_score:
        return score, why, None
    calls = [c for c in state.get("grok_calls", []) if now - c < 3600]
    if len(calls) >= cfg.grok_max_per_hour:
        return score, why, None
    state["grok_calls"] = calls + [now]
    found = buzz(title, side)
    if not found:
        return score, why, None
    newest = found.get("newest_minutes_ago")
    factor = None
    if found["buzz"] == "none":
        factor = (10, "Nobody on X is talking about this yet")
    elif found["buzz"] == "lots" and isinstance(newest, (int, float)) and newest <= 120:
        factor = (-10, "X is already buzzing about it: the trader may be reacting")
    if factor:
        why = why + [factor]
        score = max(0, min(100, score + factor[0]))
    line = f"𝕏 <b>Grok on X:</b> {html.escape(str(found.get('summary', '')))}" if found.get("summary") else None
    return score, why, line

# ---------------------------------------------------------------- top traders on X

WATCH_PROMPT = """Use X search to read posts from these accounts: {handles}.
Find posts made in the last {minutes} minutes (after {since} UTC) where the account shares a pick, a bet
they placed, or a confident call on an upcoming outcome (a game, election, price level, event...).
Ignore jokes, ads, replies with no pick, and posts older than that.
Reply with only JSON, no other text:
{{"posts": [{{"handle": "<account, no @>", "minutes_ago": <minutes since posted>,
  "pick": "<what they are backing, one short plain sentence>",
  "question": "<the yes/no question or matchup it is about, e.g. 'Will Bitcoin hit $150k by December 31, 2026?' or 'Cowboys vs. Eagles'>",
  "side": "<the side they back: Yes, No, or a team/person name>",
  "url": "<link to the post if known, else empty>"}}]}}
Use {{"posts": []}} if there are none."""


def build_watchlist(cfg, get=http_get):
    """Profitable traders who linked an X account: {handle: {"name", "wallet", "month", "all"}}."""
    found = {}
    for period in ("month", "all"):
        seen = set()
        for offset in range(0, cfg.watch_top, 50):
            try:
                rows = get(f"{DATA}/v1/leaderboard", {"timePeriod": period, "orderBy": "PNL",
                                                      "limit": 50, "offset": offset})
            except (urllib.error.URLError, TimeoutError, ValueError) as err:
                print(f"couldn't read leaderboard: {err}", file=sys.stderr, flush=True)
                break
            new = [r for r in rows or [] if r.get("proxyWallet") not in seen]
            if not new:
                break
            for r in new:
                seen.add(r.get("proxyWallet"))
                handle = (r.get("xUsername") or "").strip().lstrip("@")
                pnl = float(r.get("pnl") or 0)
                if not handle or pnl < cfg.watch_min_profit:
                    continue
                entry = found.setdefault(handle, {"name": r.get("userName") or handle,
                                                  "wallet": r.get("proxyWallet"), "month": None, "all": None})
                entry[period] = pnl
    for handle in cfg.watch_extra:
        found.setdefault(handle, {"name": handle, "wallet": None, "month": None, "all": None})
    return found


def trader_line(info):
    parts = []
    if info.get("month"):
        parts.append(f"up {short_money(info['month']).lstrip('+')} this month")
    if info.get("all"):
        parts.append(f"{short_money(info['all']).lstrip('+')} profit all-time")
    return " · ".join(parts) or "on your watchlist"


def format_post_alert(post, info, place):
    e = html.escape
    lines = [f"🗣 <b>@{e(post['handle'])}</b> just posted a pick on X",
             f"👤 Polymarket trader {e(info.get('name', post['handle']))}: {e(trader_line(info))}",
             f"💬 {e(post.get('pick', ''))}"]
    if post.get("url", "").startswith("https://"):
        lines.append(f'<a href="{e(post["url"])}">See the post</a>')
    if place:
        lines.append(how_to(place, post.get("side", ""), None, "", post.get("question", "")))
    else:
        lines.append(f"🔎 Couldn't match this to a Polymarket US market. To look yourself, search "
                     f"“{e(post.get('question', ''))}” in the Polymarket US app.")
    lines.append("<i>Traders sometimes post after they've already bought. Treat it as a clue, not a sure thing.</i>")
    return "\n".join(lines)


def check_trader_posts(cfg, state, now, catalog=None, ask=None, max_minutes=6 * 60, debug=False):
    """Every WATCH_EVERY_MIN, ask Grok whether watched traders posted picks. Returns alert texts."""
    if not (cfg.xai_key or ask) or not cfg.watch_traders:
        return []
    if now - state.get("watch_checked", 0) < cfg.watch_every_min * 60:
        return []
    if now - state.get("watchlist_at", 0) > 86400 or "watchlist" not in state:
        state["watchlist"], state["watchlist_at"] = build_watchlist(cfg), now
        print(f"watching {len(state['watchlist'])} traders on X", flush=True)
    minutes = int(min(now - state.get("watch_checked", now - cfg.watch_every_min * 60), max_minutes * 60) // 60) + 5
    state["watch_checked"] = now
    since = time.strftime("%Y-%m-%d %H:%M", time.gmtime(now - minutes * 60))
    day = time.strftime("%Y-%m-%d", time.gmtime(now - 86400))
    ask = ask or (lambda prompt, handles: grok_json(cfg, prompt, {"type": "x_search", "from_date": day,
                                                                  "allowed_x_handles": handles}, debug=debug))
    watch = state["watchlist"]
    handles = sorted(watch)
    seen = state.setdefault("x_seen", {})
    alerts = []
    for i in range(0, len(handles), 10):
        batch = handles[i:i + 10]
        found = ask(WATCH_PROMPT.format(handles=", ".join("@" + h for h in batch), minutes=minutes, since=since),
                    batch)
        if debug:
            print("batch", batch, "->", json.dumps(found)[:1500], flush=True)
        for p in (found or {}).get("posts", []) if isinstance(found, dict) else []:
            handle = str(p.get("handle", "")).lstrip("@")
            match = next((h for h in batch if h.lower() == handle.lower()), None)
            age = p.get("minutes_ago")
            if not match or not p.get("pick") or not isinstance(age, (int, float)) or age > minutes:
                continue
            key = f'{match}|{p.get("url") or p.get("pick")}'
            if key in seen:
                continue
            seen[key] = now
            p["handle"] = match
            # Post wording is free-form ("Cowboys vs. Eagles"), so allow extra words on the US side.
            place = match_us(catalog, "", p.get("question", ""), p.get("side", ""), loose=True) if catalog else None
            alerts.append(format_post_alert(p, watch[match], place))
    for k in [k for k, v in seen.items() if now - v > 3 * 86400]:
        del seen[k]
    return alerts

# ---------------------------------------------------------------- messages

def market_link(slug):
    return f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"


def other_outcome(outcomes, outcome):
    """The opposite side of a two-outcome market, e.g. Yes -> No."""
    rest = [o for o in outcomes if str(o) != str(outcome)]
    return rest[0] if len(outcomes) == 2 and rest else f"not {outcome}"


def category_suffix(m):
    return f" · {category_name(m['cat'])}" if m.get("cat") else ""


def format_split_alert(m, title, side, n, total, paid, cfg, score, why, place=None, x_line=None):
    e = html.escape
    return "\n".join([
        f"🧩 <b>{e(title)}</b>{category_suffix(m)}",
        f"{n} brand-new accounts bought <b>{e(str(side))}</b> within {cfg.cluster_window_min} min: "
        f"${total:,.0f} total at about {paid * 100:.0f}¢",
        score_lines(score, why),
        *([x_line] if x_line else []),
        how_to(place, side, paid, m.get("slug"), title),
        CAUTION])


def how_to(place, side, price, slug, title):
    """Payout plus tap-by-tap steps to place the bet (on Polymarket US when `place` is set)."""
    e = html.escape
    payout = 100 / price if price else 0
    head = f"💵 About {price * 100:.0f}¢ a share, pays $1 if right ($100 → ${payout:,.0f})" if price else None
    if place:
        app, link, name = "the Polymarket US app (or polymarket.us)", us_link(place["event"]), place["event_title"]
        button = place["button"]
        # Single-market games: the market title just repeats the game, so skip "find ...".
        same = words(place["title"]) <= words(place["event_title"]) and not deadline(place["title"])
        market = "" if same else f"Find “{e(place['title'])}” and tap "
    else:
        app, link, name, button = "Polymarket", market_link(slug), title, side
        market = f"Find “{e(title)}” and tap "
    tap = f"{market}<b>{e(str(button))}</b>" if market else f"Tap <b>{e(str(button))}</b>"
    around = f" (around {price * 100:.0f}¢; the price there may differ a little)" if price else ""
    return "\n".join([
        *([head] if head else []),
        "📲 <b>How to place it:</b>",
        f"1. Open {app} and log in.",
        f'2. Tap <a href="{link}">{e(name)}</a> (or tap Search and type “{e(name)}”).',
        f"3. {tap}{around}.",
        "4. Type how much to spend, check what it pays if you win, then confirm."])


CAUTION = "<i>A hint, not a sure thing. Check the news first.</i>"


def explain_market(title, outcomes):
    """One plain line for sports bet types that are easy to misread."""
    spread = re.search(r"Spread:\s*(.+?)\s*\(-(\d+(?:\.\d+)?)\)", title or "")
    if spread:
        fav, need = spread.group(1), int(float(spread.group(2))) + 1
        other = other_outcome(outcomes, fav) if fav in [str(o) for o in outcomes] else "the other side"
        return f"ℹ️ {fav} must win by {need}+ points for “{fav}” to pay. “{other}” pays otherwise."
    total = re.search(r"O/U\s*(\d+(?:\.\d+)?)", title or "")
    if total:
        return f"ℹ️ Over/Under: will the total score be above or below {total.group(1)}?"
    return None


def follow_side(t, m):
    """Follow the trader: buying an outcome backs it, selling it backs the other side."""
    outcome, paid = str(t.get("outcome", "")), float(t.get("price", 0))
    if t.get("side") == "SELL":
        return other_outcome(m.get("outcomes", []), outcome), 1 - paid
    return outcome, paid


def format_trade_alert(t, m, usd, score, why, rec=None, place=None, x_line=None):
    e = html.escape
    outcome, paid = str(t.get("outcome", "")), float(t.get("price", 0))
    side, price = follow_side(t, m)
    if t.get("side") == "SELL":
        did = f"A trader just sold <b>{e(outcome)}</b>: ${usd:,.0f} at {paid * 100:.0f}¢ (betting against it)"
    else:
        did = f"A trader just bought <b>{e(outcome)}</b>: ${usd:,.0f} at {paid * 100:.0f}¢"
    title = t.get("title") or m.get("question", "Unknown market")
    wallet = t.get("proxyWallet", "")
    who = t.get("pseudonym") or t.get("name") or wallet[:10]
    hint = explain_market(title, m.get("outcomes", []))
    lines = [f"🚨 <b>{e(title)}</b>{category_suffix(m)}",
             *([e(hint)] if hint else []),
             did,
             f'👤 <a href="https://polymarket.com/profile/{e(wallet)}">{e(who)}</a>'
             + (f": {e(record_text(rec))}" if rec else ""),
             score_lines(score, why),
             *([x_line] if x_line else []),
             how_to(place, side, price, t.get("eventSlug") or m.get("slug"), title),
             CAUTION]
    return "\n".join(lines)


def price_move_side(m, move):
    """A jump backs the first outcome; a drop backs the other one."""
    first = m["outcomes"][0] if m["outcomes"] else "Yes"
    if move > 0:
        return first, m["price"]
    return other_outcome(m["outcomes"], first), 1 - m["price"]


def format_price_alert(m, move, cfg, score, why, place=None, x_line=None):
    e = html.escape
    arrow = "📈" if move > 0 else "📉"
    first = m["outcomes"][0] if m["outcomes"] else "Yes"
    side, price = price_move_side(m, move)
    before = m["price"] - move / 100
    verb = "jumped" if move > 0 else "dropped"
    return "\n".join([
        f"{arrow} <b>{e(m['question'])}</b>{category_suffix(m)}",
        f"“{e(str(first))}” {verb} from {before * 100:.0f}¢ to {m['price'] * 100:.0f}¢ "
        f"in the last {cfg.price_move_window_min} min",
        score_lines(score, why),
        *([x_line] if x_line else []),
        how_to(place, side, price, m["slug"], m["question"]),
        CAUTION])


# ---------------------------------------------------------------- scorecard

def log_alert(state, now, cid, title, slug, side, price, strength, cat=None, place=None, score=None):
    entry = {"t": now, "cid": cid, "title": title, "slug": slug, "side": str(side),
             "price": round(min(max(float(price), 0.01), 0.99), 4), "strength": strength,
             "cat": cat or "other"}
    if place:
        entry["us"] = place["event"]
    if score is not None:
        entry["score"] = score
    state.setdefault("scorecard", []).append(entry)


def resolve_scorecard(state, get=http_get):
    """Mark alerts won/lost once their market has settled."""
    open_ = [a for a in state.get("scorecard", []) if "result" not in a and a.get("cid")]
    ids = sorted({a["cid"] for a in open_})
    found = {}
    for i in range(0, len(ids), 20):
        for closed in ("true", "false"):
            params = [("condition_ids", c) for c in ids[i:i + 20]] + [("closed", closed), ("limit", 50)]
            for m in get(f"{GAMMA}/markets", params):
                found[m.get("conditionId")] = m
    for a in open_:
        m = found.get(a["cid"])
        if not m or not m.get("closed"):
            continue
        outcomes = [str(o) for o in as_list(m.get("outcomes"))]
        prices = [float(p) for p in as_list(m.get("outcomePrices"))]
        winners = [o for o, p in zip(outcomes, prices) if p >= 0.99]
        if winners:
            a["result"] = "won" if a["side"] in winners else "lost"
        elif time.time() - a["t"] > 60 * 86400:
            a["result"] = "void"


def paper_profit(done):
    """Profit from $100 on each settled signal at the price when the alert went out."""
    return sum(100 * (1 / a["price"] - 1) if a["result"] == "won" else -100 for a in done)


def money(x):
    return f"{'+' if x >= 0 else '-'}${abs(x):,.0f}"


def summary_text(state):
    card = state.get("scorecard", [])
    if not card:
        return "📊 <b>Scorecard</b>\nNo alerts yet. Quiet markets are normal; I'll keep watching."
    done = [a for a in card if a.get("result") in ("won", "lost")]
    won = [a for a in done if a["result"] == "won"]
    lines = ["📊 <b>Scorecard</b> (since " + time.strftime("%b %d", time.gmtime(card[0]["t"])) + ")"]
    if done:
        lines.append(f"Settled: {len(won)} won, {len(done) - len(won)} lost ({len(won) / len(done):.0%})")
        parts = []
        for key, label in (("strong", "🟢 score 70+"), ("medium", "🟡 45–69"), ("weak", "🟠 under 45")):
            k = [a for a in done if a["strength"] == key]
            if k:
                parts.append(f"{label} {sum(a['result'] == 'won' for a in k)}-{sum(a['result'] == 'lost' for a in k)}")
        lines.append(" · ".join(parts))
        lines.append(f"If you'd bet $100 on each: {money(paper_profit(done))}")
        cats = sorted({a.get("cat", "other") for a in done},
                      key=lambda c: -sum(a["result"] == "won" for a in done if a.get("cat", "other") == c)
                      / max(1, sum(1 for a in done if a.get("cat", "other") == c)))
        lines.append("\n<b>By category</b> (most accurate first)")
        for c in cats:
            k = [a for a in done if a.get("cat", "other") == c]
            w = sum(a["result"] == "won" for a in k)
            lines.append(f"{category_name(c)}: {w}-{len(k) - w} ({w / len(k):.0%}) · {money(paper_profit(k))}")
    else:
        lines.append("No alerts have settled yet. Markets can take days or weeks to resolve.")
    pending = len(card) - len(done) - sum(a.get("result") == "void" for a in card)
    lines.append(f"Waiting to settle: {pending} alert{'s' if pending != 1 else ''}")
    return "\n".join(lines)


# ---------------------------------------------------------------- telegram

def discover_chat_id(cfg, state, get=http_get):
    """Without TELEGRAM_CHAT_ID, use whoever last messaged the bot, and remember it."""
    if cfg.telegram_chat_id or not cfg.telegram_token:
        return
    if not state.get("chat_id"):
        res = get(f"https://api.telegram.org/bot{cfg.telegram_token}/getUpdates")
        for u in res.get("result", []):
            msg = u.get("message") or u.get("channel_post") or {}
            if msg.get("chat"):
                state["chat_id"] = msg["chat"]["id"]
        if state.get("chat_id"):
            cfg.telegram_chat_id = str(state["chat_id"])
            send_telegram(cfg, "✅ Polymarket alert bot is connected. You'll get crypto alerts here.")
            return
        print("No chat id yet: send any message to your bot in Telegram.", flush=True)
    cfg.telegram_chat_id = str(state.get("chat_id", ""))


def send_telegram(cfg, text, post=http_post):
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        print("[dry run, no Telegram settings]\n" + text + "\n", flush=True)
        return
    post(f"https://api.telegram.org/bot{cfg.telegram_token}/sendMessage",
         {"chat_id": cfg.telegram_chat_id, "text": text, "parse_mode": "HTML",
          "disable_web_page_preview": True})


def handle_commands(cfg, state, get=http_get):
    """Answer /score (and /help) sent to the bot from your own chat."""
    if not (cfg.telegram_token and cfg.telegram_chat_id):
        return
    res = get(f"https://api.telegram.org/bot{cfg.telegram_token}/getUpdates",
              {"offset": state.get("tg_offset", 0) + 1, "timeout": 0})
    for u in res.get("result", []):
        state["tg_offset"] = max(state.get("tg_offset", 0), u.get("update_id", 0))
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != str(cfg.telegram_chat_id):
            continue
        text = (msg.get("text") or "").strip().lower()
        if text.startswith("/score"):
            send_telegram(cfg, summary_text(state))
        elif text.startswith(("/help", "/start")):
            send_telegram(cfg, "Send /score to see how past alerts turned out. "
                               "I also send the scorecard automatically once a week.")


def find_chat_id(cfg):
    if not cfg.telegram_token:
        sys.exit("Set TELEGRAM_BOT_TOKEN first.")
    res = http_get(f"https://api.telegram.org/bot{cfg.telegram_token}/getUpdates")
    chats = {}
    for u in res.get("result", []):
        msg = u.get("message") or u.get("channel_post") or {}
        c = msg.get("chat")
        if c:
            chats[c["id"]] = c.get("title") or c.get("username") or c.get("first_name")
    if not chats:
        print("No messages yet. Send any message to your bot in Telegram, then run this again.")
    for cid, name in chats.items():
        print(f"TELEGRAM_CHAT_ID={cid}   ({name})")


# ---------------------------------------------------------------- main loop

def read_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, **kw):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, **kw)
    os.replace(tmp, path)


def load_state(cfg):
    state = read_json(cfg.state_file, {})
    # The scorecard is also kept in its own file (committed to the repo) so it survives
    # even if the rest of the state is lost; use whichever copy is longer.
    card = read_json(cfg.scorecard_file, [])
    if len(card) > len(state.get("scorecard", [])):
        state["scorecard"] = card
    return state


def save_state(cfg, state):
    write_json(cfg.state_file, state)
    write_json(cfg.scorecard_file, state.get("scorecard", []), indent=1)


def run_once(cfg, markets=None):
    """One round of checks. Pass `markets` to reuse a recent market list (skips price checks)."""
    state = load_state(cfg)
    discover_chat_id(cfg, state)
    now = int(time.time())
    try:
        handle_commands(cfg, state)
    except (urllib.error.URLError, TimeoutError, ValueError) as err:
        print(f"couldn't read Telegram commands: {err}", file=sys.stderr, flush=True)
    fresh = markets is None
    if fresh:
        markets = fetch_markets(cfg)
    min_usd = min(cfg.big_trade_usd, cfg.fresh_wallet_min_usd, cfg.winner_min_usd, cfg.cluster_min_usd)
    trades = fetch_trades(markets.keys(), min_usd, since=now - cfg.price_move_window_min * 60)
    us, catalog = None, None
    if cfg.us_only:
        catalog = us_catalog(cfg)
        us = lambda slug, question, side: match_us(catalog, slug, question, side)
    news = (lambda title: fetch_news(title, cfg.news_hours)) if cfg.news else None
    alerts = check_trades(cfg, trades, markets, state, now, us=us, news=news) + \
        check_split_bets(cfg, trades, markets, state, now, us=us, news=news)
    try:
        alerts += check_trader_posts(cfg, state, now, catalog if cfg.us_only else _us["catalog"])
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError) as err:
        print(f"couldn't check traders on X: {err}", file=sys.stderr, flush=True)
    if fresh:
        alerts += check_price_moves(cfg, markets, state, now, us=us, news=news)
        try:
            resolve_scorecard(state)
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            print(f"couldn't settle scorecard: {err}", file=sys.stderr, flush=True)
    last = state.setdefault("last_summary", now)
    if now - last >= cfg.summary_days * 86400:
        alerts.append(summary_text(state))
        state["last_summary"] = now
    for a in alerts:
        send_telegram(cfg, a)
    save_state(cfg, state)
    newest = min((now - int(t.get("timestamp", 0)) for t in trades), default=None)
    biggest = max((trade_usd(t) for t in trades), default=0)
    print(f"{time.strftime('%H:%M:%S')} {len(markets)} markets, {len(trades)} recent trades of "
          f"${min_usd:,.0f}+ (biggest ${biggest:,.0f}, newest {newest}s ago), {len(alerts)} alerts",
          flush=True)
    return markets


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one check and exit")
    ap.add_argument("--find-chat-id", action="store_true", help="show chat ids that messaged the bot")
    ap.add_argument("--minutes", type=float, default=0, help="keep checking for this long, then exit")
    ap.add_argument("--test-grok", metavar="TITLE", help="ask Grok about one market title and print the answer")
    ap.add_argument("--test-watch", action="store_true", help="show watched traders and their picks from the last day")
    args = ap.parse_args()
    cfg = Config()
    if args.test_watch:
        watch = build_watchlist(cfg)
        print(len(watch), "traders:", {h: trader_line(i) for h, i in watch.items()})
        state = {"watchlist": watch, "watchlist_at": time.time(), "watch_checked": time.time() - 24 * 3600}
        cfg.watch_every_min = 0
        for a in check_trader_posts(cfg, state, int(time.time()), fetch_us_catalog(), max_minutes=24 * 60, debug=True):
            print("----\n" + a)
        return None
    if args.test_grok:
        if not cfg.xai_key:
            sys.exit("XAI_API_KEY is not set")
        # Shape only (never the key itself), to spot a wrong paste.
        print(f"key: {len(cfg.xai_key)} characters, starts with 'xai-': {cfg.xai_key.startswith('xai-')}, "
              f"has spaces: {' ' in cfg.xai_key}")
        try:
            req = urllib.request.Request("https://api.x.ai/v1/models",
                                         headers={"Authorization": f"Bearer {cfg.xai_key}"})
            with urllib.request.urlopen(req, timeout=30) as r:
                print("models:", [m.get("id") for m in json.loads(r.read().decode()).get("data", [])])
        except urllib.error.HTTPError as err:
            print("models:", err, err.read().decode(errors="replace")[:300])
        print(grok_buzz(cfg, args.test_grok, "Yes", debug=True))
        return None
    if args.find_chat_id:
        return find_chat_id(cfg)
    if args.once:
        return run_once(cfg)
    stop = time.time() + args.minutes * 60 if args.minutes else float("inf")
    markets, markets_at = None, 0
    while time.time() < stop:
        try:
            # Refresh the market list (and run price checks) every MARKET_REFRESH_MIN.
            if time.time() - markets_at >= cfg.market_refresh_min * 60:
                markets, markets_at = run_once(cfg), time.time()
            else:
                run_once(cfg, markets)
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            print(f"check failed, retrying next round: {err}", file=sys.stderr, flush=True)
        time.sleep(max(0, min(cfg.poll_seconds, stop - time.time())))


if __name__ == "__main__":
    main()
