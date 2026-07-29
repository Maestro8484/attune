r"""Last.fm import, Phase 1: READ-ONLY resolution probe.

The project's own named unlock for the behavioral head is importing an existing Last.fm
scrobble history (see eval/SIGNAL_REPORT.md section 4, "what would unblock it" item 2). That
NO-GO stands until its retry gate -- this script is step one toward that retry: pull the
operator's Last.fm recent + loved tracks and find out what fraction resolve to a track in the
analyzed pool BEFORE any write/backfill design gets built. If the resolution rate is low, the
next question is whether that is unowned music or a matching defect, and the miss samples this
script prints are how you tell the two apart.

WRITES NOTHING. No usermeta row, no DB table, anywhere, is touched. It opens the DB read-only
(file:...?mode=ro, same as every other eval/ script) and only ever does a network GET against
the Last.fm API. The only file it writes is its own JSON report (--out), never the library DB.

Reuse, not reinvent:
  * The pool definition -- "CLAP-512 AND finite librosa-79", exactly HybridEngine's
    clap-intersect-librosa pool -- is imported verbatim from eval/build_behavior_pairs.py's
    load_pool(). Every resolution rate below is stamped with THIS run's observed pool size
    (LAW 3: recall/resolution numbers are not comparable across pool sizes).
  * .env is located via src/export.py's find_env()/load_env(), the project's one env-loading
    path, instead of hand-rolling a second dotenv parser.
  * The module-loading pattern (importlib.util.spec_from_file_location for a sibling script,
    the UTF-8 stdout rewrap guard) matches build_behavior_pairs.py and probe_behavior_signal.py
    so this script reads as the same family, not a one-off.

One thing NOT reused, because it does not exist yet: build_behavior_pairs.py's own
"resolution" is path reconciliation (src/musicip_engine.relkey, a mirror-invariant over file
paths for two copies of the SAME local library). Last.fm scrobbles carry artist/title text, not
a file path in this library at all, so path-relkey matching does not apply -- there is no
existing artist/title matcher anywhere in this repo to reuse (checked: src/musicip_engine.py's
search() is a path substring match; nothing else in src/ or eval/ normalizes artist/title
text). The normalize()/normalize_loose() pair below is new, deliberately small (casefold +
accent-strip + punctuation-fold, then a looser pass that also drops parenthetical tags like
"(Live)"/"(Remastered)"/"feat. X"), and is the one piece of this script that is genuinely new
matching logic rather than reused. See NORMALIZE COMMENT below for exactly what it does.

usage:
  mixer-ng\.venv\Scripts\python.exe eval\lastfm_resolution_probe.py --db mixer-ng\data\_p0c.db
  mixer-ng\.venv\Scripts\python.exe eval\lastfm_resolution_probe.py --db mixer-ng\data\_p0c.db --max-pages 5   (smoke test)
"""
from __future__ import annotations

import argparse
import collections
import importlib.util
import io
import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

if (getattr(sys.stdout, "encoding", "") or "").lower().replace("-", "") != "utf8":
    # only rewrap a non-UTF-8 console; rewrapping an already-wrapped stdout detaches the
    # buffer the first wrapper owns and every later print raises (bites on import).
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8",
                                  errors="replace", line_buffering=True)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)                       # attune/

API_URL = "https://ws.audioscrobbler.com/2.0/"
USER_AGENT = "attune-lastfm-resolution-probe/1.0 (+read-only research script)"


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# --- prior art: build_behavior_pairs.py's pool definition, reused verbatim (see LAW 3 note
# in the module docstring). Do NOT redefine "the pool" a second way in this file. ---
_bbp = _load_module("build_behavior_pairs", os.path.join(HERE, "build_behavior_pairs.py"))
load_pool = _bbp.load_pool

# --- prior art: src/export.py's env-loading path, reused instead of a second dotenv parser ---
_export = _load_module("export", os.path.join(ROOT, "src", "export.py"))
find_env = _export.find_env
load_env = _export.load_env


# --------------------------------------------------------------------- artist/title matching
# NORMALIZE COMMENT: nothing in this repo already does artist/title text matching (see module
# docstring), so this is new. Two tiers, both simple and both documented here rather than left
# to guesswork:
#   normalize()        casefold, strip accents (NFKD + drop combining marks), fold "&" to
#                       "and", collapse all punctuation/whitespace. No tag stripping. This is
#                       the STRICT tier.
#   normalize_loose()   everything normalize() does, PLUS drops "(...)"/"[...]" parenthetical
#                       tags entirely (covers "(Live)", "(Remastered 2011)", "(feat. X)", a
#                       bare "feat./ft. ..." suffix with no parens). This is the LOOSE tier --
#                       a track that only resolves here differs from the library tag by a
#                       version/edition marker, not by being a different recording.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+")
_PAREN_RE = re.compile(r"[\(\[][^\)\]]*[\)\]]")
_FEAT_TAIL_RE = re.compile(r"\b(feat|featuring|ft)\.?\s+.*$", re.I)


