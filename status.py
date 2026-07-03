# Quick portfolio status: open positions marked to live books + recent closes.
# Usage: python status.py

import json, os
import requests

from copy_trader import clob_winner

ROOT = os.path.dirname(os.path.abspath(__file__))
H = {"User-Agent": "Mozilla/5.0"}

def main():
    with open(os.path.join(ROOT, "data", "copy_state.json")) as f:
        st = json.load(f)
    pos = st["positions"]
    print(f"Bankroll: ${st['bankroll']:.2f} | PnL realitzat: ${st['total_pnl']:+.2f} | "
          f"{len(pos)} obertes | {len(st['closed'])} tancades | {len(st['skips'])} skips")
    if not pos:
        return

    books = {}
    try:
        r = requests.post("https://clob.polymarket.com/books",
                          json=[{"token_id": p["asset"]} for p in pos], timeout=20)
        books = {b["asset_id"]: b for b in r.json()}
    except Exception as e:
        print(f"[books err: {e}]")
    closed_info = {}
    try:
        r = requests.get("https://gamma-api.polymarket.com/markets",
                         params={"condition_ids": [p["condition_id"] for p in pos]},
                         headers=H, timeout=20)
        closed_info = {m["conditionId"]: m for m in r.json()}
    except Exception as e:
        print(f"[gamma err: {e}]")

    print()
    tot_mark = 0.0
    for p in pos:
        bids = [(float(x["price"]), float(x["size"])) for x in (books.get(p["asset"]) or {}).get("bids") or []]
        bid = max(bids)[0] if bids else None
        m = closed_info.get(p["condition_id"], {})
        cur = bid
        state = "viu"
        try:
            toks = json.loads(m.get("clobTokenIds", "[]"))
            prices = [float(x) for x in json.loads(m.get("outcomePrices", "[]"))]
            gp = prices[toks.index(p["asset"])]
            if m.get("closed"):
                state, cur = "TANCAT", gp
            elif bid is None:
                state, cur = "sense book", gp
        except (ValueError, IndexError, json.JSONDecodeError):
            pass
        if cur is None and not m:
            won = clob_winner(p["condition_id"], p["asset"])
            if won is not None:
                state, cur = ("GUANYADA" if won else "PERDUDA"), (1.0 if won else 0.0)
        if cur is None:
            # sense book ni resolució: no assumim valor de cost, marquem a 0
            state, cur = "sense dades", 0.0
        mark = p["shares"] * cur
        tot_mark += mark
        d = mark - p["stake"]
        print(f"  {p['title'][:50]:50s} {p['outcome'][:14]:14s} @ {p['entry_price']:<6} "
              f"ara {cur:<7} {d:+7.2f}  [{state}] ({p['leader']})")
    print(f"\n  En joc: ${sum(p['stake'] for p in pos):.0f} | Mark: ${tot_mark:.2f} | "
          f"Unrealized: ${tot_mark - sum(p['stake'] for p in pos):+.2f}")

    if st["closed"]:
        print("\nÚltimes tancades:")
        for p in st["closed"][-5:]:
            print(f"  {p['status'].upper():6s} {p['title'][:48]:48s} @ {p['entry_price']} -> "
                  f"{p['exit_price']}  {p['pnl']:+8.2f} ({p['close_reason']})")

if __name__ == "__main__":
    main()
