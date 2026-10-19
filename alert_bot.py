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
import html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

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


def is_late(cfg, m, outcome_index, paid):
    """True when the price has already run LATE_MAX_POINTS past what the trader paid."""
    now_price = side_price_now(m, outcome_index)
    return now_price is not None and (now_price - paid) * 100 > cfg.late_max_points


def check_trades(cfg, trades, markets, state, now, wallet_count=None, record=None, us=None):
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
        reasons = []
        big = cfg.big_trade_usd_sports if m.get("cat") == "sports" else cfg.big_trade_usd
        if usd >= big:
            reasons.append(f"💰 Big bet: over ${big:,.0f} in one go")
        if usd >= cfg.fresh_wallet_min_usd and t.get("side") == "BUY":
            n = wallet_count(t["proxyWallet"])
            # The trade itself counts, so "prior" = n - 1.
            if n - 1 <= cfg.fresh_wallet_max_prior_trades:
                reasons.append(f"🆕 New account: only {max(n - 1, 0)} earlier trades on Polymarket")
        rec, winner = None, False
        if reasons or (usd >= cfg.winner_min_usd and t.get("side") == "BUY"):
            rec = record(t["proxyWallet"])
            winner = t.get("side") == "BUY" and usd >= cfg.winner_min_usd and is_proven_winner(cfg, rec)
            if winner:
                reasons.insert(0, "🏆 Proven winner (record below)")
        if reasons:
            strong = winner or len(reasons) > 1
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
            alerts.append(format_trade_alert(t, m, usd, reasons, rec, strong, place))
            log_alert(state, now, t.get("conditionId"), title, slug, side, price,
                      "strong" if strong else "medium", m.get("cat"), place)
    # Forget old entries so the state file stays small.
    for d in (seen, repeats):
        for k in [k for k, v in d.items() if now - v > 86400]:
            del d[k]
    return alerts


def check_split_bets(cfg, trades, markets, state, now, wallet_count=None, us=None):
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
        if len(fresh) < cfg.cluster_wallets or total < cfg.cluster_total_usd:
            continue
        cid, idx = key.split("|")
        m = markets.get(cid, {})
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
        alerts.append(format_split_alert(m, title, side, len(fresh), total, paid, cfg, place))
        log_alert(state, now, cid, title, m.get("slug", ""), side, paid, "strong", m.get("cat"), place)
    for k in [k for k, v in seen.items() if now - v > 86400]:
        del seen[k]
    for k in [k for k, v in cooldown.items() if now - v > 86400]:
        del cooldown[k]
    return alerts


def check_price_moves(cfg, markets, state, now, us=None):
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
        alerts.append(format_price_alert(m, move, cfg, place))
        log_alert(state, now, cid, m["question"], m["slug"], side, price, "weak", m.get("cat"), place)
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
                        r"\bset \d|\bround \d|first to|both teams|exact score|player|points|rebounds|assists",
                        re.I)
STOP_WORDS = {"will", "the", "a", "an", "of", "in", "on", "by", "be", "to", "for", "and", "or", "at", "is",
              "vs", "who", "what", "when", "which", "before", "after", "end", "than", "more", "less",
              "above", "below", "win", "winner", "election", "market"}
SYNONYMS = {"reach": "hit", "hits": "hit", "reaches": "hit", "btc": "bitcoin", "eth": "ethereum",
            "sol": "solana", "democrat": "democratic", "democrats": "democratic",
            "republicans": "republican", "gop": "republican", "us": "usa"}
MONTHS = {m: i + 1 for i, m in enumerate(
    ["january", "february", "march", "april", "may", "june", "july", "august", "september",
     "october", "november", "december"])}
_us = {"at": 0, "catalog": None}


def words(text):
    """Comparable words of a title: lower case, $150k -> 150000, synonyms merged."""
    t = (text or "").lower().replace(",", "")
    t = re.sub(r"(\d+(?:\.\d+)?)\s*([km])\b",
               lambda g: f"{float(g.group(1)) * (1000 if g.group(2) == 'k' else 1e6):.0f}", t)
    out = []
    for w in re.findall(r"[a-z]+|\d+(?:\.\d+)?", t):
        w = SYNONYMS.get(w, w)
        if w not in STOP_WORDS and w not in MONTHS:
            out.append(w)
    return set(out)


