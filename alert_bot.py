#!/usr/bin/env python3
"""Polymarket crypto alert bot.

Watches public Polymarket data (Gamma API for markets, Data API for trades)
and sends Telegram alerts when trading looks like it
might be informed:

  1. BIG TRADE     - one trade worth at least BIG_TRADE_USD.
  2. FRESH WALLET  - a wallet with almost no trading history places a sizeable bet.
  3. PRICE JUMP    - a market's price moves a lot within an hour while it had
                     little trading volume before.

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
        self.tag = os.environ.get("MARKET_TAG", "crypto")
        # Skip very short-term "up or down in 15 minutes" style markets; they are
        # dominated by bots and would flood you with noise.
        self.exclude = [k.strip().lower() for k in
                        os.environ.get("EXCLUDE_KEYWORDS", "up or down").split(",") if k.strip()]
        self.poll_seconds = env_int("POLL_SECONDS", 60)
        self.big_trade_usd = env_float("BIG_TRADE_USD", 10000)
        self.fresh_wallet_min_usd = env_float("FRESH_WALLET_MIN_USD", 2000)
        self.fresh_wallet_max_prior_trades = env_int("FRESH_WALLET_MAX_PRIOR_TRADES", 5)
        self.price_move_points = env_float("PRICE_MOVE_POINTS", 10)  # 10 = 10 cents = 10%
        self.price_move_window_min = env_int("PRICE_MOVE_WINDOW_MIN", 60)
        self.price_move_max_volume_24h = env_float("PRICE_MOVE_MAX_VOLUME_24H", 100000)
        # Markets with almost no trading "move" on stale quotes alone; ignore those.
        self.price_move_min_volume_24h = env_float("PRICE_MOVE_MIN_VOLUME_24H", 1000)
        self.max_markets = env_int("MAX_MARKETS", 300)
        self.state_file = os.environ.get(
            "STATE_FILE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "state.json"))


# ---------------------------------------------------------------- HTTP helpers

def http_get(url, params=None, timeout=20):
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "polymarket-alert-bot/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


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


def fetch_markets(cfg, get=http_get):
    """Active markets under the configured tag, keyed by conditionId."""
    markets = {}
    offset = 0
    while len(markets) < cfg.max_markets:
        events = get(f"{GAMMA}/events", {"tag_slug": cfg.tag, "active": "true", "closed": "false",
                                         "limit": 100, "offset": offset,
                                         "order": "volume24hr", "ascending": "false"})
        if not events:
            break
        for ev in events:
            for m in ev.get("markets", []):
                if m.get("closed") or not m.get("conditionId"):
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
                }
        if len(events) < 100:
            break
        offset += 100
    return markets


def fetch_trades(condition_ids, min_usd, get=http_get):
    """Recent trades of at least min_usd in the given markets, newest first."""
    trades = []
    ids = list(condition_ids)
    for i in range(0, len(ids), 40):
        trades += get(f"{DATA}/trades", {"market": ",".join(ids[i:i + 40]), "limit": 500,
                                         "takerOnly": "true", "filterType": "CASH",
                                         "filterAmount": int(min_usd)})
    return trades


def wallet_trade_count(wallet, limit, get=http_get):
    """How many trades this wallet has ever made, capped at limit."""
    return len(get(f"{DATA}/trades", {"user": wallet, "limit": limit}))


# ---------------------------------------------------------------- rules

def trade_usd(t):
    return float(t.get("size", 0)) * float(t.get("price", 0))


def trade_id(t):
    return f'{t.get("transactionHash")}:{t.get("asset")}:{t.get("proxyWallet")}:{t.get("size")}'


def check_trades(cfg, trades, markets, state, now, wallet_count=None):
    """Return a list of alert messages for trades not seen before."""
    wallet_count = wallet_count or (lambda w: wallet_trade_count(w, cfg.fresh_wallet_max_prior_trades + 2))
    seen = state.setdefault("seen_trades", {})
    alerts = []
    for t in trades:
        tid = trade_id(t)
        if tid in seen:
            continue
        seen[tid] = now
        # Ignore trades older than the watch window (e.g. on the very first run).
        if now - int(t.get("timestamp", now)) > cfg.price_move_window_min * 60:
            continue
        usd = trade_usd(t)
        m = markets.get(t.get("conditionId"), {})
        reasons = []
        if usd >= cfg.big_trade_usd:
            reasons.append(f"Big trade: ${usd:,.0f} in one go (alert level ${cfg.big_trade_usd:,.0f})")
        if usd >= cfg.fresh_wallet_min_usd and t.get("side") == "BUY":
            n = wallet_count(t["proxyWallet"])
            # The trade itself counts, so "prior" = n - 1.
            if n - 1 <= cfg.fresh_wallet_max_prior_trades:
                reasons.append(f"Fresh wallet: only {max(n - 1, 0)} earlier trades on Polymarket")
        if reasons:
            alerts.append(format_trade_alert(t, m, usd, reasons))
    # Forget trade ids after a day so the state file stays small.
    for k in [k for k, v in seen.items() if now - v > 86400]:
        del seen[k]
    return alerts


def check_price_moves(cfg, markets, state, now):
    """Alert when a low-volume market's price moves PRICE_MOVE_POINTS within the window."""
    history = state.setdefault("prices", {})
    cooldown = state.setdefault("price_alerted", {})
    window = cfg.price_move_window_min * 60
    alerts = []
    for cid, m in markets.items():
        if m["price"] is None:
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
        alerts.append(format_price_alert(m, move, cfg))
    for cid in [c for c in history if c not in markets]:
        del history[cid]
    for cid in [c for c, v in cooldown.items() if now - v > 86400]:
        del cooldown[cid]
    return alerts