def _fold(s):
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower().replace("&", " and ")
    s = _PUNCT_RE.sub(" ", s)
    return _WS_RE.sub(" ", s).strip()


def normalize(s):
    return _fold(s or "")


def normalize_loose(s):
    s = s or ""
    s = _PAREN_RE.sub(" ", s)
    s = _FEAT_TAIL_RE.sub("", s)
    return _fold(s)


def load_pool_artist_title(db_path, pool_paths):
    """artist/title per pool track, keyed by the same path load_pool() returned. Reads the
    `tracks` table read-only and keeps only rows whose path is in the pool -- the pool stays
    exactly what load_pool() defined, this just attaches the tags needed for text matching."""
    pool_set = set(pool_paths)
    con = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
    out = {}
    for p, artist, title in con.execute("SELECT path, artist, title FROM tracks"):
        if p in pool_set:
            out[p] = (artist or "", title or "")
    con.close()
    return out


def build_indices(pool_paths, meta):
    strict_idx = collections.defaultdict(list)   # (artist, title) strict -> [pool_i, ...]
    loose_idx = collections.defaultdict(list)     # (artist, title) loose  -> [pool_i, ...]
    artist_idx = collections.defaultdict(list)    # artist strict         -> [pool_i, ...]
    for i, p in enumerate(pool_paths):
        artist, title = meta.get(p, ("", ""))
        na, nt = normalize(artist), normalize(title)
        if na or nt:
            strict_idx[(na, nt)].append(i)
        if na:
            artist_idx[na].append(i)
        la, lt = normalize_loose(artist), normalize_loose(title)
        if la or lt:
            loose_idx[(la, lt)].append(i)
    return strict_idx, loose_idx, artist_idx


def resolve(artist_raw, title_raw, strict_idx, loose_idx, artist_idx):
    """-> (tier, category) where tier in {"strict","loose","miss"} and category is only set
    for a miss, classifying WHY, for the report's miss breakdown."""
    na, nt = normalize(artist_raw), normalize(title_raw)
    if (na, nt) in strict_idx:
        return "strict", None
    la, lt = normalize_loose(artist_raw), normalize_loose(title_raw)
    if (la, lt) in loose_idx:
        return "loose", None
    if na in artist_idx or la in artist_idx:
        return "miss", "artist_owned_title_mismatch"
    return "miss", "artist_not_in_pool"


# --------------------------------------------------------------------------- Last.fm API
def _mask(url, api_key):
    if not api_key:
        return url
    return url.replace(urllib.parse.quote(api_key, safe=""), "***")


def lastfm_call(method, api_key, user, page, limit, sleep_s, max_retries=4):
    params = {"method": method, "user": user, "api_key": api_key, "format": "json",
              "page": str(page), "limit": str(limit)}
    url = API_URL + "?" + urllib.parse.urlencode(params)
    safe_url = _mask(url, api_key)
    last_err = None
    for attempt in range(1, max_retries + 1):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=20) as r:
                body = r.read().decode("utf-8", "replace")
                status = r.getcode()
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", "replace")
            status = e.code
        except urllib.error.URLError as e:
            last_err = f"network error calling Last.fm ({method} page {page}): {e.reason}"
            time.sleep(min(30.0, sleep_s * attempt * 2))
            continue
        if status == 429:
            last_err = f"HTTP 429 rate-limited on {method} page {page}"
            time.sleep(min(30.0, max(1.0, sleep_s) * attempt * 3))
            continue
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            raise RuntimeError(f"Last.fm returned non-JSON (status {status}) for {method} "
                                f"page {page}: {body[:200]!r} -- {safe_url}")
        if "error" in data:
            # an API-level error (bad key, unknown user, ...) will not fix itself on retry
            raise RuntimeError(f"Last.fm API error {data.get('error')}: "
                                f"{data.get('message', '?')} (HTTP {status}) for {method} "
                                f"page {page} -- {safe_url}")
        if status != 200:
            last_err = f"Last.fm HTTP {status} for {method} page {page}: {body[:200]!r}"
            time.sleep(min(30.0, sleep_s * attempt))
            continue
        return data
    raise RuntimeError(last_err or f"Last.fm call failed after {max_retries} attempts "
                                    f"for {method} page {page}")


