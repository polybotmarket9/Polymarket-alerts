import sys, time
sys.path.insert(0, ".")
import alert_bot as ab

cfg = ab.Config()
seen, odds, rate, total = set(), 0, 0, 0
for t in ab.http_get(f"{ab.DATA}/trades", {"limit": 1000, "filterType": "CASH", "filterAmount": 10000}):
    w = t["proxyWallet"]
    if w in seen or t.get("side") != "BUY":
        continue
    seen.add(w)
    b = ab.bet_record(w, 90)
    if b["n"] < cfg.win_min_bets:
        print(f"{w[:10]} too few bets ({b['n']})", flush=True)
        continue
    total += 1
    cfg.trader_filter = "odds"; o = ab.passes_trader_filter(cfg, b)
    cfg.trader_filter = "winrate"; r = ab.passes_trader_filter(cfg, b)
    odds += o; rate += r
    print(f"{w[:10]} n={b['n']} win={b['wins'] / b['n']:.0%} avg={b['avg_price']:.2f} odds={o} 80%={r}", flush=True)
    if len(seen) >= 60:
        break
print(f"SUMMARY traders with enough bets: {total}; beat-the-odds pass: {odds}; 80% pass: {rate}")
