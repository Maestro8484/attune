"""Attune export — turn a mix into something you can actually play.

Two targets, one path-map:
  * .m3u8 file  — portable playlist; paths rewritten to a chosen "flavor"
                  (local drive / UNC share / Plex server path).
  * Plex        — creates a real playlist on your Plex server via its HTTP API.

Everything is config-driven from a .env file (no secrets in code). Keys used:
  LOCAL_LIBRARY_ROOT   e.g.  D:\\Music              (paths as stored in the DB)
  UNC_LIBRARY_ROOT     e.g.  \\\\NAS\\Music           (for playlists played on other LAN devices)
  PLEX_LIBRARY_ROOT    e.g.  /music                 (paths as your Plex server sees them)
  PLEX_URL, PLEX_ACCOUNT_TOKEN, PLEX_SECTION_KEY, PLEX_MACHINE_ID

The library is a mirror: the tree BELOW the root is identical across local / NAS /
Plex, so translation is a pure root-prefix swap (+ slash normalisation).

CLI:
    python src/export.py --db ../mixer-ng/data/mixer.db --seed "black dog" --to m3u
    python src/export.py --db ../mixer-ng/data/mixer.db --seed "black dog" --to plex
"""
import argparse
import importlib.util
import json
import os
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))


# --------------------------------------------------------------------------- env
def find_env(explicit=None):
    """Locate a .env: explicit arg → $ATTUNE_ENV → walk up from cwd → None if not found.

    No repo-root fallback exists: a process whose cwd is outside the workspace (e.g.
    a scheduled task starting in System32, or an installed copy under Program Files)
    gets no .env and therefore no UNC/Plex export roots. Root-caused S12 2026-07-28
    (nightly gate 400); the settings-based fix is a queued design item."""
    for cand in (explicit, os.environ.get("ATTUNE_ENV")):
        if cand and os.path.exists(cand):
            return cand
    d = os.getcwd()
    while True:
        p = os.path.join(d, ".env")
        if os.path.exists(p):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def load_env(explicit=None):
    path = find_env(explicit)
    cfg = {}
    if not path:
        return cfg
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    return cfg


# ----------------------------------------------------------------------- paths
class PathMapper:
    """Rewrite a local library path to a chosen flavor by swapping the root prefix.

    If a path doesn't sit under local_root (e.g. a stray path left over from an earlier
    setup on a different drive/mount), it falls back to whatever follows the library's
    leaf folder name, so a mirrored tree still lines up.
    """

    def __init__(self, local_root, unc_root=None, plex_root=None):
        self.local_root = local_root.rstrip("\\/")
        self.unc_root = (unc_root or "").rstrip("\\/")
        self.plex_root = (plex_root or "").rstrip("/")

    def _relative(self, path):
        """Return the path tail *after* the library root, as forward-slash segments."""
        norm = path.replace("\\", "/")
        root = self.local_root.replace("\\", "/")
        nl, rl = norm.lower(), root.lower()
        # require an exact root or a real path boundary after it, so a sibling like
        # '.../Music Library Backup/...' is NOT treated as inside '.../Music Library'.
        if nl == rl or nl.startswith(rl + "/"):
            tail = norm[len(root):]
        else:
            # not under local_root (e.g. an old, un-rekeyed drive/mount): fall back to
            # whatever follows the library's own leaf folder name (derived, not hardcoded).
            leaf = rl.rstrip("/").rsplit("/", 1)[-1]
            i = nl.rfind(leaf + "/") if leaf else -1
            tail = norm[i + len(leaf) + 1:] if i >= 0 else norm
        return tail.lstrip("/")

    def to_local(self, path):
        return path

    def to_unc(self, path):
        if not self.unc_root:
            raise ValueError("UNC_LIBRARY_ROOT is not set — cannot write UNC playlist paths")
        return self.unc_root + "\\" + self._relative(path).replace("/", "\\")

    def to_plex(self, path):
        if not self.plex_root:
            raise ValueError("PLEX_LIBRARY_ROOT is not set — cannot map to Plex paths")
        return self.plex_root + "/" + self._relative(path)

    def convert(self, path, flavor):
        return {"local": self.to_local, "unc": self.to_unc, "plex": self.to_plex}[flavor](path)


