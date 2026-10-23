import sys
sys.path.insert(0, ".")
import alert_bot as ab

cfg = ab.Config()
seen, n, fresh, winners, losers = set(), 0, 0, 0, 0
for t in ab.http_get(f"{ab.DATA}/trades", {"limit": 1000, "filterType": "CASH", "filterAmount": 10000}):
    w = t["proxyWallet"]
    if w in seen or t.get("side") != "BUY":
        continue
    seen.add(w)
    n += 1
    if ab.wallet_trade_count(w, cfg.fresh_wallet_max_prior_trades + 2) - 1 <= cfg.fresh_wallet_max_prior_trades:
        fresh += 1
        continue
    rec = ab.trader_record(w)
    winners += ab.is_proven_winner(cfg, rec)
    losers += rec["all"]["profit"] < 0
    if n >= 80:
        break
print(f"SUMMARY big buyers: {n}; new accounts: {fresh}; proven winners: {winners}; down overall: {losers}; "
      f"other: {n - fresh - winners - losers}")