def fetch_paginated(method, api_key, user, sleep_s, limit, max_pages, root_key, label):
    """Generic paginator for user.getrecenttracks / user.getlovedtracks. root_key is the
    top-level JSON key ("recenttracks" / "lovedtracks") Last.fm nests results under."""
    page = 1
    items = []
    total_pages = None
    total = None
    while True:
        data = lastfm_call(method, api_key, user, page, limit, sleep_s)
        root = data.get(root_key, {})
        attr = root.get("@attr", {})
        if total_pages is None:
            total_pages = int(attr.get("totalPages", 1) or 1)
            total = int(attr.get("total", 0) or 0)
            print(f"  {label}: {total:,} total, {total_pages:,} pages "
                  f"(page size {limit})")
        tr = root.get("track", [])
        if isinstance(tr, dict):
            tr = [tr]
        items.extend(tr)
        if max_pages and page >= max_pages:
            print(f"  {label}: stopped at --max-pages {max_pages} "
                  f"({page}/{total_pages} pages pulled)")
            break
        if page >= total_pages:
            break
        page += 1
        if page % 10 == 1:
            print(f"  {label}: page {page}/{total_pages}...")
        time.sleep(sleep_s)
    return items, total, total_pages


def entry_artist_title(entry):
    a = entry.get("artist", {})
    if isinstance(a, dict):
        artist = a.get("#text") or a.get("name") or ""
    else:
        artist = str(a or "")
    title = entry.get("name", "") or ""
    return artist, title


def is_nowplaying(entry):
    attr = entry.get("@attr", {})
    return bool(isinstance(attr, dict) and attr.get("nowplaying") == "true")


# --------------------------------------------------------------------------- report
def tally(entries, strict_idx, loose_idx, artist_idx, sample_cap):
    counts = collections.Counter()
    miss_categories = collections.Counter()
    miss_samples = collections.defaultdict(list)
    for artist, title in entries:
        tier, cat = resolve(artist, title, strict_idx, loose_idx, artist_idx)
        counts[tier] += 1
        if tier == "miss":
            miss_categories[cat] += 1
            if len(miss_samples[cat]) < sample_cap:
                miss_samples[cat].append({"artist": artist, "title": title})
    return counts, miss_categories, miss_samples


def pct(n, d):
    return (100.0 * n / d) if d else 0.0


def print_block(label, entries, counts, miss_categories, miss_samples, pool_size):
    n = len(entries)
    resolved = counts["strict"] + counts["loose"]
    print()
    print("=" * 78)
    print(f"{label}  (pool size: {pool_size:,} tracks -- LAW 3 stamp)")
    print("=" * 78)
    print(f"  entries pulled            : {n:,}")
    print(f"  resolved (strict)         : {counts['strict']:,}  ({pct(counts['strict'], n):.1f}%)")
    print(f"  resolved (strict+loose)   : {resolved:,}  ({pct(resolved, n):.1f}%)")
    print(f"  unresolved                : {counts['miss']:,}  ({pct(counts['miss'], n):.1f}%)")
    if miss_categories:
        print("  miss breakdown:")
        for cat, c in miss_categories.most_common():
            print(f"    {cat:28} {c:,}  ({pct(c, counts['miss']):.1f}% of misses)")
    for cat, samples in miss_samples.items():
        if not samples:
            continue
        print(f"  sample misses [{cat}]:")
        for s in samples:
            print(f"    {s['artist']!r} - {s['title']!r}")


