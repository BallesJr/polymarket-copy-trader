# Long-window verification + copyability profile for deep-harvested candidates.
# Per wallet:
#   - realized PnL per market (cash-flow, complete round-trips only), 180d window
#   - monthly PnL matrix + daily t-stat overall AND excluding June-2026 (World Cup month):
#     if the edge only exists in June, copying it after the Cup buys nothing
#   - copyability: PnL-weighted median hours from wallet's first entry in a market to its
#     last cash flow (reaction window for a copier), edge per share bought, and PnL under
#     1/2/3 cents of entry+exit slippage per share
import json, os, math
from datetime import datetime, timezone
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
DEEP = os.path.join(DATA, "deep")

def tstat(xs):
    n = len(xs)
    if n < 3: return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd > 0 else None

def load_positions(w):
    try:
        with open(os.path.join(DATA, "positions", f"{w}.json")) as f:
            return json.load(f)
    except FileNotFoundError:
        return []

def analyze(w):
    with open(os.path.join(DEEP, f"{w}.json")) as f:
        acts = json.load(f)
    poss = load_positions(w)
    seen, rows = set(), []
    for a in acts:
        k = (a.get("transactionHash"), a.get("type"), a.get("asset"), a.get("side"),
             a.get("size"), a.get("timestamp"))
        if k in seen: continue
        seen.add(k)
        rows.append(a)

    open_conds = set(p["conditionId"] for p in poss if p.get("size", 0) > 1e-9 and not p.get("redeemable"))
    held_redeem, redeem_value = defaultdict(float), defaultdict(float)
    for p in poss:
        if p.get("size", 0) > 1e-9 and p.get("redeemable"):
            held_redeem[p["conditionId"]] += p["size"]
            redeem_value[p["conditionId"]] += p.get("currentValue") or 0

    cash = defaultdict(float); shares = defaultdict(float)
    bought = defaultdict(float); sold = defaultdict(float)
    first_ts, last_ts = {}, {}
    complex_mkts, n_trades = set(), 0
    for a in rows:
        c, t = a.get("conditionId"), a.get("type", "TRADE")
        if not c: continue
        usd = a.get("usdcSize")
        if usd is None:
            usd = (a.get("price") or 0) * (a.get("size") or 0)
        sz = a.get("size") or 0
        if t == "TRADE":
            n_trades += 1
            if a.get("side") == "BUY":
                cash[c] -= usd; shares[c] += sz; bought[c] += sz
            else:
                cash[c] += usd; shares[c] -= sz; sold[c] += sz
        elif t == "REDEEM":
            cash[c] += usd; shares[c] -= sz
        elif t in ("REWARD", "TAKER_REBATE"):
            cash[c] += usd
        else:
            complex_mkts.add(c)
        first_ts[c] = min(first_ts.get(c, a["timestamp"]), a["timestamp"])
        last_ts[c] = max(last_ts.get(c, 0), a["timestamp"])

    mkt = {}
    for c, v in cash.items():
        if c in open_conds or c in complex_mkts: continue
        if abs(shares[c] - held_redeem.get(c, 0.0)) > 1.0: continue
        mkt[c] = {"pnl": v + redeem_value.get(c, 0.0), "bought": bought[c], "sold": sold[c],
                  "hours": (last_ts[c] - first_ts[c]) / 3600,
                  "day": datetime.fromtimestamp(last_ts[c], timezone.utc).strftime("%Y-%m-%d")}

    day_pnl = defaultdict(float); month_pnl = defaultdict(float)
    for m in mkt.values():
        day_pnl[m["day"]] += m["pnl"]
        month_pnl[m["day"][:7]] += m["pnl"]
    days = sorted(day_pnl)
    series = [day_pnl[d] for d in days]
    series_nojune = [day_pnl[d] for d in days if not d.startswith("2026-06") and not d.startswith("2026-07")]

    total = sum(m["pnl"] for m in mkt.values())
    tot_bought = sum(m["bought"] for m in mkt.values())
    tot_traded = sum(m["bought"] + m["sold"] for m in mkt.values())
    # PnL-weighted median of per-market duration (weight = |pnl|)
    dur = sorted((m["hours"], abs(m["pnl"])) for m in mkt.values())
    wsum, acc, med_h = sum(x[1] for x in dur) or 1, 0, None
    for h, wgt in dur:
        acc += wgt
        if acc >= wsum / 2:
            med_h = h
            break

    span = (max(last_ts.values()) - min(first_ts.values())) / 86400 if last_ts else 0
    return {
        "wallet": w, "span_days": round(span), "n_trades": n_trades, "n_mkts": len(mkt),
        "active_days": len(days), "pnl": round(total),
        "t_all": round(tstat(series), 2) if tstat(series) else None,
        "t_preWC": round(tstat(series_nojune), 2) if tstat(series_nojune) else None,
        "pnl_preWC": round(sum(series_nojune)),
        "days_preWC": len(series_nojune),
        "monthly": {k: round(v) for k, v in sorted(month_pnl.items())},
        "edge_per_share": round(total / tot_bought, 4) if tot_bought else None,
        "median_hold_h": round(med_h, 1) if med_h is not None else None,
        "slip": {f"{c}c": round(total - c / 100 * tot_traded) for c in (1, 2, 3)},
        "trades_per_day": round(n_trades / max(span, 1), 1),
    }

def main():
    with open(os.path.join(DATA, "metrics.json")) as f:
        metrics = json.load(f)
    n2w = {m["name"] or m["wallet"][:12]: m["wallet"] for m in metrics if m["cohort"] == "leaderboard"}
    for name, w in n2w.items():
        if not os.path.exists(os.path.join(DEEP, f"{w}.json")):
            continue
        r = analyze(w)
        print(f"\n=== {name} ({w[:10]}) — span {r['span_days']}d, {r['n_trades']} trades, "
              f"{r['n_mkts']} mercats, {r['trades_per_day']}/dia")
        print(f"  PnL 180d: ${r['pnl']:+,}  | t_all={r['t_all']}  | "
              f"PRE-Mundial: ${r['pnl_preWC']:+,} en {r['days_preWC']} dies (t={r['t_preWC']})")
        print(f"  mensual: {r['monthly']}")
        print(f"  copiabilitat: hold medià (pnl-pond.) {r['median_hold_h']}h | "
              f"edge/share {r['edge_per_share']} | slippage 1/2/3c: "
              f"{r['slip']['1c']:+,} / {r['slip']['2c']:+,} / {r['slip']['3c']:+,}")

if __name__ == "__main__":
    main()
