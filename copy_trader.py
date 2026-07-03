# Paper copy-trader: mirror high-conviction positions of verified pro wallets.
#
# Entry:  leader's cumulative net cost in a token crosses CONVICTION_USD (aggregating
#         their micro-orders) -> we buy STAKE_USD walking the live CLOB asks up to
#         leader VWAP + MAX_CHASE; skip if even the best ask is past that (chase) or
#         the book can't fill the full stake within it (thin_book).
# Exit:   leader unwinds below EXIT_FRACTION of their max shares -> sell into the bids,
#         waiting if depth can't absorb us; or market resolves -> redeem at $1/$0.
# Every position records leader VWAP, our fill, and copy delay: the whole point of the
# experiment is measuring what a copier actually gets.
#
# Usage:  python copy_trader.py --once      (single cycle, for schedulers)
#         python copy_trader.py --loop 600  (poll every 600s)

import json, os, sys, time
from datetime import datetime, timezone
from collections import defaultdict
import requests

DATA_API = "https://data-api.polymarket.com"
CLOB = "https://clob.polymarket.com"
GAMMA = "https://gamma-api.polymarket.com"
H = {"User-Agent": "Mozilla/5.0"}

ROOT = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(ROOT, "data", "copy_state.json")

LEADERS = {
    "jtwyslljy": "0x9cb990f1862568a63d8601efeebe0304225c32f2",
    "0x53757615de": "0x53757615de1c42b83f893b79d4241a009dc2aeea",
}
CONVICTION_USD = 500.0   # leader net cost in a token before we mirror
STAKE_USD = 100.0        # our fixed paper stake per position
EXIT_FRACTION = 0.5      # leader below this share of their max -> we exit
MAX_CHASE = 0.15         # skip if ask exceeds leader VWAP by more than this
MIN_PRICE, MAX_PRICE = 0.03, 0.97
MAX_OPEN = 60
INITIAL_BANKROLL = 10000.0
FIRST_RUN_LOOKBACK_S = 6 * 3600

def now_ts():
    return datetime.now(timezone.utc).timestamp()

def iso(ts=None):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else datetime.now(timezone.utc).isoformat()

def load_state():
    if os.path.exists(STATE_PATH):
        with open(STATE_PATH) as f:
            return json.load(f)
    return {
        "created_at": iso(),
        "bankroll": INITIAL_BANKROLL,
        "last_seen": {},          # leader -> ts of newest processed trade
        "seen_keys": [],          # recent trade keys for dedupe across cycles
        "leader_pos": {},         # leader|asset -> {shares, cost, max_shares, meta...}
        "positions": [],          # our open paper positions
        "closed": [],             # our closed positions
        "skips": [],              # chase/liquidity skips (kept for analysis)
        "total_pnl": 0.0,
    }

def save_state(st):
    st["last_updated"] = iso()
    st["seen_keys"] = st["seen_keys"][-4000:]
    tmp = STATE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_PATH)

def fetch_new_trades(wallet, since_ts):
    """Newest-first pages until we pass since_ts. Returns oldest-first list."""
    rows, offset = [], 0
    while offset <= 3000:
        try:
            r = requests.get(f"{DATA_API}/trades", params={"user": wallet, "limit": 500, "offset": offset},
                             headers=H, timeout=20)
            r.raise_for_status()
            batch = r.json()
        except Exception as e:
            print(f"  [ERR] trades {wallet[:8]}: {e}")
            break
        if not batch:
            break
        rows.extend(batch)
        if min(t["timestamp"] for t in batch) <= since_ts or len(batch) < 500:
            break
        offset += 500
        time.sleep(0.1)
    rows = [t for t in rows if t["timestamp"] > since_ts]
    return sorted(rows, key=lambda t: t["timestamp"])

