"""Capture MusicIP ground-truth mixes for later validation of a reimplementation.

Strategy:
- Load parsed library.json.
- Select ~72 diverse seeds: stratified across genres and spread across years,
  restricted to active/mixable tracks whose files are reachable.
- For each seed, query /api/mix at a grid of (style, variety) settings.
  * One "deep similarity ranking": style=40, variety=0, size=100 -> the ordered
    nearest-neighbour list MusicIP produces (the key oracle for a distance model).
  * A variety sweep at fixed style to see how variety reshuffles/extends.
  * A style sweep at fixed variety.
- Save each result as JSON: {seed, params, tracks:[...ordered...]}.
- Write an index + manifest.

Idempotent: skips a (seed,params) whose output file already exists.
"""
import json, os, urllib.parse, urllib.request, hashlib, time, collections, sys

HERE = os.path.dirname(os.path.abspath(__file__))
GT = os.path.join(HERE, "groundtruth")
os.makedirs(GT, exist_ok=True)
BASE = "http://localhost:10002/api/mix"

library = json.load(open(os.path.join(HERE, "library.json"), encoding="utf-8"))
mixable = [r for r in library if r.get("active") == "yes" and r.get("file")]
print(f"{len(mixable)} mixable tracks")

# ---- diverse seed selection ----
by_genre = collections.defaultdict(list)
for r in mixable:
    by_genre[r.get("genre", "")].append(r)
# rank genres by size, take spread of seeds from the top genres + a few niche
genres_sorted = sorted(by_genre, key=lambda g: -len(by_genre[g]))
seeds = []
seen_files = set()

def add_seed(r):
    f = r["file"]
    if f in seen_files:
        return
    seen_files.add(f)
    seeds.append(r)

# 2 seeds from each of the top 24 genres, spread by year within genre
for g in genres_sorted[:24]:
    tracks = sorted(by_genre[g], key=lambda r: (r.get("year", 0), r.get("file", "")))
    if not tracks:
        continue
    picks = [tracks[len(tracks)//4], tracks[3*len(tracks)//4]] if len(tracks) > 1 else [tracks[0]]
    for p in picks:
        add_seed(p)
# a handful of niche-genre seeds
for g in genres_sorted[24:60:6]:
    add_seed(by_genre[g][len(by_genre[g])//2])

seeds = seeds[:72]
print(f"selected {len(seeds)} diverse seeds across {len(set(s.get('genre','') for s in seeds))} genres")

# ---- parameter grid ----
def param_sets():
    yield {"style": 40, "variety": 0, "size": 100, "sizetype": "tracks", "mixgenre": 0, "tag": "deep_s40_v0"}
    for v in (0, 3, 6, 9):
        yield {"style": 40, "variety": v, "size": 25, "sizetype": "tracks", "mixgenre": 0, "tag": f"var_s40_v{v}"}
    for s in (0, 20, 60, 100):
        yield {"style": s, "variety": 0, "size": 25, "sizetype": "tracks", "mixgenre": 0, "tag": f"sty_s{s}_v0"}
    yield {"style": 40, "variety": 0, "size": 25, "sizetype": "tracks", "mixgenre": 1, "tag": "genre_s40_v0"}

def fetch_mix(seed_file, params):
    q = {k: v for k, v in params.items() if k != "tag"}
    q["song"] = seed_file
    url = BASE + "?" + urllib.parse.urlencode(q)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=30) as resp:
                data = resp.read().decode("utf-8", "replace")
            return [l for l in data.splitlines() if l.strip()]
        except Exception as e:
            if attempt == 2:
                return {"error": str(e)}
            time.sleep(2)

manifest = []
t0 = time.time()
done = 0
skipped = 0
for si, seed in enumerate(seeds):
    sf = seed["file"]
    sid = hashlib.md5(sf.encode("utf-8")).hexdigest()[:12]
    for params in param_sets():
        out = os.path.join(GT, f"{sid}_{params['tag']}.json")
        if os.path.exists(out):
            skipped += 1
            manifest.append({"seed": sf, "sid": sid, "tag": params["tag"], "file": os.path.basename(out)})
            continue
        tracks = fetch_mix(sf, params)
        rec = {
            "seed": sf,
            "seed_meta": {k: seed.get(k) for k in ("name", "artist", "album", "genre", "year", "seconds")},
            "params": {k: v for k, v in params.items() if k != "tag"},
            "tag": params["tag"],
        }
        if isinstance(tracks, dict):
            rec["error"] = tracks["error"]
            rec["tracks"] = []
        else:
            # first line is typically the seed itself; keep full ordered list
            rec["tracks"] = tracks
            rec["count"] = len(tracks)
        json.dump(rec, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        manifest.append({"seed": sf, "sid": sid, "tag": params["tag"], "file": os.path.basename(out),
                         "count": rec.get("count", 0), "error": rec.get("error")})
        done += 1
    if (si + 1) % 10 == 0:
        print(f"  {si+1}/{len(seeds)} seeds, {done} fetched, {time.time()-t0:.0f}s")

json.dump({"seeds": [{"file": s["file"], "sid": hashlib.md5(s["file"].encode()).hexdigest()[:12],
                       "meta": {k: s.get(k) for k in ('name','artist','album','genre','year')}} for s in seeds],
           "param_tags": [p["tag"] for p in param_sets()],
           "manifest": manifest},
          open(os.path.join(GT, "_index.json"), "w", encoding="utf-8"), ensure_ascii=False, indent=1)

errs = sum(1 for m in manifest if m.get("error"))
print(f"DONE: {done} fetched, {skipped} skipped, {errs} errors, {time.time()-t0:.0f}s total")
print(f"wrote {GT}\\_index.json")
