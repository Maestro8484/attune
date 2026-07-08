"""Parse /api/songs?extended=1 output (key<space>value blocks, blank-line separated)
into structured JSON + summary stats."""
import json, os, collections

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "library_extended.txt")

records = []
cur = {}
# Known scalar keys; everything else kept as string. Values may contain spaces.
INT_KEYS = {"track", "seconds", "bytes", "year", "bitrate", "modified", "added", "album-id"}

with open(SRC, encoding="utf-8", errors="replace") as f:
    for line in f:
        line = line.rstrip("\n")
        if not line.strip():
            if cur:
                records.append(cur)
                cur = {}
            continue
        # split on first space
        if " " in line:
            k, v = line.split(" ", 1)
        else:
            k, v = line, ""
        if k in INT_KEYS:
            try:
                v = int(v)
            except ValueError:
                pass
        cur[k] = v
if cur:
    records.append(cur)

print(f"parsed {len(records)} records")

# keys seen
keycount = collections.Counter()
for r in records:
    keycount.update(r.keys())
print("keys seen:", dict(keycount.most_common()))

# write JSON
with open(os.path.join(HERE, "library.json"), "w", encoding="utf-8") as f:
    json.dump(records, f, ensure_ascii=False, indent=1)

# stats
active = sum(1 for r in records if r.get("active") == "yes")
total_sec = sum(r.get("seconds", 0) for r in records if isinstance(r.get("seconds"), int))
exts = collections.Counter(os.path.splitext(r.get("file",""))[1].lower() for r in records)
genres = collections.Counter(r.get("genre","") for r in records)
years = collections.Counter(r.get("year","") for r in records if r.get("year"))
no_analysis = [r for r in records if r.get("active") != "yes"]

summary = {
    "total_records": len(records),
    "active_mixable": active,
    "inactive": len(records) - active,
    "total_hours": round(total_sec/3600, 1),
    "extensions": dict(exts),
    "distinct_genres": len(genres),
    "top_genres": genres.most_common(25),
    "keys_present": dict(keycount),
}
with open(os.path.join(HERE, "library_summary.json"), "w", encoding="utf-8") as f:
    json.dump(summary, f, ensure_ascii=False, indent=2)

print(f"active/mixable: {active}, inactive: {len(records)-active}")
print(f"total hours: {summary['total_hours']}")
print("extensions:", dict(exts))
print("wrote library.json, library_summary.json")