def get_books(token_ids):
    books = {}
    for i in range(0, len(token_ids), 50):
        try:
            r = requests.post(f"{CLOB}/books", json=[{"token_id": t} for t in token_ids[i:i+50]], timeout=20)
            r.raise_for_status()
            for b in r.json():
                books[b.get("asset_id")] = b
        except Exception as e:
            print(f"  [ERR] books: {e}")
        time.sleep(0.1)
    return books

def best(book, side):
    lvls = [(float(x["price"]), float(x["size"])) for x in (book or {}).get(side) or []]
    if not lvls:
        return None, 0.0
    return (min(lvls) if side == "asks" else max(lvls))

def sim_fill(book, side, budget=None, size=None, limit_px=None):
    """Marketable-order fill against real depth: buy asks until `budget` USD is
    spent, or sell `size` shares into bids. Returns (shares, usd) actually filled."""
    lvls = [(float(x["price"]), float(x["size"])) for x in (book or {}).get(side) or []]
    lvls.sort(reverse=(side == "bids"))
    shares = usd = 0.0
    for px, sz in lvls:
        if limit_px is not None and (px > limit_px if side == "asks" else px < limit_px):
            break
        take = min(sz, (budget - usd) / px) if side == "asks" else min(sz, size - shares)
        if take <= 0:
            break
        shares += take
        usd += take * px
    return shares, usd

def clob_winner(condition_id, asset):
    """Gamma omits resolved markets from condition_ids queries; CLOB still serves
    them per-condition with winner flags. None = not resolved yet / unknown."""
    try:
        r = requests.get(f"{CLOB}/markets/{condition_id}", timeout=20)
        r.raise_for_status()
        m = r.json()
    except Exception as e:
        print(f"  [ERR] clob market {condition_id[:10]}: {e}")
        return None
    toks = m.get("tokens") or []
    if not m.get("closed") or not any(t.get("winner") for t in toks):
        return None
    for t in toks:
        if t.get("token_id") == asset:
            return bool(t.get("winner"))
    return None

def resolve_positions(st):
    """Check gamma for closed markets; settle our positions at $1/$0."""
    conds = sorted(set(p["condition_id"] for p in st["positions"]))
    if not conds:
        return
    closed_info = {}
    for i in range(0, len(conds), 20):
        try:
            # gamma expects condition_ids as repeated params; comma-joined returns []
            r = requests.get(f"{GAMMA}/markets",
                             params={"condition_ids": conds[i:i+20]}, headers=H, timeout=20)
            r.raise_for_status()
            for m in r.json():
                closed_info[m.get("conditionId")] = m
        except Exception as e:
            print(f"  [ERR] gamma resolve: {e}")
        time.sleep(0.1)
    still_open = []
    for p in st["positions"]:
        m = closed_info.get(p["condition_id"])
        won = None
        if m and m.get("closed"):
            try:
                tokens = json.loads(m.get("clobTokenIds", "[]"))
                prices = [float(x) for x in json.loads(m.get("outcomePrices", "[]"))]
                idx = tokens.index(p["asset"])
                won = prices[idx] > 0.5
            except (ValueError, IndexError, json.JSONDecodeError):
                won = None
        if won is None and (m is None or m.get("closed")):
            won = clob_winner(p["condition_id"], p["asset"])
            time.sleep(0.1)
        if won is None:
            still_open.append(p)
            continue
        payout = p["shares"] * (1.0 if won else 0.0)
        p.update(status="won" if won else "lost", closed_at=iso(),
                 exit_price=1.0 if won else 0.0, pnl=round(payout - p["stake"], 4),
                 close_reason="resolved")
        st["bankroll"] += payout
        st["total_pnl"] += p["pnl"]
        st["closed"].append(p)
        print(f"  [RESOLVED {'WIN' if won else 'LOSS'}] {p['title'][:50]} ({p['outcome']}) pnl {p['pnl']:+.2f}")
    st["positions"] = still_open