# ---------------------------------------------------------------- messages

def market_link(slug):
    return f"https://polymarket.com/event/{slug}" if slug else "https://polymarket.com"


def other_outcome(outcomes, outcome):
    """The opposite side of a two-outcome market, e.g. Yes -> No."""
    rest = [o for o in outcomes if str(o) != str(outcome)]
    return rest[0] if len(outcomes) == 2 and rest else f"not {outcome}"


def signal_line(side, strength):
    return (f"👉 <b>Signal: bet {html.escape(str(side))}</b> · strength {strength}\n"
            "<i>A hint from public trades, not a sure thing. Check the news first.</i>")


def format_trade_alert(t, m, usd, reasons):
    e = html.escape
    # Follow the trader: buying an outcome backs it, selling it backs the other side.
    side = t.get("outcome", "")
    if t.get("side") == "SELL":
        side = other_outcome(m.get("outcomes", []), side)
    strength = "🟢 strong" if len(reasons) > 1 else "🟡 medium"
    title = t.get("title") or m.get("question", "Unknown market")
    wallet = t.get("proxyWallet", "")
    who = t.get("pseudonym") or t.get("name") or wallet[:10]
    lines = [f"🚨 <b>{e(title)}</b>",
             f'{e(t.get("side", ""))} {e(str(t.get("outcome", "")))} at {float(t.get("price", 0)) * 100:.0f}¢ '
             f'· ${usd:,.0f}',
             *[f"• {e(r)}" for r in reasons],
             signal_line(side, strength),
             f'Trader: <a href="https://polymarket.com/profile/{e(wallet)}">{e(who)}</a>',
             f'<a href="{market_link(t.get("eventSlug") or m.get("slug"))}">Open market</a>']
    return "\n".join(lines)


def format_price_alert(m, move, cfg):
    e = html.escape
    arrow = "📈" if move > 0 else "📉"
    first = m["outcomes"][0] if m["outcomes"] else "Yes"
    side = first if move > 0 else other_outcome(m["outcomes"], first)
    return "\n".join([
        f"{arrow} <b>{e(m['question'])}</b>",
        f"{e(str(first))} moved {move:+.0f} points in {cfg.price_move_window_min} min, now {m['price'] * 100:.0f}¢",
        f"• Quiet market: only ${m['volume24hr']:,.0f} traded in the last 24h",
        signal_line(side, "🟠 weak"),
        f'<a href="{market_link(m["slug"])}">Open market</a>'])


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

def load_state(path):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def save_state(path, state):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f)
    os.replace(tmp, path)


def run_once(cfg):
    state = load_state(cfg.state_file)
    discover_chat_id(cfg, state)
    now = int(time.time())
    markets = fetch_markets(cfg)
    min_usd = min(cfg.big_trade_usd, cfg.fresh_wallet_min_usd)
    trades = fetch_trades(markets.keys(), min_usd)
    alerts = check_trades(cfg, trades, markets, state, now) + check_price_moves(cfg, markets, state, now)
    for a in alerts:
        send_telegram(cfg, a)
    save_state(cfg.state_file, state)
    print(f"{time.strftime('%H:%M:%S')} checked {len(markets)} markets, {len(trades)} trades, "
          f"{len(alerts)} alerts", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--once", action="store_true", help="run one check and exit")
    ap.add_argument("--find-chat-id", action="store_true", help="show chat ids that messaged the bot")
    args = ap.parse_args()
    cfg = Config()
    if args.find_chat_id:
        return find_chat_id(cfg)
    if args.once:
        return run_once(cfg)
    while True:
        try:
            run_once(cfg)
        except (urllib.error.URLError, TimeoutError, ValueError) as err:
            print(f"check failed, retrying next round: {err}", file=sys.stderr, flush=True)
        time.sleep(cfg.poll_seconds)


if __name__ == "__main__":
    main()
