import sys
sys.path.insert(0, ".")
from alert_bot import http_get, DATA

wallets = []
for period in ("month", "all"):
    for r in http_get(f"{DATA}/v1/leaderboard", {"timePeriod": period, "orderBy": "PNL", "limit": 8}):
        wallets.append(("top-" + period, r.get("proxyWallet"), r.get("pnl")))
for t in http_get(f"{DATA}/trades", {"limit": 500, "filterType": "CASH", "filterAmount": 3000}):
    if len(wallets) >= 30:
        break
    if t["proxyWallet"] not in [w[1] for w in wallets]:
        wallets.append(("big-trade", t["proxyWallet"], None))

for kind, w, pnl in wallets:
    closed = []
    for off in (0, 50):
        rows = http_get(f"{DATA}/closed-positions", {"user": w, "limit": 50, "offset": off,
                                                     "sortBy": "TIMESTAMP", "sortDirection": "DESC"})
        closed += rows
        if len(rows) < 50:
            break
    red = http_get(f"{DATA}/positions", {"user": w, "redeemable": "true", "limit": 100, "sizeThreshold": 0})
    lost_unredeemed = [p for p in red if float(p.get("curPrice", 1)) == 0]
    won_unredeemed = [p for p in red if float(p.get("curPrice", 0)) == 1]
    wins = sum(float(p["realizedPnl"]) > 0 for p in closed)
    ts = [p["timestamp"] for p in closed]
    order_ok = ts == sorted(ts, reverse=True)
    n = len(closed) + len(red)
    full = (wins + len(won_unredeemed)) / n if n else None
    avgp = sum(float(p["avgPrice"]) for p in closed) / len(closed) if closed else 0
    print(f"{kind:10} {w[:10]} pnl={pnl} closed={len(closed)} closedWin={wins} "
          f"redeemable lost={len(lost_unredeemed)} won={len(won_unredeemed)} "
          f"winrate_closed={wins/len(closed) if closed else None:} winrate_full={full} avgPrice={avgp:.2f} sorted={order_ok}",
          flush=True)
