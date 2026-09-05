"""Fetch all search_kb query_text from Langfuse and generate statistics."""
import httpx
import json
import os
import time
from collections import Counter
from datetime import datetime
from pathlib import Path

from dotenv import load_dotenv

# Load .env from project root
project_root = Path(__file__).resolve().parent.parent
load_dotenv(project_root / ".env")

base = os.getenv("LANGFUSE_BASE_URL", "http://localhost:3000").rstrip("/")
public_key = os.getenv("LANGFUSE_PUBLIC_KEY", "")
secret_key = os.getenv("LANGFUSE_SECRET_KEY", "")
auth = (public_key, secret_key)

all_records = []
errors = 0
page = 1
max_page = 700  # safety limit

print(f"Starting fetch at {datetime.now()}")
while page <= max_page:
    try:
        resp = httpx.get(
            f"{base}/api/public/observations",
            auth=auth,
            params={"name": "search_kb", "limit": 50, "page": page},
            timeout=30,
        )
        if resp.status_code != 200:
            print(f"Page {page}: HTTP {resp.status_code}")
            errors += 1
            if errors > 5:
                break
            page += 1
            continue

        data = resp.json()
        batch = data.get("data", [])
        if not batch:
            print(f"Page {page}: empty, stopping")
            break

        for obs in batch:
            inp = obs.get("input")
            inp_dict = {}
            if inp and isinstance(inp, dict):
                inp_dict = inp
            elif inp and isinstance(inp, str):
                try:
                    inp_dict = json.loads(inp)
                except json.JSONDecodeError:
                    pass

            query = inp_dict.get("query_text", "")
            limit_val = inp_dict.get("limit", None)
            target_tables = inp_dict.get("target_tables", None)

            created = obs.get("createdAt", "")
            if query:
                all_records.append({
                    "query_text": query,
                    "limit": limit_val,
                    "target_tables": target_tables,
                    "created_at": created,
                })

        if page % 100 == 0:
            print(f"Progress: page {page}, collected {len(all_records)} records")

        page += 1
    except Exception as e:
        print(f"Page {page} error: {e}")
        errors += 1
        if errors > 5:
            break
        time.sleep(0.5)

print(f"\nDone at {datetime.now()}")
print(f"Total records: {len(all_records)}")

# Frequency count
queries = [r["query_text"] for r in all_records]
counter = Counter(queries)

# limit distribution
limits = [r["limit"] for r in all_records]
limit_counter = Counter(limits)

# target_tables distribution
all_tables = []
for r in all_records:
    tt = r["target_tables"]
    if tt is None:
        all_tables.append("<未填>")
    elif isinstance(tt, list):
        all_tables.append(",".join(sorted(tt)))
    else:
        all_tables.append(str(tt))
tables_counter = Counter(all_tables)

# cross-tab: query_text + target_tables combo
combos = []
for r in all_records:
    tt = r["target_tables"]
    tt_str = "<未填>" if tt is None else ",".join(sorted(tt)) if isinstance(tt, list) else str(tt)
    combos.append((r["query_text"], r["limit"], tt_str))
combo_counter = Counter(combos)

# Save full data
with open(project_root / "search_kb_queries.json", "w", encoding="utf-8") as f:
    json.dump(all_records, f, ensure_ascii=False, indent=2)

# Save stats
with open(project_root / "search_kb_queries_stats.json", "w", encoding="utf-8") as f:
    json.dump({
        "generated_at": datetime.now().isoformat(),
        "total_records": len(all_records),
        "unique_queries": len(counter),
        "frequency": counter.most_common(),
        "limit_distribution": dict(limit_counter.most_common()),
        "target_tables_distribution": dict(tables_counter.most_common()),
        "top_combos": [{"query_text": c[0], "limit": c[1], "target_tables": c[2], "count": cnt}
                       for c, cnt in combo_counter.most_common(50)],
    }, f, ensure_ascii=False, indent=2)

print(f"Unique queries: {len(counter)}")
print("Top 30 queries:")
for q, cnt in counter.most_common(30):
    print(f"  [{cnt:5d}] {q}")
print(f"\nLimit distribution: {dict(limit_counter.most_common())}")
print(f"Target tables distribution: {dict(tables_counter.most_common())}")