# ------------------------------------------------------------------------- m3u8
def _oneline(s):
    """Collapse CR/LF so a stray newline in a tag can't inject extra m3u directives."""
    return (s or "").replace("\r", " ").replace("\n", " ")


def write_m3u8(items, outfile, mapper, flavor="unc"):
    """items: list of dicts with keys path, artist, title, (secs optional)."""
    lines = ["#EXTM3U"]
    for it in items:
        secs = int(it.get("secs") or -1)
        artist = _oneline(it.get("artist") or "?")
        title = _oneline(it.get("title") or os.path.basename(it["path"]))
        lines.append(f"#EXTINF:{secs},{artist} - {title}")
        lines.append(mapper.convert(it["path"], flavor))
    with open(outfile, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return outfile


# -------------------------------------------------------------------------- plex
class PlexExporter:
    def __init__(self, url, token, section_key, machine_id, mapper, timeout=30):
        self.url = url.rstrip("/")
        self.token = token
        self.section_key = str(section_key)
        self.machine_id = machine_id
        self.mapper = mapper
        self.timeout = timeout
        self._index = None                      # {plex_path: ratingKey}

    def _get(self, path, params=None):
        params = dict(params or {})
        params["X-Plex-Token"] = self.token
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.url}{path}?{q}", headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.load(r)

    def _post(self, path, params):
        params = dict(params)
        params["X-Plex-Token"] = self.token
        q = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{self.url}{path}?{q}", method="POST",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            body = r.read()
            return json.loads(body) if body else {}

    def build_index(self, page=2000, progress=None):
        """Page the whole music section once, mapping server file path -> ratingKey."""
        index, start = {}, 0
        while True:
            d = self._get(f"/library/sections/{self.section_key}/all",
                          {"type": 10, "X-Plex-Container-Start": start,
                           "X-Plex-Container-Size": page})
            mc = d.get("MediaContainer", {})
            meta = mc.get("Metadata", [])
            for t in meta:
                for m in t.get("Media", []):
                    for prt in m.get("Part", []):
                        f = prt.get("file")
                        if f:
                            index[f] = t["ratingKey"]
            start += len(meta)
            total = mc.get("totalSize", mc.get("size", 0))
            if progress:
                progress(start, total)
            if not meta or start >= total:
                break
        self._index = index
        return index

    def match(self, local_paths):
        """Return (rating_keys_in_order, missed_local_paths)."""
        if self._index is None:
            self.build_index()
        keys, missed = [], []
        for p in local_paths:
            rk = self._index.get(self.mapper.to_plex(p))
            (keys.append(rk) if rk else missed.append(p))
        return keys, missed

    def create_playlist(self, title, local_paths):
        keys, missed = self.match(local_paths)
        if not keys:
            return {"ok": False, "error": "no tracks matched in Plex", "missed": missed}
        uri = (f"server://{self.machine_id}/com.plexapp.plugins.library/"
               f"library/metadata/{','.join(str(k) for k in keys)}")
        resp = self._post("/playlists", {"type": "audio", "title": title, "smart": 0, "uri": uri})
        pl = resp.get("MediaContainer", {}).get("Metadata", [{}])
        return {"ok": True, "matched": len(keys), "missed": missed,
                "playlist": pl[0].get("ratingKey") if pl else None, "title": title}


