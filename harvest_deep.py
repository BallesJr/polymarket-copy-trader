# Deep harvest: long-lookback activity for shortlisted candidate wallets.
# Same output format as harvest.py but 180-day lookback, higher row cap,
# separate directory (data/deep/) so the v0 dataset stays intact.

import json, os, time, sys
from datetime import datetime, timezone, timedelta
import requests

BASE = "https://data-api.polymarket.com"
H = {"User-Agent": "Mozilla/5.0"}
ROOT = os.path.dirname(os.path.abspath(__file__))
DEEP = os.path.join(ROOT, "data", "deep")
LOOKBACK_DAYS = 180
MAX_ROWS = 40000
PAGE = 500

# Shortlist: t>=~1.4 with >=10 active days (human-scale first) + the high-t HFTs for contrast
CANDIDATES = {
    "MD14":          "wallet_from_metrics",
    "balekadyr":     "wallet_from_metrics",
    "cnyek":         "wallet_from_metrics",
    "zb8":           "wallet_from_metrics",
    "Woaifacai":     "wallet_from_metrics",
    "ygggg1":        "wallet_from_metrics",
    "jtwyslljy":     "wallet_from_metrics",
    "0x53757615de":  "wallet_from_metrics",
}

os.makedirs(DEEP, exist_ok=True)

def get(path, **params):
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE}{path}", params=params, headers=H, timeout=25)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3)
                continue
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 3:
                print(f"  [ERR] {path}: {e}", flush=True)
                return None
            time.sleep(1.5)
    return None

with open(os.path.join(ROOT, "data", "metrics.json")) as f:
    metrics = json.load(f)
name2wallet = {m["name"] or m["wallet"][:12]: m["wallet"] for m in metrics if m["cohort"] == "leaderboard"}
targets = {n: name2wallet[n] for n in CANDIDATES if n in name2wallet}
print(f"Deep harvest de {len(targets)} wallets: {list(targets)}", flush=True)

cutoff = (datetime.now(timezone.utc) - timedelta(days=LOOKBACK_DAYS)).timestamp()
for name, w in targets.items():
    apath = os.path.join(DEEP, f"{w}.json")
    if os.path.exists(apath):
        print(f"{name}: cached", flush=True)
        continue
    rows, offset = [], 0
    while offset < MAX_ROWS:
        batch = get("/activity", user=w, limit=PAGE, offset=offset)
        if not batch:
            break
        rows.extend(batch)
        oldest = min(x["timestamp"] for x in batch)
        if len(batch) < PAGE or oldest < cutoff:
            break
        offset += PAGE
        time.sleep(0.12)
    kept = [x for x in rows if x["timestamp"] >= cutoff]
    with open(apath, "w") as f:
        json.dump(kept, f)
    span = (max(x["timestamp"] for x in kept) - min(x["timestamp"] for x in kept)) / 86400 if kept else 0
    print(f"{name} ({w[:10]}): {len(kept)} rows, span {span:.0f} days"
          f"{' [CAP HIT]' if offset >= MAX_ROWS else ''}", flush=True)
    time.sleep(0.2)
print("DONE", flush=True)
