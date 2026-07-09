"""MusicIP engine adapter — presents the Attune engine interface over a live MusicIP Mixer.

Wraps the MusicMagic HTTP API (localhost:10002) exactly the way attune/bridge/bridge.py already
does for /api/songs and /api/mix — reuse, not reinvent. NOTE: this API build has no working
/api/search endpoint (verified live: GET /api lists only version/duplicates/mix, and
/api/search?q=... errors for any query) — search() below substring-matches the cached
/api/songs catalog instead, the same "search over what we already fetched" fallback
bridge.py's own /api/search route uses.

Path reconciliation: MusicIP speaks UNC paths (\\\\DiskStation\\music\\Music Library\\...) while our
DB speaks local L:\\ paths. The library tree is a MIRROR, so we reconcile by the path tail after
"Music Library/" (the same swap export.py's PathMapper does for playlists). This keeps the adapter
engine-agnostic — no hardcoded drive letters.

Capabilities: search + similar + MusicIP-native style/variety controls. It does NOT support the
CLAP-only extras (thumbs/refine, mmr, flow, explain) — those need our vectors and stay a hybrid/V2
concern. The shell hides UI for capabilities an engine lacks.
"""
import urllib.parse
import urllib.request

# MusicIP /api/mix defaults, matching bridge.py's proven call.
MIX_DEFAULTS = {"sizetype": "tracks", "style": "40", "variety": "5",
                "mixgenre": "0", "rejectsize": "3", "rejecttype": "tracks"}


def relkey(path):
    """Tree tail after 'Music Library/', lowercased/forward-slashed — the mirror-invariant key
    shared by a track's UNC path (MusicIP) and its L:\\ path (our DB)."""
    n = (path or "").replace("\\", "/").lower()
    i = n.rfind("music library/")
    return n[i + len("music library/"):] if i >= 0 else n


class MusicIPEngine:
    name = "musicip"
    capabilities = frozenset({"search", "similar", "style", "variety"})

    def __init__(self, url="http://localhost:10002", timeout=60):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self._songs_cache = None

    def _get(self, pathq, timeout=None):
        with urllib.request.urlopen(self.url + pathq, timeout=timeout or self.timeout) as r:
            return r.read().decode("utf-8", "replace")

    def alive(self):
        try:
            # /api/version is a plain GET (per the API index); /api/status is a form endpoint
            self._get("/api/version", timeout=5)
            return True
        except Exception:
            return False

    def songs(self):
        """All track paths MusicIP has analyzed (UNC). Cached: repeated calls (e.g. one
        per search()) reuse the first fetch instead of re-pulling ~17k lines each time."""
        if self._songs_cache is None:
            self._songs_cache = [l.strip() for l in
                                  self._get("/api/songs", timeout=300).splitlines() if l.strip()]
        return self._songs_cache

    def search(self, q, limit=40):
        """Substring match over cached track paths (case-insensitive).

        NOTE (verified live, not assumed): this HTTP API's own root page (GET /api) lists
        only version/duplicates/mix -- there is no working text-search endpoint on this
        install. GET /api/search?q=... returns "MusicIP API error - invalid request or
        internal error." for every query tried (q=, song=, artist=, text=). So this can't
        wrap a native call the way songs()/similar() do; instead it substring-matches the
        cached /api/songs catalog's UNC paths (artist/album/title live in the path itself,
        e.g. .../Albums/ACDC/1980 - Back In Black/...) -- the same "search over what we
        already fetched" approach bridge.py's own /api/search uses locally."""
        q = (q or "").strip().lower()
        if len(q) < 2:
            return []
        out = []
        for p in self.songs():
            if q in p.lower():
                out.append(p)
                if len(out) >= limit:
                    break
        return out

    def similar(self, seed_path, size=25, style=40, variety=5, **_ignored):
        """MusicIP mix seeded by a full (UNC) song path -> list of track paths (seed removed)."""
        args = dict(MIX_DEFAULTS, song=seed_path, size=str(int(size)),
                    style=str(int(style)), variety=str(int(variety)))
        body = self._get("/api/mix?" + urllib.parse.urlencode(args))
        seed_k = relkey(seed_path)
        return [t.strip() for t in body.splitlines()
                if t.strip() and relkey(t) != seed_k]


def build_reconciler(paths):
    """Map relkey -> pool index for a list of our DB paths, so MusicIP UNC results can be
    resolved to our engine's pool (for scoring, playback, export)."""
    return {relkey(p): i for i, p in enumerate(paths)}


def to_pool_indices(reconciler, paths):
    """Resolve MusicIP paths to our pool indices; None for tracks not in our pool."""
    return [reconciler.get(relkey(p)) for p in paths]


if __name__ == "__main__":
    # Self-test: adapter alive, library known, mix works, and reconciliation hit-rate vs our DB.
    import argparse
    import importlib.util
    import os

    ap = argparse.ArgumentParser(description="MusicIP adapter self-test")
    ap.add_argument("--db", required=True, help="our mixer DB, to measure path reconciliation")
    ap.add_argument("--url", default="http://localhost:10002")
    args = ap.parse_args()

    eng = MusicIPEngine(args.url)
    print("alive:", eng.alive())
    mip_songs = eng.songs()
    print(f"MusicIP knows {len(mip_songs):,} tracks (UNC paths)")

    here = os.path.dirname(os.path.abspath(__file__))
    spec = importlib.util.spec_from_file_location("hybrid", os.path.join(here, "hybrid.py"))
    hyb = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(hyb)
    v2 = hyb.HybridEngine(args.db)
    recon = build_reconciler(v2.paths)
    print(f"our DB pool: {len(v2.paths):,} tracks")

    resolved = [i for i in to_pool_indices(recon, mip_songs) if i is not None]
    print(f"reconciliation: {len(resolved):,}/{len(mip_songs):,} MusicIP tracks map into our pool "
          f"({100 * len(resolved) // max(1, len(mip_songs))}%)")

    seed = mip_songs[0]
    mix = eng.similar(seed, size=8)
    hits = [i for i in to_pool_indices(recon, mix) if i is not None]
    print(f"sample mix seed: ...{relkey(seed)}")
    print(f"  MusicIP returned {len(mix)} tracks; {len(hits)} resolved to our pool")
    for p in mix[:8]:
        idx = recon.get(relkey(p))
        lab = (f"{v2.meta[v2.paths[idx]].get('artist','?')} - {v2.meta[v2.paths[idx]].get('title','?')}"
               if idx is not None else "(not in our pool)")
        print(f"    {lab}")