def main():
    ap = argparse.ArgumentParser(
        description="Phase 1 of Last.fm import: READ-ONLY resolution probe against the pool")
    ap.add_argument("--db", required=True, help="mixer.db (opened READ-ONLY, never written)")
    ap.add_argument("--user", default=None, help="Last.fm username (default: LASTFM_USER in .env)")
    ap.add_argument("--api-key", default=None, help="Last.fm API key (default: LASTFM_API_KEY in .env)")
    ap.add_argument("--env", default=None, help="explicit .env path (default: find_env() search)")
    ap.add_argument("--limit", type=int, default=200, help="Last.fm page size (API max is 200)")
    ap.add_argument("--sleep", type=float, default=0.25, help="seconds between paginated calls")
    ap.add_argument("--max-pages", type=int, default=0,
                    help="cap pages per endpoint (0 = pull everything); use a small value for a smoke test")
    ap.add_argument("--sample-misses", type=int, default=15, help="miss samples to print/save per category")
    ap.add_argument("--out", default=os.path.join(HERE, "lastfm_resolution_report.json"))
    args = ap.parse_args()

    env_path = find_env(args.env)
    cfg = load_env(args.env)
    api_key = args.api_key or cfg.get("LASTFM_API_KEY") or os.environ.get("LASTFM_API_KEY")
    user = args.user or cfg.get("LASTFM_USER") or os.environ.get("LASTFM_USER")

    print(f".env located at: {env_path or '(not found)'}")
    print(f"Last.fm user   : {user or '(missing)'}")
    print(f"Last.fm API key: {'set (' + str(len(api_key)) + ' chars, not printed)' if api_key else '(missing)'}")

    if not api_key or not user:
        missing = []
        if not api_key:
            missing.append("LASTFM_API_KEY")
        if not user:
            missing.append("LASTFM_USER")
        print(f"\nERROR: missing {', '.join(missing)}. Set in {env_path or '.env'} "
              f"or pass --api-key/--user. Nothing was called.")
        sys.exit(2)

    print(f"\nDB: {args.db}")
    paths, by_relkey, collisions = load_pool(args.db)
    pool_size = len(paths)
    print(f"pool (CLAP-512 AND librosa-79, same definition as build_behavior_pairs.load_pool): "
          f"{pool_size:,} tracks")
    if collisions:
        print(f"  note: {collisions} pool paths share a relkey with an earlier one; first-wins "
              f"(inherited from load_pool, irrelevant to artist/title matching)")

    meta = load_pool_artist_title(args.db, paths)
    strict_idx, loose_idx, artist_idx = build_indices(paths, meta)
    print(f"artist/title index built from {len(meta):,} of {pool_size:,} pool tracks "
          f"({pool_size - len(meta):,} had no tracks-table row)")

    print("\nfetching from Last.fm...")
    try:
        recent_raw, recent_total, recent_pages = fetch_paginated(
            "user.getrecenttracks", api_key, user, args.sleep, args.limit, args.max_pages,
            "recenttracks", "recent tracks")
        loved_raw, loved_total, loved_pages = fetch_paginated(
            "user.getlovedtracks", api_key, user, args.sleep, args.limit, args.max_pages,
            "lovedtracks", "loved tracks")
    except RuntimeError as e:
        print(f"\nERROR calling the Last.fm API: {e}")
        sys.exit(1)

    nowplaying = [e for e in recent_raw if is_nowplaying(e)]
    recent_scrobbles = [e for e in recent_raw if not is_nowplaying(e)]
    print(f"\nrecent tracks pulled: {len(recent_raw):,} "
          f"({len(nowplaying)} excluded as 'now playing', not a completed scrobble)")
    print(f"loved tracks pulled : {len(loved_raw):,}")

    recent_pairs = [entry_artist_title(e) for e in recent_scrobbles]
    loved_pairs = [entry_artist_title(e) for e in loved_raw]

    r_counts, r_miss_cat, r_miss_samples = tally(
        recent_pairs, strict_idx, loose_idx, artist_idx, args.sample_misses)
    l_counts, l_miss_cat, l_miss_samples = tally(
        loved_pairs, strict_idx, loose_idx, artist_idx, args.sample_misses)

    print_block("RECENT TRACKS (scrobbles, now-playing excluded)", recent_pairs,
                r_counts, r_miss_cat, r_miss_samples, pool_size)
    print_block("LOVED TRACKS", loved_pairs, l_counts, l_miss_cat, l_miss_samples, pool_size)

    combined_n = len(recent_pairs) + len(loved_pairs)
    combined_resolved = (r_counts["strict"] + r_counts["loose"]
                          + l_counts["strict"] + l_counts["loose"])
    print()
    print("=" * 78)
    print(f"COMBINED (pool size: {pool_size:,} tracks -- LAW 3 stamp)")
    print("=" * 78)
    print(f"  entries pulled             : {combined_n:,}")
    print(f"  resolved (strict+loose)    : {combined_resolved:,}  ({pct(combined_resolved, combined_n):.1f}%)")

    out = {
        "pool_size": pool_size,
        "db": os.path.abspath(args.db),
        "lastfm_user": user,
        "recent": {
            "api_total": recent_total, "api_total_pages": recent_pages,
            "pulled": len(recent_raw), "nowplaying_excluded": len(nowplaying),
            "scrobbles_considered": len(recent_pairs),
            "resolved_strict": r_counts["strict"], "resolved_loose": r_counts["loose"],
            "unresolved": r_counts["miss"],
            "miss_categories": dict(r_miss_cat),
            "miss_samples": {k: v for k, v in r_miss_samples.items()},
        },
        "loved": {
            "api_total": loved_total, "api_total_pages": loved_pages,
            "pulled": len(loved_raw),
            "resolved_strict": l_counts["strict"], "resolved_loose": l_counts["loose"],
            "unresolved": l_counts["miss"],
            "miss_categories": dict(l_miss_cat),
            "miss_samples": {k: v for k, v in l_miss_samples.items()},
        },
        "config": {"limit": args.limit, "sleep": args.sleep, "max_pages": args.max_pages},
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=1, ensure_ascii=False)
    print(f"\nwrote {args.out}")


if __name__ == "__main__":
    main()
