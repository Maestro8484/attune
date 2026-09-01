"""Attune export — turn a mix into something you can actually play.

Two targets, one path-map:
  * .m3u8 file  — portable playlist; paths rewritten to a chosen "flavor"
                  (local drive / UNC share / Plex server path).
  * Plex        — creates a real playlist on your Plex server via its HTTP API.

The three path roots (LOCAL_LIBRARY_ROOT / UNC_LIBRARY_ROOT / PLEX_LIBRARY_ROOT) live in
settings.json (Preferences -> Advanced) as of PROPOSAL_EXPORT_ROOTS_2026-07-28.md -- see
mapper_from_settings(). None of the three is ever guessed: an unset root, or a track
stored outside it, REFUSES the rewrite and the export keeps local paths with a notice
(ruling (a), MORNING_REPORT_2026-07-29.md §5.1 -- see PathMapper and convert_all).
suggest_local_root() exists only to SHOW the operator a candidate; nothing applies it.
A .env file is a dev override for those same three, plus where Plex's real secrets live:
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
    the original bug wearing a different hat.

    The walk only accepts a .env whose directory is Attune-shaped (an `attune`
    checkout beside it, or a pyproject.toml/.git marking a repo root). Without that
    check the walk reaches the drive root and adopts ANY machine-level .env -- a real
    drive-root .env belonging to an unrelated tool bit here (HANDOFF_RESUME S15/S17). So a
    process whose cwd is outside an Attune workspace (a scheduled task starting in
    System32, an installed copy under Program Files) finds no .env: correct, and now
    actually true. ATTUNE_ENV stays the explicit escape hatch."""
    for cand in (explicit, os.environ.get("ATTUNE_ENV")):
        if cand and os.path.exists(cand):
            return cand
    d = os.getcwd()
    while True:
        p = os.path.join(d, ".env")
        if os.path.exists(p) and _attune_shaped(d):
            return p
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _attune_shaped(d):
    # Guard for find_env's upward walk: a .env only counts inside a dev tree tied
    # to Attune (workspace root holding an attune/ checkout, or a repo root). Bare
    # ancestors like C:\ or a home dir carry none of these markers.
    return (os.path.isdir(os.path.join(d, "attune"))
            or os.path.exists(os.path.join(d, "pyproject.toml"))
            or os.path.exists(os.path.join(d, ".git")))


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

    NOTHING HERE IS EVER GUESSED (ruling (a), 2026-07-29 -- MORNING_REPORT §5.1). Two
    cases refuse the rewrite instead of producing something plausible:

      * local_root is not set at all -> refuse. It used to be derived from the common
        prefix of the DB's own paths, which on the operator's real library came out one
        level too deep and sent 21,236 of 21,236 exports to a wrong network path while
        the server reported success. A loud refusal beats a silent wrong answer.
      * a single path does not sit under local_root and does not match the library's
        leaf folder either -> refuse THAT path. The old code pasted the whole local path
        after the target root, which is how a drive letter ended up in the middle of a
        UNC path (\\\\DiskStation\\music\\Music Library\\M:\\Music Library\\...).

    Refusal is a ValueError. convert_all() turns it into "this track kept its local path,
    and here is why", so an export still completes and the UI can say what happened.
    """

    def __init__(self, local_root, unc_root=None, plex_root=None):
        self.local_root = (local_root or "").rstrip("\\/")
        self.unc_root = (unc_root or "").rstrip("\\/")
        self.plex_root = (plex_root or "").rstrip("/")

    def _relative(self, path):
        """Return the path tail *after* the library root, as forward-slash segments.

        Raises ValueError when the tail cannot be established honestly -- see the class
        docstring. Never returns the whole path as a "tail"."""
        if not self.local_root:
            raise ValueError(
                "your local library root is not set, and Attune will not guess it. "
                "Set it in Preferences, Advanced: it is the folder your music sits "
                "under, spelled exactly the way the library stores it.")
        norm = path.replace("\\", "/")
        root = self.local_root.replace("\\", "/")
        nl, rl = norm.lower(), root.lower()
        # require an exact root or a real path boundary after it, so a sibling like
        # '.../Music Library Backup/...' is NOT treated as inside '.../Music Library'.
        if nl == rl or nl.startswith(rl + "/"):
            return norm[len(root):].lstrip("/")
        # not under local_root (e.g. an old, un-rekeyed drive/mount): fall back to
        # whatever follows the library's own leaf folder name (derived, not hardcoded).
        leaf = rl.rstrip("/").rsplit("/", 1)[-1]
        i = nl.rfind(leaf + "/") if leaf else -1
        if i < 0:
            raise ValueError(
                f"this track is not stored under your local library root "
                f"({self.local_root}), so its path cannot be rewritten")
        return norm[i + len(leaf) + 1:].lstrip("/")

    def to_local(self, path):
        return path

    def to_unc(self, path):
        if not self.unc_root:
            raise ValueError("the network folder other players see is not set yet, "
                             "so playlist paths cannot point at it "
                             "(Preferences, Advanced: UNC library root)")
        return self.unc_root + "\\" + self._relative(path).replace("/", "\\")

    def to_plex(self, path):
        if not self.plex_root:
            raise ValueError("the library path your Plex server uses is not set yet, "
                             "so playlist paths cannot point at it "
                             "(Preferences, Advanced: Plex library root)")
        return self.plex_root + "/" + self._relative(path)

    def coverage(self, paths):
        """How many of `paths` this mapper can actually rewrite: {"ok": n, "total": n}.

        The half of ruling (a) that refusing-to-guess does NOT cover: a root the operator
        typed themselves, one level off. Those paths sit under it, so every rewrite
        succeeds and every result is wrong -- the same silent failure as the old derived
        guess, just hand-entered. Counting up front turns it into something the window can
        say before an export happens, and it is a string prefix test per track.

        Counted with _relative(), not a prefix test of our own, so this can never disagree
        with what a real export will do."""
        ok = 0
        for p in paths:
            try:
                self._relative(p)
                ok += 1
            except ValueError:
                pass
        return {"ok": ok, "total": len(paths)}

    def convert(self, path, flavor):
        return {"local": self.to_local, "unc": self.to_unc, "plex": self.to_plex}[flavor](path)

    def convert_all(self, paths, flavor):
        """Convert a whole playlist's paths at once, and report ONCE what happened.

        Never raises. Any path this mapper cannot rewrite honestly -- an unconfigured
        target root, an unset local root, a track stored outside the local root -- is
        handed back as the LOCAL path, and the report says so. That is ruling (a) of
        MORNING_REPORT §5.1: refuse to rewrite, hand back local paths, be visible about
        it. An unconfigured root used to 400 the whole export with an empty file
        (PROPOSAL_EXPORT_ROOTS_2026-07-28.md, Ruling 2 Amended item 3); a wrong local
        root used to succeed silently, which was worse.

        Returns (converted_paths, report):
          requested       the flavor asked for
          used            'unc'/'plex'/'local' -- or 'mixed' when only some paths fell back
          fallback        True if any path kept its local form
          fallback_count  how many, out of `total`
          reason          the first refusal's own message, for the UI to show verbatim

        Both export routes (download .m3u8 and save-to-playlist-folder) go through this
        one method, so they can no longer disagree about the same mix -- MORNING_REPORT
        §5.2's "two adjacent buttons disagree"."""
        out, reason, fell_back = [], "", 0
        for p in paths:
            try:
                out.append(self.convert(p, flavor))
            except ValueError as e:
                out.append(self.to_local(p))
                fell_back += 1
                reason = reason or str(e)
        used = flavor if not fell_back else ("local" if fell_back == len(out) else "mixed")
        return out, {"requested": flavor, "used": used, "fallback": bool(fell_back),
                     "fallback_count": fell_back, "total": len(out), "reason": reason}


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

    def _put(self, path, params=None):
        return self._verb("PUT", path, params)

    def _delete(self, path, params=None):
        return self._verb("DELETE", path, params)

    def _verb(self, method, path, params=None):
        """PUT/DELETE share _post's shape; Plex answers DELETE with 204 and no body."""
        p = dict(params or {})
        p["X-Plex-Token"] = self.token
        req = urllib.request.Request(f"{self.url}{path}?{urllib.parse.urlencode(p)}",
                                     method=method, headers={"Accept": "application/json"})
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

    def build_meta_index(self, page=2000, progress=None):
        """Page the same music section, keeping METADATA as well as the file paths.

        ``build_index`` above answers "what is the ratingKey for this exact server
        path", which is all a root-prefix export needs. Matching a folder of loose
        copies (src/plexmatch.py) needs the other columns too -- title, artist,
        album and duration -- because those files are NOT under the library root and
        their paths are not translatable. Same request, same paging, wider projection,
        so the two never disagree about what the library contains.

        ``orig`` is Plex's originalTitle: on a compilation, grandparentTitle is
        "Various Artists" and originalTitle is the real track artist.

        Returns a list of dicts: rk / title / artist / album / orig / dur (ms) / files.
        """
        rows, start = [], 0
        while True:
            d = self._get(f"/library/sections/{self.section_key}/all",
                          {"type": 10, "X-Plex-Container-Start": start,
                           "X-Plex-Container-Size": page})
            mc = d.get("MediaContainer", {})
            meta = mc.get("Metadata", [])
            for t in meta:
                files = [prt["file"] for m in t.get("Media", [])
                         for prt in m.get("Part", []) if prt.get("file")]
                rows.append({"rk": t.get("ratingKey"), "title": t.get("title"),
                             "artist": t.get("grandparentTitle"),
                             "album": t.get("parentTitle"),
                             "orig": t.get("originalTitle"),
                             "dur": t.get("duration"), "files": files})
            start += len(meta)
            total = mc.get("totalSize", mc.get("size", 0))
            if progress:
                progress(start, total)
            if not meta or start >= total:
                break
        return rows

    # ---------------------------------------------------------------- playlists
    def _library_uri(self, keys):
        return (f"server://{self.machine_id}/com.plexapp.plugins.library/"
                f"library/metadata/{','.join(str(k) for k in keys)}")

    def web_url(self, playlist_key):
        """A link that opens this playlist in Plex's own web app.

        The point is human confirmation: Attune saying "128 tracks" is Attune's word for
        it, and the operator should not have to take it. This link opens the real
        playlist on the real server, so the check is his eyes on Plex, not our number.

        Verified against this server 2026-09-01: GET /web/index.html answers 200 with the
        Plex Web app, and this is the fragment form Plex Web itself uses for a playlist.
        """
        key = urllib.parse.quote(f"/playlists/{playlist_key}", safe="")
        return f"{self.url}/web/index.html#!/server/{self.machine_id}/playlist?key={key}"

    def rescan(self, path=None):
        """Ask Plex to re-read the library (or one folder of it) from disk.

        Needed because Plex only knows about files it has scanned: a track restored to
        the library five minutes ago is genuinely absent from its index, and this
        module would honestly but uselessly report it as "not in your library". A scan
        of a 21,000-track section takes minutes and runs in the background -- the server
        answers immediately with an EMPTY BODY, which is why this goes through _verb
        rather than _get (the latter assumes JSON and raises on an empty response).
        """
        params = {"path": path} if path else None
        self._verb("GET", f"/library/sections/{self.section_key}/refresh", params)
        return True

    def scan_activity(self):
        """(scanning, percent) for this server, or (False, None) when idle.

        Read off /activities, which is where Plex reports the background scan it starts;
        without it a caller cannot tell "scan finished, the track really is missing" from
        "scan still running, ask again in a minute" -- two answers that need opposite
        reactions from the operator.
        """
        try:
            d = self._get("/activities")
        except Exception:                                   # noqa: BLE001
            return False, None
        for a in d.get("MediaContainer", {}).get("Activity", []):
            if "library.update" in (a.get("type") or ""):
                return True, a.get("progress")
        return False, None

    def find_playlist(self, title):
        """The audio playlist with exactly this title, or None. Title match is exact.

        Deliberately not fuzzy and deliberately not "starts with": picking the wrong
        existing playlist would rewrite somebody's real list. Exact or create-new.
        """
        d = self._get("/playlists", {"playlistType": "audio"})
        for pl in d.get("MediaContainer", {}).get("Metadata", []):
            if pl.get("title") == title:
                return pl
        return None

    def playlist_items(self, playlist_key):
        """[(playlistItemID, ratingKey)] in playlist order.

        playlistItemID is NOT the ratingKey: it identifies this track's slot in this
        playlist, and it is what DELETE .../items/<id> takes. Removing by ratingKey
        does not work.
        """
        d = self._get(f"/playlists/{playlist_key}/items")
        return [(m.get("playlistItemID"), str(m.get("ratingKey")))
                for m in d.get("MediaContainer", {}).get("Metadata", [])]

    def set_order(self, playlist_key, keys, progress=None):
        """Put `keys` at the front of the playlist, in that order. Returns True when the
        running order read back off the server matches.

        Empty the playlist, then re-add it in the order wanted. Two HTTP calls, and the
        result is exactly what was asked for because Plex keeps a playlist in the order
        things were added -- the same property that already makes a NEWLY created
        playlist come out right.

        ANYTHING ALREADY IN THE PLAYLIST THAT IS NOT IN `keys` IS KEPT, after `keys`, in
        its existing relative order. Ordering is not pruning. The first version of this
        re-added only `keys`, which meant the "only add, never remove" mode quietly
        deleted every track the folder did not contain and reported `removed: 0` -- three
        of six in the scratch test that caught it (2026-09-01). Whether to drop those
        tracks is the caller's decision, made through `prune`, never a side effect here.

        This replaced a move-by-move implementation, and the reason is worth keeping.
        Plex does support moving one item at a time
        (``PUT /playlists/<k>/items/<playlistItemID>/move``, optionally ``after=<pid>``),
        and on a small playlist it is exactly right: a full reversal of 8 items landed
        perfectly in 7 moves. At real size it did not. A full reversal of 127 items took
        237 moves across two passes and still came out wrong from position 15 onward.
        Rather than keep tuning an algorithm whose failure is invisible unless you check,
        this does the thing that cannot be subtly wrong. Measured: 127 tracks reordered in
        0.1 s, byte-exact.

        The playlist's own ratingKey is untouched, so every link and every client pointing
        at it keeps working -- only the internal slot ids change, and nothing outside this
        server holds those.

        `progress` is called once at the end; there is no per-item stage left to report.
        """
        want = [str(k) for k in keys]
        if not want:
            return False
        wset = set(want)
        extras = [rk for _pid, rk in self.playlist_items(playlist_key) if rk not in wset]
        full = want + extras
        self._delete(f"/playlists/{playlist_key}/items")
        self._put(f"/playlists/{playlist_key}/items", {"uri": self._library_uri(full)})
        if progress:
            progress(len(full), len(full))
        return self.order_matches(playlist_key, full)

    def order_matches(self, playlist_key, keys):
        """True when the playlist's running order really is `keys`.

        Read back off the server and compared, so "it is in the order you asked for" is
        an observation rather than a claim about the requests that were sent.
        """
        want = [str(k) for k in keys]
        have = [rk for _pid, rk in self.playlist_items(playlist_key)]
        wset, hset = set(want), set(have)
        return [rk for rk in have if rk in wset] == [rk for rk in want if rk in hset]

    def sync_playlist(self, title, keys, prune=True, order=False, progress=None):
        """Make the playlist called `title` hold exactly `keys`, and say what moved.

        Add-then-remove against the EXISTING playlist rather than delete-and-recreate,
        because the playlist is a thing the operator has already put on a phone and in
        a car -- recreating it mints a new ratingKey and every client's reference to it
        goes stale. Rotation is the whole point of this feature, so it has to survive
        being rotated.

        Verified against Plex Media Server 1.43.4 on 2026-09-01: PUT .../items with a
        uri listing the full desired set adds only the ones missing (the server
        de-duplicates), and DELETE .../items/<playlistItemID> removes exactly one slot.

        `prune=False` makes this add-only, for "top up the car list, keep what is
        already there".

        Returns {created, playlist, before, after, added, removed}.
        """
        want = [str(k) for k in keys]
        pl = self.find_playlist(title)
        created = False
        if pl is None:
            if not want:
                return {"created": False, "playlist": None, "before": 0, "after": 0,
                        "added": 0, "removed": 0,
                        "error": "nothing to put in the playlist, so none was created"}
            resp = self._post("/playlists", {"type": "audio", "title": title,
                                             "smart": 0, "uri": self._library_uri(want)})
            md = resp.get("MediaContainer", {}).get("Metadata", [{}])
            pl = md[0] if md else {}
            created = True
        key = pl.get("ratingKey")
        before = self.playlist_items(key)
        have = {rk for _pid, rk in before}
        missing = [k for k in want if k not in have]
        if missing:
            self._put(f"/playlists/{key}/items", {"uri": self._library_uri(missing)})
        removed = 0
        if prune:
            keep = set(want)
            for pid, rk in self.playlist_items(key):
                if rk not in keep:
                    self._delete(f"/playlists/{key}/items/{pid}")
                    removed += 1
        # A freshly created playlist is already in the order it was built in, so only an
        # existing one that just gained tracks needs the moves -- new tracks land on the
        # end otherwise, which is exactly the "why is the new stuff all at the bottom"
        # complaint this answers.
        ordered = None
        if order:
            # A newly created playlist is already in build order, so it only needs
            # checking, not rewriting. An existing one that just gained tracks has them
            # all stuck on the end -- the "why is the new stuff at the bottom" case this
            # answers -- so it gets rewritten in order.
            # Either way `ordered` is READ BACK off the server, never assumed.
            ordered = (self.order_matches(key, want) if created
                       else self.set_order(key, want, progress=progress))
        # Read the playlist back off the server rather than reporting what we sent.
        # "128 added" is a claim about a request; `after` is what Plex actually holds.
        after = self.playlist_items(key)
        return {"created": created, "playlist": key, "title": title,
                "before": len(before), "after": len(after),
                "added": len(missing), "removed": removed, "ordered": ordered,
                "web_url": self.web_url(key),
                "final_keys": [rk for _pid, rk in after]}

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
    # LOCAL_LIBRARY_ROOT must match the path prefix as stored in your DB. Unset is left
    # unset -- the old "C:\Music" placeholder default made a wrong rewrite look like a
    # working one, which is the exact failure ruling (a) closed off; the CLI now exits
    # with the mapper's own message instead.
    return PathMapper(cfg.get("LOCAL_LIBRARY_ROOT", ""),
                      cfg.get("UNC_LIBRARY_ROOT"), cfg.get("PLEX_LIBRARY_ROOT"))


