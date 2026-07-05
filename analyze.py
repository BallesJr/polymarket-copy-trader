# Skill-vs-luck metrics over harvested wallets — v2.
# Fixes vs v1: (1) realized PnL windowed to the SAME 30d as the leaderboard;
# (2) only complete round-trips count (|net shares bought-sold-redeemed-held| <= 1,
#     markets touched by SPLIT/MERGE/CONVERSION dropped as unverifiable);
# (3) truncation flag for wallets that hit the 12k-row harvest cap;
# (4) FIFA World Cup slugs (fifwc-*) classified as sports.

import json, os, math
from datetime import datetime, timezone, timedelta
from collections import defaultdict

ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
WINDOW_DAYS = 30
MAX_ROWS = 12000

def category(slug, title):
    s = (slug or "").lower() + " " + (title or "").lower()
    if "updown" in s or "up or down" in s or "up-or-down" in s: return "crypto-updown"
    if any(k in s for k in ("fifwc", "fifa", "world-cup", "nba", "wnba", "mlb", "nhl", "nfl", "ufc",
                             "tennis", "epl", "laliga", "serie-a", "bundesliga", "ucl", "f1", "-vs-",
                             "spread", "moneyline", "wimbledon", "boxing")): return "sports"
    if any(k in s for k in ("bitcoin", "btc", "ethereum", "eth-", "solana", "xrp", "doge", "crypto")): return "crypto"
    if any(k in s for k in ("temperature", "weather")): return "weather"
    if any(k in s for k in ("president", "election", "senate", "governor", "mayor", "primary",
                             "poll", "trump", "nominee", "impeach", "cabinet", "fed", "tariff")): return "politics"
    return "other"

def tstat(xs):
    n = len(xs)
    if n < 3: return None
    m = sum(xs) / n
    sd = math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))
    return m / (sd / math.sqrt(n)) if sd > 0 else None

def analyze_wallet(w):
    with open(os.path.join(DATA, "activity", f"{w}.json")) as f:
        acts = json.load(f)
    try:
        with open(os.path.join(DATA, "positions", f"{w}.json")) as f:
            poss = json.load(f)
    except FileNotFoundError:
        poss = []

    seen, rows = set(), []
    for a in acts:
        k = (a.get("transactionHash"), a.get("type"), a.get("asset"), a.get("side"),
             a.get("size"), a.get("timestamp"))
        if k in seen: continue
        seen.add(k)
        rows.append(a)

    truncated = len(acts) >= MAX_ROWS
    now = datetime.now(timezone.utc)
    w_start = (now - timedelta(days=WINDOW_DAYS)).timestamp()

    open_nonredeem = [p for p in poss if p.get("size", 0) > 1e-9 and not p.get("redeemable")]
    open_conds = set(p["conditionId"] for p in open_nonredeem)
    held_redeem = defaultdict(float)   # conditionId -> shares held awaiting redeem
    redeem_value = defaultdict(float)  # conditionId -> current (resolved) value of those shares
    for p in poss:
        if p.get("size", 0) > 1e-9 and p.get("redeemable"):
            held_redeem[p["conditionId"]] += p["size"]
            redeem_value[p["conditionId"]] += p.get("currentValue") or 0

    cash = defaultdict(float); shares = defaultdict(float)
    last_ts, first_ts_mkt, meta = {}, {}, {}
    complex_mkts = set()
    n_trades, buy_px_num, buy_px_den = 0, 0.0, 0.0
    ts_all = []
    for a in rows:
        c, t = a.get("conditionId"), a.get("type", "TRADE")
        if not c: continue
        ts_all.append(a["timestamp"])
        usd = a.get("usdcSize")
        if usd is None:
            usd = (a.get("price") or 0) * (a.get("size") or 0)
        sz = a.get("size") or 0
        if t == "TRADE":
            n_trades += 1
            if a.get("side") == "BUY":
                cash[c] -= usd; shares[c] += sz
                buy_px_num += (a.get("price") or 0) * sz; buy_px_den += sz
            else:
                cash[c] += usd; shares[c] -= sz
        elif t == "REDEEM":
            cash[c] += usd; shares[c] -= sz
        elif t in ("REWARD", "TAKER_REBATE"):
            cash[c] += usd
        else:  # SPLIT / MERGE / CONVERSION: share accounting not reconstructible here
            complex_mkts.add(c)
        last_ts[c] = max(last_ts.get(c, 0), a["timestamp"])
        first_ts_mkt[c] = min(first_ts_mkt.get(c, a["timestamp"]), a["timestamp"])
        meta[c] = (a.get("eventSlug") or a.get("slug"), a.get("title"))

    mkt_pnl, dropped_incomplete = {}, 0
    day_pnl, cat_pnl = defaultdict(float), defaultdict(float)
    for c, v in cash.items():
        if c in open_conds or c in complex_mkts:
            continue
        bal = shares[c] - held_redeem.get(c, 0.0)
        if abs(bal) > 1.0:            # missing legs (window edge or harvest cap)
            dropped_incomplete += 1
            continue
        if last_ts[c] < w_start:      # resolved before the leaderboard window
            continue
        pnl = v + redeem_value.get(c, 0.0)
        mkt_pnl[c] = pnl
        d = datetime.fromtimestamp(last_ts[c], timezone.utc).strftime("%Y-%m-%d")
        day_pnl[d] += pnl
        cat_pnl[category(*meta.get(c, (None, None)))] += pnl

    realized = sum(mkt_pnl.values())
    unrealized = sum(p.get("cashPnl") or 0 for p in open_nonredeem)
    days = sorted(day_pnl)
    series = [day_pnl[d] for d in days]
    pos_pnls = sorted((v for v in mkt_pnl.values() if v > 0), reverse=True)
    gross_pos = sum(pos_pnls)
    half = len(days) // 2
    h1, h2 = sum(series[:half]), sum(series[half:])
    span = (max(ts_all) - min(ts_all)) / 86400 if ts_all else 0

    return {
        "wallet": w, "truncated": truncated, "harvest_span_days": round(span, 1),
        "n_trades": n_trades, "n_markets_realized": len(mkt_pnl),
        "dropped_incomplete": dropped_incomplete, "n_complex": len(complex_mkts),
        "active_days": len(days),
        "realized_pnl_30d": round(realized, 2), "unrealized_pnl": round(unrealized, 2),
        "t_daily": round(tstat(series), 2) if tstat(series) is not None else None,
        "top1_share": round(pos_pnls[0] / gross_pos, 3) if pos_pnls and gross_pos > 0 else None,
        "top5_share": round(sum(pos_pnls[:5]) / gross_pos, 3) if pos_pnls and gross_pos > 0 else None,
        "h1_pnl": round(h1, 2), "h2_pnl": round(h2, 2),
        "consistent": (h1 > 0 and h2 > 0) if len(days) >= 6 else None,
        "trades_per_day": round(n_trades / max(span, 1), 1),
        "avg_buy_price": round(buy_px_num / buy_px_den, 3) if buy_px_den else None,
        "cat_pnl": {k: round(v, 2) for k, v in sorted(cat_pnl.items(), key=lambda x: -abs(x[1]))},
    }