def cycle(st):
    t0 = now_ts()
    seen = set(tuple(k) for k in st["seen_keys"])
    new_by_leader = {}
    for name, wallet in LEADERS.items():
        since = st["last_seen"].get(name, t0 - FIRST_RUN_LOOKBACK_S)
        trades = fetch_new_trades(wallet, since)
        fresh = []
        for t in trades:
            k = (t.get("transactionHash"), t.get("asset"), t.get("side"), t.get("size"), t.get("timestamp"))
            if k in seen:
                continue
            seen.add(k)
            st["seen_keys"].append(list(k))
            fresh.append(t)
        if trades:
            st["last_seen"][name] = max(t["timestamp"] for t in trades)
        elif name not in st["last_seen"]:
            st["last_seen"][name] = t0
        new_by_leader[name] = fresh
        print(f"  {name}: {len(fresh)} trades nous")

    # update leader aggregate positions
    candidates = set()
    for name, trades in new_by_leader.items():
        for t in trades:
            key = f"{name}|{t['asset']}"
            lp = st["leader_pos"].setdefault(key, {
                "leader": name, "asset": t["asset"], "condition_id": t.get("conditionId"),
                "title": t.get("title", ""), "outcome": t.get("outcome", ""),
                "slug": t.get("eventSlug") or t.get("slug", ""),
                "shares": 0.0, "cost": 0.0, "max_shares": 0.0, "last_trade_ts": 0,
            })
            usd = t.get("usdcSize") or (t.get("price") or 0) * (t.get("size") or 0)
            if t.get("side") == "BUY":
                if lp["shares"] <= 0:
                    # nou episodi: fora restes (cost residual d'un episodi tancat,
                    # shares negatius per inventari previ al tracking)
                    lp["shares"], lp["cost"], lp["max_shares"] = 0.0, 0.0, 0.0
                lp["shares"] += t.get("size") or 0
                lp["cost"] += usd
            else:
                lp["shares"] -= t.get("size") or 0
                lp["cost"] -= usd
            lp["max_shares"] = max(lp["max_shares"], lp["shares"])
            lp["last_trade_ts"] = max(lp["last_trade_ts"], t["timestamp"])
            candidates.add(key)

    held_assets = set(p["asset"] for p in st["positions"])

    # exits first: leader unwinding a token we hold
    exit_keys = [k for k, lp in st["leader_pos"].items()
                 if lp["asset"] in held_assets and lp["max_shares"] > 0
                 and lp["shares"] < EXIT_FRACTION * lp["max_shares"]]
    exit_assets = set(st["leader_pos"][k]["asset"] for k in exit_keys)

    # entries: conviction crossed, not held, leader still building
    entry_keys = []
    for k in candidates:
        lp = st["leader_pos"][k]
        if (lp["asset"] not in held_assets and lp["cost"] >= CONVICTION_USD
                and lp["shares"] >= EXIT_FRACTION * lp["max_shares"]
                and len(st["positions"]) + len(entry_keys) < MAX_OPEN
                and st["bankroll"] >= STAKE_USD):
            entry_keys.append(k)

    need_books = list(exit_assets | set(st["leader_pos"][k]["asset"] for k in entry_keys))
    books = get_books(need_books) if need_books else {}

    for asset in exit_assets:
        for p in list(st["positions"]):
            if p["asset"] != asset:
                continue
            sold, proceeds = sim_fill(books.get(asset), "bids", size=p["shares"])
            if sold < p["shares"] * 0.999:
                print(f"  [WARN] bids insuficients per sortir de {p['title'][:40]} "
                      f"({sold:.0f}/{p['shares']:.0f}), espero")
                continue
            bid = round(proceeds / sold, 4)
            p.update(status="closed", closed_at=iso(), exit_price=bid,
                     pnl=round(proceeds - p["stake"], 4), close_reason="leader_exit")
            st["bankroll"] += proceeds
            st["total_pnl"] += p["pnl"]
            st["closed"].append(p)
            st["positions"].remove(p)
            print(f"  [EXIT líder] {p['title'][:50]} @ {bid} pnl {p['pnl']:+.2f}")

    for k in entry_keys:
        lp = st["leader_pos"][k]
        ask, ask_sz = best(books.get(lp["asset"]), "asks")
        vwap = lp["cost"] / lp["shares"] if lp["shares"] > 0 else None
        if ask is None or not (MIN_PRICE <= ask <= MAX_PRICE):
            st["skips"].append({"at": iso(), "reason": "no_book_or_extreme", "ask": ask, **{x: lp[x] for x in ("leader", "title", "outcome")}})
            continue
        limit_px = min(MAX_PRICE, vwap + MAX_CHASE) if vwap is not None else MAX_PRICE
        shares, cost = sim_fill(books.get(lp["asset"]), "asks", budget=STAKE_USD, limit_px=limit_px)
        if cost < STAKE_USD * 0.999:
            reason = "chase" if shares <= 0 else "thin_book"
            st["skips"].append({"at": iso(), "reason": reason, "ask": ask, "fillable_usd": round(cost, 2),
                                "leader_vwap": round(vwap, 4) if vwap is not None else None,
                                **{x: lp[x] for x in ("leader", "title", "outcome")}})
            vw = f"{vwap:.3f}" if vwap is not None else "?"
            print(f"  [SKIP {reason}] {lp['title'][:45]} ask {ask} fillable ${cost:.0f} vs vwap {vw}")
            continue
        fill_px = STAKE_USD / shares
        st["positions"].append({
            "leader": lp["leader"], "asset": lp["asset"], "condition_id": lp["condition_id"],
            "title": lp["title"], "outcome": lp["outcome"], "slug": lp["slug"],
            "opened_at": iso(), "entry_price": round(fill_px, 4), "shares": round(shares, 4),
            "stake": STAKE_USD, "leader_vwap": round(vwap, 4) if vwap else None,
            "leader_cost": round(lp["cost"], 2),
            "copy_delay_min": round((t0 - lp["last_trade_ts"]) / 60, 1),
            "best_ask": ask, "ask_depth": ask_sz, "status": "open",
        })
        st["bankroll"] -= STAKE_USD
        print(f"  [COPY] {lp['leader']} {lp['title'][:45]} ({lp['outcome']}) @ {fill_px:.4f} "
              f"(millor ask {ask}, vwap líder {vwap:.3f}, retard {(t0 - lp['last_trade_ts'])/60:.0f}m)")

    resolve_positions(st)

    open_val = sum(p["stake"] for p in st["positions"])
    print(f"[CYCLE] bankroll ${st['bankroll']:.2f} | {len(st['positions'])} obertes (${open_val:.0f}) | "
          f"{len(st['closed'])} tancades | PnL total ${st['total_pnl']:+.2f}")

def main():
    if "--logfile" in sys.argv:
        lf = open(os.path.join(ROOT, "data", "copy_log.txt"), "a", encoding="utf-8", buffering=1)
        sys.stdout = sys.stderr = lf
        print(f"\n=== {iso()} (scheduled) ===")

    # guard against overlapping scheduled runs
    lock = os.path.join(ROOT, "data", "copy.lock")
    if os.path.exists(lock) and now_ts() - os.path.getmtime(lock) < 300:
        print("[LOCK] cicle anterior encara actiu, surto")
        return
    with open(lock, "w") as f:
        f.write(str(os.getpid()))
    st = load_state()
    if "--loop" in sys.argv:
        i = sys.argv.index("--loop")
        period = int(sys.argv[i + 1]) if len(sys.argv) > i + 1 else 600
        while True:
            print(f"\n=== {iso()} ===")
            try:
                cycle(st)
            except Exception as e:
                print(f"[CYCLE ERR] {e}")
            save_state(st)
            time.sleep(period)
    else:
        try:
            cycle(st)
        finally:
            save_state(st)
            try:
                os.remove(lock)
            except OSError:
                pass

if __name__ == "__main__":
    main()