def key_numbers(ws):
    """Numbers that change what a market means (price levels, seat counts), not years or days."""
    return {w for w in ws if w[0].isdigit() and float(w) >= 100 and not 2000 <= float(w) <= 2100}


def deadline(text):
    """(year, month) a market ends by, if it says so; "before January 2027" -> (2026, 12)."""
    t = (text or "").lower()
    g = re.search(r"(before|by|in|end of|on)?\s*(" + "|".join(MONTHS) + r")\b[^0-9]{0,3}(\d{1,2})?,?\s*(20\d\d)", t)
    if not g:
        return None
    y, mo = int(g.group(4)), MONTHS[g.group(2)]
    if g.group(1) == "before":
        y, mo = (y - 1, 12) if mo == 1 else (y, mo - 1)
    return y, mo


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
                entry = {"event": ev.get("slug", ""), "event_title": ev.get("title", ""), "title": title,
                         "outcomes": [str(o) for o in as_list(m.get("outcomes"))],
                         "type": m.get("sportsMarketTypeV2") or m.get("sportsMarketType") or "",
                         "words": words(f'{ev.get("title", "")} {title}'),
                         "deadline": deadline(title) or deadline(ev.get("title", ""))}
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


def pick_side(entry, side, question):
    """The button to tap on a US market for this side, or None if it can't be placed there."""
    outs = {o.lower(): o for o in entry["outcomes"]}
    s = str(side).lower()
    if s in outs:
        return outs[s]
    # A US "Yes/No" market about one team or person: backing them on .com means Yes there.
    if "yes" in outs and s not in ("yes", "no") and words(side) and words(side) <= entry["words"]:
        return outs["yes"]
    return None


def match_us(catalog, event_slug, question, side):
    """Find where to place this bet on Polymarket US. Returns {"event", "event_title", "title",
    "button"} or None when Polymarket US doesn't offer it."""
    if not catalog:
        return None
    games = catalog["by_slug"].get(event_slug or "")
    if games is not None:
        # Same game on both sites (sports events share slugs). Only the winner market is listed,
        # so skip spreads, totals and other props.
        if PROP_WORDS.search(question or ""):
            return None
        candidates = games
    else:
        candidates = catalog["all"]
    q = words(question)
    nums, due = key_numbers(q), deadline(question)
    best, best_score, tie = None, 0, False
    for e in candidates:
        button = pick_side(e, side, question)
        if not button or (due and e["deadline"] and due != e["deadline"]):
            continue
        if games is not None:
            # Same event: the market whose title fits the question best (usually there's one).
            score = 1 + len(q & e["words"])
        else:
            shared = q & e["words"]
            if len(shared) < 2 or key_numbers(e["words"]) != nums:
                continue
            score = len(shared) / max(len(q), 1)
            if score < 0.6 or len(shared) / max(len(e["words"]), 1) < 0.3:
                continue
        if score > best_score:
            best, best_score, tie = dict(e, button=button), score, False
        elif score == best_score:
            tie = True
    # Two equally good fits (e.g. different deadlines): don't guess.
    return None if tie else best


def us_link(slug):
    return f"{US_SITE}/event/{slug}"

# ---------------------------------------------------------------- messages

def market_link(slug):
    return f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"


def other_outcome(outcomes, outcome):
    """The opposite side of a two-outcome market, e.g. Yes -> No."""
    rest = [o for o in outcomes if str(o) != str(outcome)]
    return rest[0] if len(outcomes) == 2 and rest else f"not {outcome}"


def category_suffix(m):
    return f" · {category_name(m['cat'])}" if m.get("cat") else ""


def format_split_alert(m, title, side, n, total, paid, cfg, place=None):
    e = html.escape
    return "\n".join([
        f"🧩 <b>{e(title)}</b>{category_suffix(m)}",
        f"{n} brand-new accounts bought <b>{e(str(side))}</b> within {cfg.cluster_window_min} min: "
        f"${total:,.0f} total at about {paid * 100:.0f}¢",
        "🕵️ Often one person splitting a big bet across new accounts to hide it",
        how_to(place, side, "🟢 strong", paid, m.get("slug"), title),
        CAUTION])