# --------------------------------------------------------------------------- glue
def _load_hybrid():
    spec = importlib.util.spec_from_file_location("hybrid", os.path.join(HERE, "hybrid.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def mapper_from_env(cfg):
    # LOCAL_LIBRARY_ROOT should match the path prefix as stored in your DB; the generic
    # default is only a placeholder — set it in .env for exports to map correctly.
    return PathMapper(cfg.get("LOCAL_LIBRARY_ROOT", "C:\\Music"),
                      cfg.get("UNC_LIBRARY_ROOT"), cfg.get("PLEX_LIBRARY_ROOT"))


def plex_from_env(cfg, mapper):
    need = ("PLEX_URL", "PLEX_ACCOUNT_TOKEN", "PLEX_MACHINE_ID", "PLEX_LIBRARY_ROOT")
    missing = [k for k in need if not cfg.get(k)]
    if missing:
        raise SystemExit(f"Plex export needs these .env keys: {', '.join(missing)}")
    return PlexExporter(cfg["PLEX_URL"], cfg["PLEX_ACCOUNT_TOKEN"],
                        cfg.get("PLEX_SECTION_KEY", "1"), cfg["PLEX_MACHINE_ID"], mapper)


def main():
    ap = argparse.ArgumentParser(description="Export an Attune mix to .m3u8 or Plex.")
    ap.add_argument("--db", required=True)
    ap.add_argument("--seed", required=True, help="song/artist substring to seed the mix")
    ap.add_argument("--size", type=int, default=25)
    ap.add_argument("--to", choices=["m3u", "plex", "both"], default="m3u")
    ap.add_argument("--flavor", choices=["local", "unc", "plex"], default="unc",
                    help="path form written into the .m3u8 (default: unc, for LAN devices)")
    ap.add_argument("--out", help="output .m3u8 path (default: ./Attune-<seed>.m3u8)")
    ap.add_argument("--env", help="path to .env (default: auto-discover)")
    args = ap.parse_args()

    cfg = load_env(args.env)
    mapper = mapper_from_env(cfg)

    hybrid = _load_hybrid()
    eng = hybrid.HybridEngine(args.db)
    hits = [p for p in eng.paths
            if args.seed.lower() in f"{eng.meta.get(p, {}).get('artist','')} "
                                    f"{eng.meta.get(p, {}).get('title','')}".lower()]
    if not hits:
        raise SystemExit(f"No track matched '{args.seed}'.")
    seed = hits[0]
    picks = eng.mix(seed, size=args.size) or []
    if not picks:
        raise SystemExit("Engine returned no mix for that seed.")
    tracks = [seed] + picks
    items = [{"path": p, "artist": eng.meta.get(p, {}).get("artist"),
              "title": eng.meta.get(p, {}).get("title")} for p in tracks]
    seedlab = f"{eng.meta.get(seed, {}).get('artist','?')} - {eng.meta.get(seed, {}).get('title','?')}"
    title = f"Attune — like {seedlab}"

    if args.to in ("m3u", "both"):
        if args.out:
            out = args.out                          # user chose the path; respect it verbatim
        else:                                       # generated name: no separators / reserved chars
            stem = eng.meta.get(seed, {}).get("title") or "mix"
            safe = "".join(c for c in stem if c.isalnum() or c in " -_").strip()[:60] or "mix"
            out = f"Attune-{safe}.m3u8"
        try:
            write_m3u8(items, out, mapper, flavor=args.flavor)
        except ValueError as e:
            raise SystemExit(f"Cannot export .m3u8 as '{args.flavor}': {e}")
        print(f"[m3u] wrote {len(items)} tracks ({args.flavor} paths) -> {out}")

    if args.to in ("plex", "both"):
        plex = plex_from_env(cfg, mapper)
        print("[plex] indexing library ...")
        res = plex.create_playlist(title, tracks)
        if res["ok"]:
            print(f"[plex] created '{res['title']}' — {res['matched']} tracks"
                  + (f", {len(res['missed'])} not found in Plex" if res["missed"] else ""))
        else:
            print(f"[plex] FAILED: {res['error']}")


if __name__ == "__main__":
    main()