def suggest_local_root(paths, folders=None):
    """A SUGGESTION for local_library_root, to show the operator. NEVER applied.

    Was `derive_local_root`, and was wired in as a silent default until ruling (a) of
    MORNING_REPORT §5.1 (2026-07-29) took that job away from it. Two changes came with
    the demotion:

      * it reads EVERY path, not the first 5,000. The 5,000 cap is precisely what made
        the old default wrong on the operator's library -- the first few thousand paths
        were all albums, so the "common" prefix came out as ...\\Music Library\\Albums
        and every single export went to a wrong network path.
      * a suggestion that does not cover the whole library is no suggestion, so anything
        short of a prefix EVERY path sits under returns ''.

    Returns '' when there is no such prefix -- including the real case of one stray track
    on another drive (measured 2026-07-29: 21,452 of 21,453 paths under
    L:\\_MUSIC\\Music Library, one under M:\\). '' means "Attune has nothing honest to
    offer here, ask the operator", which is the whole point of ruling (a).

    `folders` (settings.json's library_folders) is a SECONDARY source, used only when
    there are no paths at all -- it is empty on today's real settings.json (verified
    2026-07-28, PROPOSAL_EXPORT_ROOTS_2026-07-28.md §3)."""
    sample = [p for p in paths if p]
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


def mapper_from_settings(settings, env_cfg):
    """Build the PathMapper the web app actually uses. settings.json (Preferences) is the
    home for the three export roots; env_cfg (a loaded .env, see load_env()) is a dev
    override that still wins over settings.json day to day -- there's no CLI leg here,
    so this is env_cfg > settings.json, the non-CLI tail of config.effective()'s chain.

    There is no third leg any more. None of the three roots is guessed, derived or
    defaulted (ruling (a), MORNING_REPORT §5.1): unset means unset, and PathMapper
    refuses to rewrite rather than inventing a plausible root. `paths` used to be
    threaded in here for the derivation -- callers now pass the DB's paths to
    suggest_local_root() themselves if they want something to SHOW the operator."""
    return PathMapper(env_cfg.get("LOCAL_LIBRARY_ROOT") or settings.get("local_library_root") or "",
                      env_cfg.get("UNC_LIBRARY_ROOT") or settings.get("unc_library_root") or "",
                      env_cfg.get("PLEX_LIBRARY_ROOT") or settings.get("plex_library_root") or "")


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