def how_to(place, side, strength, price, slug, title):
    """Signal strength plus tap-by-tap steps to place the bet (on Polymarket US when `place` is set)."""
    e = html.escape
    payout = 100 / price if price > 0 else 0
    head = (f"Signal strength: {strength}\n"
            f"💵 About {price * 100:.0f}¢ a share, pays $1 if right ($100 → ${payout:,.0f})")
    if place:
        app, link, name = "the Polymarket US app (or polymarket.us)", us_link(place["event"]), place["event_title"]
        button = place["button"]
        # Single-market games: the market title just repeats the game, so skip "find ...".
        same = words(place["title"]) <= words(place["event_title"]) or len(place["title"]) < 3
        market = "" if same else f"Find “{e(place['title'])}” and tap "
    else:
        app, link, name, button = "Polymarket", market_link(slug), title, side
        market = f"Find “{e(title)}” and tap "
    tap = f"{market}<b>{e(str(button))}</b>" if market else f"Tap <b>{e(str(button))}</b>"
    return "\n".join([
        head,
        "📲 <b>How to place it:</b>",
        f"1. Open {app} and log in.",
        f'2. Tap <a href="{link}">{e(name)}</a> (or tap Search and type “{e(name)}”).',
        f"3. {tap} (around {price * 100:.0f}¢; the price there may differ a little).",
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


def format_trade_alert(t, m, usd, reasons, rec=None, strong=False, place=None):
    e = html.escape
    outcome, paid = str(t.get("outcome", "")), float(t.get("price", 0))
    side, price = follow_side(t, m)
    if t.get("side") == "SELL":
        did = f"A trader just sold <b>{e(outcome)}</b>: ${usd:,.0f} at {paid * 100:.0f}¢ (betting against it)"
    else:
        did = f"A trader just bought <b>{e(outcome)}</b>: ${usd:,.0f} at {paid * 100:.0f}¢"
    strength = "🟢 strong" if strong else "🟡 medium"
    title = t.get("title") or m.get("question", "Unknown market")
    wallet = t.get("proxyWallet", "")
    who = t.get("pseudonym") or t.get("name") or wallet[:10]
    hint = explain_market(title, m.get("outcomes", []))
    lines = [f"🚨 <b>{e(title)}</b>{category_suffix(m)}",
             *([e(hint)] if hint else []),
             did,
             *[e(r) for r in reasons],
             f'👤 <a href="https://polymarket.com/profile/{e(wallet)}">{e(who)}</a>'
             + (f": {e(record_text(rec))}" if rec else ""),
             how_to(place, side, strength, price, t.get("eventSlug") or m.get("slug"), title),
             CAUTION]
    return "\n".join(lines)


def price_move_side(m, move):
    """A jump backs the first outcome; a drop backs the other one."""
    first = m["outcomes"][0] if m["outcomes"] else "Yes"
    if move > 0:
        return first, m["price"]
    return other_outcome(m["outcomes"], first), 1 - m["price"]


def format_price_alert(m, move, cfg, place=None):
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
        f"🤫 Quiet market: only ${m['volume24hr']:,.0f} traded in the last day, so someone may know something",
        how_to(place, side, "🟠 weak", price, m["slug"], m["question"]),
        CAUTION])


# ---------------------------------------------------------------- scorecard

def log_alert(state, now, cid, title, slug, side, price, strength, cat=None, place=None):
    entry = {"t": now, "cid": cid, "title": title, "slug": slug, "side": str(side),
             "price": round(min(max(float(price), 0.01), 0.99), 4), "strength": strength,
             "cat": cat or "other"}
    if place:
        entry["us"] = place["event"]
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
        for key, label in (("strong", "🟢 strong"), ("medium", "🟡 medium"), ("weak", "🟠 weak")):
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
    us = None
    if cfg.us_only:
        catalog = us_catalog(cfg)
        us = lambda slug, question, side: match_us(catalog, slug, question, side)
    alerts = check_trades(cfg, trades, markets, state, now, us=us) + \
        check_split_bets(cfg, trades, markets, state, now, us=us)
    if fresh:
        alerts += check_price_moves(cfg, markets, state, now, us=us)
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
    args = ap.parse_args()
    cfg = Config()
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