def main():
    with open(os.path.join(DATA, "wallets.json")) as f:
        winfo = {x["wallet"]: x for x in json.load(f)["wallets"]}
    out = []
    for w, info in winfo.items():
        try:
            m = analyze_wallet(w)
        except FileNotFoundError:
            continue
        m.update({"cohort": info["cohort"], "lb_rank": info["lb_rank"],
                  "lb_pnl": info["lb_pnl"], "name": info["name"]})
        out.append(m)
    with open(os.path.join(DATA, "metrics.json"), "w") as f:
        json.dump(out, f, indent=1)

    lead = sorted([m for m in out if m["cohort"] == "leaderboard"], key=lambda m: m["lb_rank"] or 99)
    print(f"{'#':>3} {'name':16.16} {'lb_pnl':>8} {'real30d':>9} {'unreal':>8} {'mkts':>5} {'drop':>4} "
          f"{'days':>4} {'t':>6} {'top1':>5} {'cons':>5} {'tr/d':>6} {'trunc':>5} {'style':>13}")
    for m in lead:
        style = next(iter(m["cat_pnl"]), "-")
        print(f"{m['lb_rank']:>3} {(m['name'] or m['wallet'][:12]):16.16} {m['lb_pnl']:>8.0f} "
              f"{m['realized_pnl_30d']:>9.0f} {m['unrealized_pnl']:>8.0f} {m['n_markets_realized']:>5} "
              f"{m['dropped_incomplete']:>4} {m['active_days']:>4} {str(m['t_daily']):>6} "
              f"{(m['top1_share'] if m['top1_share'] is not None else float('nan')):>5.2f} "
              f"{str(m['consistent']):>5.5} {m['trades_per_day']:>6.1f} "
              f"{'YES' if m['truncated'] else '':>5} {style:>13}")

    ctrl = [m for m in out if m["cohort"] == "control"]
    if ctrl:
        r = sorted(m["realized_pnl_30d"] for m in ctrl)
        print(f"\nControl (n={len(ctrl)}): median {r[len(r)//2]:+.2f}, positive "
              f"{sum(1 for m in ctrl if m['realized_pnl_30d'] > 0)}/{len(ctrl)}, "
              f"worst {r[0]:+.0f}, best {r[-1]:+.0f}")

if __name__ == "__main__":
    main()
