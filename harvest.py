# Smart-money harvester: download trader cohorts from Polymarket public APIs.
#   Cohort A ("naive leaderboard"): top 30 wallets by 30d PnL.
#   Cohort B ("control"): 30 random active wallets sampled from the public trade feed.
# For each wallet: full activity history (TRADE/REDEEM/SPLIT/MERGE/rebates, paginated)
# capped at MAX_ROWS or LOOKBACK_DAYS, plus current open positions.
# Output: data/wallets.json (cohort labels) + data/activity/<wallet>.json + data/positions/<wallet>.json

import json, os, time, random
from datetime import datetime, timezone, timedelta
import requests

BASE = "https://data-api.polymarket.com"
H = {"User-Agent": "Mozilla/5.0"}
ROOT = os.path.dirname(os.path.abspath(__file__))
DATA = os.path.join(ROOT, "data")
MAX_ROWS = 12000          # per-wallet activity cap
LOOKBACK_DAYS = 60        # stop paginating once rows are older than this
PAGE = 500

os.makedirs(os.path.join(DATA, "activity"), exist_ok=True)
os.makedirs(os.path.join(DATA, "positions"), exist_ok=True)

def get(path, **params):
    for attempt in range(3):
        try:
            r = requests.get(f"{BASE}{path}", params=params, headers=H, timeout=25)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 2:
                print(f"  [ERR] {path} {params.get('user','')[:10]}: {e}")
                return None
            time.sleep(1.5)
    return None

# ---------- 1. Cohorts ----------
lb = get("/v1/leaderboard", window="30d", rankType="pnl", limit=30) or []
cohort_a = [{"wallet": x["proxyWallet"], "cohort": "leaderboard",
             "lb_rank": int(x["rank"]), "lb_pnl": x.get("pnl"), "lb_vol": x.get("vol"),
             "name": x.get("userName", "")} for x in lb]
print(f"Leaderboard: {len(cohort_a)} wallets")

# Control: sample distinct wallets from several pages of the public trade feed
seen, control = set(x["wallet"] for x in cohort_a), []
for off in range(0, 5000, 500):
    feed = get("/trades", limit=500, offset=off) or []
    for t in feed:
        w = t.get("proxyWallet")
        if w and w not in seen:
            seen.add(w)
            control.append(w)
    time.sleep(0.15)
random.seed(42)
random.shuffle(control)
cohort_b = [{"wallet": w, "cohort": "control", "lb_rank": None, "lb_pnl": None,
             "lb_vol": None, "name": ""} for w in control[:30]]
print(f"Control pool: {len(control)} candidates -> sampled {len(cohort_b)}")

wallets = cohort_a + cohort_b
with open(os.path.join(DATA, "wallets.json"), "w") as f:
    json.dump({"harvested_at": datetime.now(timezone.utc).isoformat(), "wallets": wallets}, f, indent=1)

# ---------- 2. Per-wallet activity + positions ----------
cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp()
for i, wi in enumerate(wallets):
    w = wi["wallet"]
    apath = os.path.join(DATA, "activity", f"{w}.json")
    if os.path.exists(apath):
        print(f"[{i+1}/{len(wallets)}] {w[:10]} cached, skip")
        continue
    rows, offset = [], 0
    while offset < MAX_ROWS:
        batch = get("/activity", user=w, limit=PAGE, offset=offset)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE or min(x["timestamp"] for x in batch) < cutoff:
            break
        offset += PAGE
        time.sleep(0.12)
    rows = [x for x in rows if x["timestamp"] >= cutoff]
    with open(apath, "w") as f:
        json.dump(rows, f)

    pos = []
    for poff in (0, 500):
        pb = get("/positions", user=w, limit=500, offset=poff)
        if not pb:
            break
        pos.extend(pb)
        if len(pb) < 500:
            break
        time.sleep(0.1)
    with open(os.path.join(DATA, "positions", f"{w}.json"), "w") as f:
        json.dump(pos, f)

    print(f"[{i+1}/{len(wallets)}] {w[:10]} ({wi['cohort']}): {len(rows)} activity rows, {len(pos)} positions")
    time.sleep(0.15)

print("DONE")
