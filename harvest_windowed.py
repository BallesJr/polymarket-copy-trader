# Windowed deep-harvest: bypasses the /activity offset cap (400 above offset 3000)
# by chunking the 180-day lookback into time windows via start/end params.
# If a window still hits the cap, it is split in half recursively.

import json, os, time
from datetime import datetime, timezone, timedelta
import requests

BASE = "https://data-api.polymarket.com"
H = {"User-Agent": "Mozilla/5.0"}
ROOT = os.path.dirname(os.path.abspath(__file__))
DEEP = os.path.join(ROOT, "data", "deep")
LOOKBACK_DAYS = 180
PAGE = 500
OFFSET_CAP = 3000  # last offset the API accepts

def get(**params):
    for attempt in range(4):
        try:
            r = requests.get(f"{BASE}/activity", params=params, headers=H, timeout=25)
            if r.status_code == 429:
                time.sleep(2 + attempt * 3); continue
            if r.status_code == 400:
                return "CAP"
            r.raise_for_status()
            return r.json()
        except Exception as e:
            if attempt == 3:
                print(f"  [ERR] {e}", flush=True); return None
            time.sleep(1.5)
    return None

def fetch_window(user, start, end, depth=0):
    rows, offset = [], 0
    while True:
        batch = get(user=user, limit=PAGE, offset=offset, start=int(start), end=int(end))
        if batch == "CAP" or (batch is not None and offset >= OFFSET_CAP and len(batch) == PAGE):
            # window too dense -> split
            if depth > 12 or end - start < 3600:
                print(f"  [WARN] finestra mínima encara densa {start}-{end}", flush=True)
                return rows
            mid = (start + end) / 2
            return fetch_window(user, start, mid, depth + 1) + fetch_window(user, mid, end, depth + 1)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < PAGE:
            break
        offset += PAGE
        time.sleep(0.1)
    return rows

TARGETS = ["MD14", "Woaifacai", "ygggg1", "jtwyslljy", "0x53757615de"]

with open(os.path.join(ROOT, "data", "metrics.json")) as f:
    metrics = json.load(f)
n2w = {m["name"] or m["wallet"][:12]: m["wallet"] for m in metrics if m["cohort"] == "leaderboard"}

now = datetime.now(timezone.utc)
t0 = (now - timedelta(days=LOOKBACK_DAYS)).timestamp()
t1 = now.timestamp()

for name in TARGETS:
    w = n2w[name]
    print(f"{name} ({w[:10]})...", flush=True)
    # chunks of 10 days, splitting on density
    rows = []
    cur = t0
    while cur < t1:
        nxt = min(cur + 10 * 86400, t1)
        rows.extend(fetch_window(w, cur, nxt))
        cur = nxt
    # dedupe
    seen, clean = set(), []
    for a in rows:
        k = (a.get("transactionHash"), a.get("type"), a.get("asset"), a.get("side"),
             a.get("size"), a.get("timestamp"))
        if k in seen: continue
        seen.add(k); clean.append(a)
    with open(os.path.join(DEEP, f"{w}.json"), "w") as f:
        json.dump(clean, f)
    ts = [a["timestamp"] for a in clean]
    span = (max(ts) - min(ts)) / 86400 if ts else 0
    print(f"  -> {len(clean)} files, span {span:.0f} dies", flush=True)

print("DONE", flush=True)
