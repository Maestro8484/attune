"""Attune export — turn a mix into something you can actually play.

Two targets, one path-map:
  * .m3u8 file  — portable playlist; paths rewritten to a chosen "flavor"
                  (local drive / UNC share / Plex server path).
  * Plex        — creates a real playlist on your Plex server via its HTTP API.

The three path roots (LOCAL_LIBRARY_ROOT / UNC_LIBRARY_ROOT / PLEX_LIBRARY_ROOT) live in
settings.json (Preferences -> Advanced) as of PROPOSAL_EXPORT_ROOTS_2026-07-28.md -- see
mapper_from_settings() and derive_local_root(). A .env file is a dev override for those
same three, plus wherever Plex's real connection secrets live:
  PLEX_URL, PLEX_ACCOUNT_TOKEN, PLEX_SECTION_KEY, PLEX_MACHINE_ID  (never in settings.json)
mapper_from_env()/load_env() below still exist for the CLI, which is a dev tool run from a
workspace checkout and keeps reading straight from .env.

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

    Export roots (LOCAL/UNC/PLEX_LIBRARY_ROOT) no longer depend on this chain finding
    anything -- they live in settings.json now (see mapper_from_settings()), which every
    install can always find regardless of cwd. What's left behind this search is a dev
    override for those same three roots, plus Plex's actual connection secrets
    (PLEX_URL, PLEX_ACCOUNT_TOKEN, ...), which never move out of .env.

    No repo-root or app-root fallback is added on purpose
    (PROPOSAL_EXPORT_ROOTS_2026-07-28.md §5): once settings.json is the answer for an
    installed app, a developer's .env sitting near that install would otherwise silently
    override the user's own Preferences, and precedence would depend on install layout --
    the original bug wearing a different hat. This is why a process whose cwd is outside
    the workspace (a scheduled task starting in System32, an installed copy under Program
    Files) still finds no .env: that's correct, not a gap."""
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

    def convert_safe(self, path, flavor):
        """Like convert(), but never raises for an unconfigured root: falls back to LOCAL
        paths instead of hard-failing. An unconfigured UNC/Plex root used to 400 the whole
        export with an empty file (PROPOSAL_EXPORT_ROOTS_2026-07-28.md, Ruling 2 Amended
        item 3) -- callers should never see ValueError from this path anymore.

        Returns (converted_path, used_flavor); used_flavor differs from `flavor` only when
        a fallback happened, so the caller can report it."""
        if flavor == "unc" and not self.unc_root:
            return self.to_local(path), "local"
        if flavor == "plex" and not self.plex_root:
            return self.to_local(path), "local"
        return self.convert(path, flavor), flavor


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
    # CLI-only path (see main() below). The web app builds its mapper through
    # mapper_from_settings() instead, so settings.json (Preferences) is consulted too.
    # LOCAL_LIBRARY_ROOT should match the path prefix as stored in your DB; the generic
    # default is only a placeholder — set it in .env for exports to map correctly.
    return PathMapper(cfg.get("LOCAL_LIBRARY_ROOT", "C:\\Music"),
                      cfg.get("UNC_LIBRARY_ROOT"), cfg.get("PLEX_LIBRARY_ROOT"))


def derive_local_root(paths, folders=None):
    """Best-effort default for local_library_root when nothing configured it: the common
    directory prefix across the DB's own track paths. A DB with tracks necessarily has
    paths, unlike `library_folders`, which is empty on today's real settings.json
    (verified 2026-07-28 -- PROPOSAL_EXPORT_ROOTS_2026-07-28.md §3). `folders` is
    consulted only as a SECONDARY source, when the path-based derivation comes up empty.

    Returns '' if nothing usable is found (no paths, or paths that share no common
    directory at all -- e.g. libraries split across multiple drive letters)."""
    sample = [p for p in paths if p][:5000]      # a shared prefix across a few thousand
                                                   # tracks is as good as checking all of them
    if not sample:
        sample = [f for f in (folders or []) if f]
    if not sample:
        return ""
    try:
        common = os.path.commonpath(sample)
    except ValueError:      # mixed drive letters on Windows -> no common path at all
        return ""
    # commonpath of a single file (or of paths that already share their full directory)
    # returns that file's own path, not its parent directory -- fall back to dirname.
    return common if os.path.isdir(common) else os.path.dirname(common)


def mapper_from_settings(settings, env_cfg, paths=None):
    """Build the PathMapper the web app actually uses. settings.json (Preferences) is the
    home for the three export roots; env_cfg (a loaded .env, see load_env()) is a dev
    override that still wins over settings.json day to day -- there's no CLI leg here,
    so this is env_cfg > settings.json > derived default, the non-CLI tail of
    config.effective()'s chain. UNC and Plex are never guessed -- only local_library_root
    has a derived fallback (see derive_local_root)."""
    local = (env_cfg.get("LOCAL_LIBRARY_ROOT") or settings.get("local_library_root")
            or derive_local_root(paths or [], settings.get("library_folders")))
    unc = env_cfg.get("UNC_LIBRARY_ROOT") or settings.get("unc_library_root") or ""
    plex = env_cfg.get("PLEX_LIBRARY_ROOT") or settings.get("plex_library_root") or ""
    return PathMapper(local or "C:\\Music", unc, plex)


def plex_from_env(cfg, mapper):
    """Build a PlexExporter. PLEX_URL/ACCOUNT_TOKEN/MACHINE_ID are connection secrets and
    only ever come from .env (they never move to settings.json). The library-path root is
    read off `mapper.plex_root` instead of straight off `cfg`, so a root configured only
    in Preferences (settings.json) still satisfies this check -- mapper is already
    resolved through mapper_from_settings()'s env-then-settings chain by the caller."""
    need = ("PLEX_URL", "PLEX_ACCOUNT_TOKEN", "PLEX_MACHINE_ID")
    missing = [k for k in need if not cfg.get(k)]
    if not mapper.plex_root:
        missing.append("PLEX_LIBRARY_ROOT (.env) or plex_library_root (Preferences)")
    if missing:
        raise SystemExit(f"Plex export needs: {', '.join(missing)}")
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
