"""Attune Studio — the MusicIP-Mixer-shaped desktop view, modernised.

This module adds ONLY what app.py lacks. It does not duplicate mix, export, refine,
explain, or audio streaming: those already exist and are passed in via `ctx`.

What the original MusicIP Mixer GUI gives you, and where it comes from here:

  MusicIP GUI element              -> this module
  ------------------------------------------------------------------------------
  Genres | Artists | Albums panes  -> GET /api/lib/facets     (cascading, with counts)
  centre track table              -> GET /api/lib/tracks      (sort, page, filter)
  Track # column                  -> lazily read from tags (mutagen), cached
  album art + transport           -> GET /api/art             (folder art, then embedded)
  Filters/Playlists tree          -> GET /api/playlists       (a real folder on disk)
  a saved playlist's contents     -> GET /api/playlist
  "Send To" / export              -> POST /api/export/m3u_dir (writes into that folder)
  status bar counts               -> GET /api/lib/stats

Everything is read-only against mixer.db. The single write target is the configured
playlist directory, and nothing outside it can be written (see _safe_out).
"""
from __future__ import annotations

import io
import os
import re
import sqlite3
import threading
import unicodedata
from collections import Counter, defaultdict

from flask import Blueprint, Response, jsonify, request, send_file

# ----------------------------------------------------------------------- helpers

# A filename's leading digits are a track number only if they look like one:
#   "07 - Title.mp3"  -> 7      "001 Nirvana - ....mp3" -> 1
#   "112 - Peaches & Cream.mp3" -> NOT a track number; that is the band *112*.
# So: 1-2 digits, or 3 digits with a leading zero (the compilation-rip convention),
# and always followed by a separator.
_TRACKNO_RE = re.compile(r"^\s*(0\d{2}|\d{1,2})(?=[\s._\-])")
_ART_NAMES = ("cover", "folder", "front", "album", "albumart", "thumb")
_ART_EXT = (".jpg", ".jpeg", ".png", ".webp")


def _relkey(path):
    """Mirror-invariant key for a track: the tail of the path below the library root,
    lowercased with separators normalised, so the same file reached through a UNC share,
    a mapped drive, or a Linux mount collapses to one key.

    We do not know the roots for certain, so we key on the LAST THREE components
    (artist-ish / album-ish / file), which is stable across every mirror of a library
    and is exactly the `relkey` idea musicip_engine.build_reconciler() already relies on.
    """
    p = str(path).replace("/", "\\").rstrip("\\")
    parts = [x for x in p.split("\\") if x]
    return "\\".join(parts[-3:]).lower()


def _seconds_to_len(s):
    s = int(s or 0)
    return f"{s // 60}:{s % 60:02d}"


def _fold(s):
    """accent/case-insensitive compare key for search + sort."""
    s = unicodedata.normalize("NFKD", str(s or ""))
    return "".join(c for c in s if not unicodedata.combining(c)).casefold()


def _split_genres(g):
    """Genre tags are FREE TEXT and often multi-valued: 'Soul; Female Vocalists; British'.

    MusicIP's own Genres pane lists the whole raw string as one entry ("Alt-Country,
    Americana, Singer-Songwriter" is a single row in the screenshots), and we match that:
    splitting on ',' shreds legitimate names and yields garbage facets ("Rock, & Country"
    -> "& Country"). Splitting only on ';' is safe -- no genre name contains a semicolon --
    so we get the useful multi-tag case without the artifacts.
    """
    if not g:
        return []
    return [p.strip() for p in str(g).split(";") if p.strip()]


# ----------------------------------------------------------------------- library index

class LibraryIndex:
    """An in-memory, read-only projection of mixer.db over the engine's pool order.

    Row i here == pool index i in app.py (eng.paths[i]) so every id crossing the wire is
    the SAME integer the existing /api/mix, /audio and export routes already speak.
    """

    def __init__(self, db_path, paths, meta):
        self.paths = paths
        self.n = len(paths)
        self.idx = {p: i for i, p in enumerate(paths)}
        self.by_relkey = {}
        for i, p in enumerate(paths):
            self.by_relkey.setdefault(_relkey(p), i)
        self.by_base = defaultdict(list)
        for i, p in enumerate(paths):
            self.by_base[os.path.basename(p).lower()].append(i)

        self.artist = [""] * self.n
        self.album = [""] * self.n
        self.title = [""] * self.n
        self.genre = [""] * self.n
        self.year = [0] * self.n
        self.seconds = [0] * self.n
        for i, p in enumerate(paths):
            m = meta.get(p) or {}
            self.artist[i] = m.get("artist") or ""
            self.album[i] = m.get("album") or ""
            self.title[i] = m.get("title") or os.path.splitext(os.path.basename(p))[0]
            self.genre[i] = m.get("genre") or ""
            self.year[i] = int(m.get("year") or 0)

        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        idx = {p: i for i, p in enumerate(paths)}
        for p, sec in con.execute("SELECT path, seconds FROM tracks"):
            i = idx.get(p)
            if i is not None:
                self.seconds[i] = int(sec or 0)
        self.analyzed = [False] * self.n
        for (p,) in con.execute(
                "SELECT path FROM features WHERE error IS NULL AND vec IS NOT NULL"):
            i = idx.get(p)
            if i is not None:
                self.analyzed[i] = True
        self.db_tracks = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        self.failures = self._load_failures(con, set(paths))
        con.close()

        # per-track genre TAGS (multi-valued) + folded sort/search keys
        self.gtags = [_split_genres(g) for g in self.genre]
        # a handful of pool tracks have no row in `tracks` (CLAP-only strays): sort them
        # last rather than letting empty strings head every alphabetical view.
        last = "￿"
        self.f_artist = [_fold(a) if a else last for a in self.artist]
        self.f_album = [_fold(a) if a else last for a in self.album]
        self.f_title = [_fold(t) for t in self.title]
        self.f_all = [f"{a} {b} {c}" for a, b, c in zip(self.f_artist, self.f_album, self.f_title)]

        # Cheap track number: the leading digits of the filename, computed with zero I/O.
        # This library is almost entirely named "07 - Title.mp3", so it is nearly always
        # right, and it is the ONLY thing library-wide sorts are allowed to touch --
        # sorting 21k rows by a tag would mean 21k mutagen reads per request.
        self.fno = [0] * self.n
        for i, p in enumerate(paths):
            m = _TRACKNO_RE.match(os.path.basename(p))
            if m:
                v = int(m.group(1))
                self.fno[i] = v if v <= 999 else 0

        # user columns (ratings / play counts) — zeros until userdata.py loads the
        # real values from `usermeta` and overwrites these lists in place.
        self.rating = [0] * self.n
        self.loved = [False] * self.n
        self.plays = [0] * self.n
        self.last_played = [0] * self.n
        self.date_added = [0] * self.n
        # file-presence columns — zeros until libverify.py's FileState loads the real
        # values from `filestate` and overwrites these lists in place (same pattern
        # as the user columns just above).
        self.missing = [False] * self.n
        self.checked_at = [0] * self.n

        self._trackno = {}
        self._trackno_lock = threading.Lock()
        self.total_seconds = sum(self.seconds)
        self.total_bytes = 0
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute("SELECT SUM(bytes) FROM tracks").fetchone()
        con.close()
        self.total_bytes = int(row[0] or 0)

    # -- analysis failures: the tracks the library holds that never reached the mix pool.
    @staticmethod
    def _load_failures(con, pool):
        """Every `tracks` row whose path is NOT in the mixable pool, newest DB order kept.

        MEMBERSHIP IS THE FACT, the reason is a best-effort hint. The pool is built by
        hybrid.py (CLAP vectors intersected with 79-dim librosa vectors, plus finiteness
        and dimension checks this module deliberately does not duplicate -- pool contents
        are mix semantics and gated by LAW 1). So "not in `pool`" is read straight off the
        engine's own path list, and `why` is then reconstructed from the DB columns, with
        an honest "cannot tell from the tables" when they look fine.

        These rows were invisible everywhere before this: absent from the browser, the
        counts, the facets, the folder tree and every smart view, while /api/lib/stats
        reported a 100%-analyzed library because both its numbers came from the pool
        (MORNING_REPORT_2026-07-29.md §4.1 -- 217 tracks on the operator's own library).

        Read ONCE at construction, not per request: it needs a full pass over `tracks`,
        and a freshly scanned track only shows up after a library reload in any case --
        libreload.py rebuilds this whole index, so the list rebuilds with it."""
        feat = {}
        for p, err, hasvec, dim in con.execute(
                "SELECT path, error, vec IS NOT NULL, dim FROM features"):
            feat[p] = (err or "", bool(hasvec), dim)
        clap = {p for (p,) in con.execute("SELECT path FROM clap WHERE vec IS NOT NULL")}
        out = []
        for p, artist, album, title, genre, year, secs in con.execute(
                "SELECT path, artist, album, title, genre, year, seconds FROM tracks"):
            if p in pool:
                continue
            err, hasvec, dim = feat.get(p, ("", False, 0))
            why = []
            if p not in clap:
                why.append("no CLAP embedding")
            if not hasvec:
                why.append(f"librosa analysis failed: {err}" if err else "no librosa features")
            elif err:
                why.append(f"librosa reported: {err}")
            elif dim and dim != 79:
                why.append(f"librosa vector is {dim}-dim, not the 79 the engine expects")
            out.append({
                "path": p,
                "file": os.path.basename(p),
                "artist": artist or "",
                "album": album or "",
                "title": title or os.path.splitext(os.path.basename(p))[0],
                "genre": genre or "",
                "year": int(year or 0) or None,
                "seconds": int(secs or 0),
                "length": _seconds_to_len(secs),
                "why": "; ".join(why) or "cannot tell from the analysis tables",
            })
        return out

    # -- track number: not in mixer.db. Read from tags on demand, only for rows the user
    #    is actually looking at (<=200 at a time), then cache. Falls back to the leading
    #    digits of the filename, which is how most of this library is named anyway.
    def trackno(self, i):
        """The number shown in the Track column. Reads the real tag (cached), falling back
        to the filename's leading digits.

        The filename is NOT trustworthy on its own: `112 - Peaches & Cream.mp3` is by the
        band *112*, not track 112. So the tag wins whenever it exists, and `fno` -- which
        library-wide sorting uses because it costs no I/O -- is only the fallback. Only the
        ~200 rows actually on screen ever pay for a tag read.
        """
        if i in self._trackno:
            return self._trackno[i]
        n = 0
        try:
            import mutagen
            f = mutagen.File(self.paths[i], easy=True)
            if f is not None:
                m = re.match(r"\s*(\d+)", str((f.get("tracknumber") or [""])[0]))
                if m:
                    n = int(m.group(1))
        except Exception:
            n = 0
        if not n:
            n = self.fno[i]
        with self._trackno_lock:
            self._trackno[i] = n
        return n

    # -- filtering ------------------------------------------------------------
    def select(self, genres=(), artists=(), albums=(), q=""):
        """Cascading AND across panes, OR within a pane — exactly the MusicIP behaviour."""
        gset = {g.casefold() for g in genres}
        aset = {a.casefold() for a in artists}
        bset = {b.casefold() for b in albums}
        fq = _fold(q)
        out = []
        for i in range(self.n):
            if gset and not any(t.casefold() in gset for t in self.gtags[i]):
                continue
            if aset and self.artist[i].casefold() not in aset:
                continue
            if bset and self.album[i].casefold() not in bset:
                continue
            if fq and fq not in self.f_all[i]:
                continue
            out.append(i)
        return out

    def facets(self, rows):
        g, a, b = Counter(), Counter(), Counter()
        for i in rows:
            for t in self.gtags[i]:
                g[t] += 1
            if self.artist[i]:
                a[self.artist[i]] += 1
            if self.album[i]:
                b[self.album[i]] += 1
        srt = lambda c: sorted(c.items(), key=lambda kv: _fold(kv[0]))
        return srt(g), srt(a), srt(b)

    # Library-wide sorts use `fno` (zero I/O), never `trackno()` (a tag read per row).
    SORTS = {
        "track": lambda s, i: (s.fno[i], s.f_title[i]),
        "title": lambda s, i: s.f_title[i],
        "artist": lambda s, i: (s.f_artist[i], s.f_album[i], s.fno[i]),
        "album": lambda s, i: (s.f_album[i], s.fno[i]),
        "genre": lambda s, i: (_fold(s.genre[i]) or "￿", s.f_artist[i]),
        "length": lambda s, i: s.seconds[i],
        "year": lambda s, i: s.year[i],
        "rating": lambda s, i: (-s.rating[i], s.f_artist[i], s.f_album[i], s.fno[i]),
        "plays": lambda s, i: (-s.plays[i], -s.last_played[i]),
        "added": lambda s, i: -s.date_added[i],
        "status": lambda s, i: (not s.analyzed[i], s.f_artist[i]),
    }

    def update_row(self, i, fields):
        """Apply edited tag fields to the in-memory index (userdata.write_tags mirrors
        the same values into `tracks`), keeping folded search/sort keys in step."""
        last = "￿"
        if "artist" in fields:
            self.artist[i] = fields["artist"] or ""
            self.f_artist[i] = _fold(self.artist[i]) if self.artist[i] else last
        if "album" in fields:
            self.album[i] = fields["album"] or ""
            self.f_album[i] = _fold(self.album[i]) if self.album[i] else last
        if "title" in fields:
            self.title[i] = fields["title"] or os.path.splitext(
                os.path.basename(self.paths[i]))[0]
            self.f_title[i] = _fold(self.title[i])
        if "genre" in fields:
            self.genre[i] = fields["genre"] or ""
            self.gtags[i] = _split_genres(self.genre[i])
        if "year" in fields and fields["year"]:
            self.year[i] = int(fields["year"])
        self.f_all[i] = f"{self.f_artist[i]} {self.f_album[i]} {self.f_title[i]}"
        self._trackno.pop(i, None)

    def row(self, i, with_trackno=True):
        return {
            "i": i,
            "track": self.trackno(i) if with_trackno else 0,
            "title": self.title[i],
            "artist": self.artist[i],
            "album": self.album[i],
            "genre": self.genre[i],
            "year": self.year[i] or None,
            "length": _seconds_to_len(self.seconds[i]),
            "seconds": self.seconds[i],
            "rating": self.rating[i],
            "loved": self.loved[i],
            "plays": self.plays[i],
            "added": self.date_added[i] or None,
            "status": "Analyzed" if self.analyzed[i] else "Not analyzed",
            "missing": bool(self.missing[i]),
        }


# ----------------------------------------------------------------------- album art

class ArtCache:
    def __init__(self, limit=400):
        self.d = {}
        self.limit = limit
        self.lock = threading.Lock()

    def get(self, key):
        return self.d.get(key)

    def put(self, key, val):
        with self.lock:
            if len(self.d) >= self.limit:
                self.d.clear()
            self.d[key] = val


def _folder_art(path):
    d = os.path.dirname(path)
    try:
        names = os.listdir(d)
    except OSError:
        return None
    lower = {n.lower(): n for n in names}
    for stem in _ART_NAMES:
        for ext in _ART_EXT:
            n = lower.get(stem + ext)
            if n:
                return os.path.join(d, n)
    for n in names:                                 # any image at all, as a last resort
        if os.path.splitext(n)[1].lower() in _ART_EXT:
            return os.path.join(d, n)
    return None


def _embedded_art(path):
    try:
        import mutagen
        f = mutagen.File(path)
        if f is None:
            return None
        tags = getattr(f, "tags", None)
        if tags:
            for k in tags.keys():
                if str(k).startswith("APIC"):
                    return tags[k].data, tags[k].mime
        pics = getattr(f, "pictures", None)          # FLAC
        if pics:
            return pics[0].data, pics[0].mime
    except Exception:
        return None
    return None


# ----------------------------------------------------------------------- playlists

_M3U_EXT = (".m3u", ".m3u8")


def _safe_out(playlist_dir, name):
    """Resolve `name` to a path that is provably inside playlist_dir.

    Filenames only — no directories, no traversal, no absolute paths, no device
    namespaces. We sanitise first, then verify containment on the REAL path, because
    sanitising alone is not a containment proof (8.3 short names, symlinks).
    """
    base = os.path.basename(str(name or "").strip())
    base = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", base)
    base = base.strip(" .") or "mix"
    if not base.lower().endswith(_M3U_EXT):
        base += ".m3u8"
    root = os.path.realpath(playlist_dir)
    dest = os.path.realpath(os.path.join(root, base))
    if os.path.commonpath([root, dest]) != root:
        raise ValueError("refusing to write outside the playlist folder")
    return dest


def _scan_playlists(playlist_dir):
    out = []
    if not playlist_dir or not os.path.isdir(playlist_dir):
        return out
    for dirpath, _dirnames, filenames in os.walk(playlist_dir):
        depth = os.path.relpath(dirpath, playlist_dir).count(os.sep)
        if depth > 1:
            continue
        for fn in filenames:
            if os.path.splitext(fn)[1].lower() not in _M3U_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, playlist_dir).replace("\\", "/")
            try:
                st = os.stat(full)
            except OSError:
                continue
            out.append({"name": rel, "mtime": int(st.st_mtime), "bytes": st.st_size})
    out.sort(key=lambda r: -r["mtime"])
    return out


def _read_playlist(full, lib):
    """Resolve an m3u/m3u8's entries back to pool indices. Missing entries are reported,
    never silently dropped — a playlist viewer that lies about its contents is useless."""
    rows, missing = [], []
    try:
        # utf-8-sig: many .m3u8 writers (including Windows tools) prepend a BOM, which would
        # otherwise turn the leading "#EXTM3U" into "﻿#EXTM3U" -- not a comment, and so
        # reported as a missing track.
        with open(full, "r", encoding="utf-8-sig", errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError as e:
        raise ValueError(str(e))
    for ln in lines:
        ln = ln.strip().lstrip("﻿")
        if not ln or ln.startswith("#"):
            continue
        i = lib.by_relkey.get(_relkey(ln))
        if i is None:
            cands = lib.by_base.get(os.path.basename(ln.replace("/", "\\")).lower(), [])
            i = cands[0] if len(cands) == 1 else None
        if i is None:
            missing.append(os.path.basename(ln))
        else:
            rows.append(i)
    return rows, missing


# ----------------------------------------------------------------------- blueprint

def register(app, ctx):
    """ctx: dict(db_path, eng, labels, mapper, playlist_dir, engine_name, plex_configured,
                 active_mix_tracks=callable(i,size,field)->[paths])"""
    eng = ctx["eng"]
    lib = LibraryIndex(ctx["db_path"], eng.paths, eng.meta)
    art = ArtCache()
    bp = Blueprint("studio", __name__)
    playlist_dir = ctx.get("playlist_dir") or ""
    locked = ctx["locked"]
    # hands-out record (ruling B1); absent in older callers, so no-op fallback
    ledger = ctx.get("ledger") or (lambda *_a, **_k: None)

    @bp.get("/api/lib/stats")
    @locked
    def lib_stats():
        analyzed = sum(lib.analyzed)
        return jsonify(
            songs=lib.n, analyzed=analyzed,
            missing=sum(lib.missing),
            # `songs` is the MIXABLE POOL and always was; these two say so out loud.
            # db_tracks is what the library actually holds, failed is the difference --
            # tracks that never made the pool. Reporting only the pool made a library with
            # 217 unusable tracks read as "21,236 songs, 21,236 analyzed", i.e. perfect
            # (MORNING_REPORT §4.1). The UI shows both whenever they disagree.
            pool=lib.n, db_tracks=lib.db_tracks, failed=len(lib.failures),
            # how many tracks have ever been looked for on disk (libverify.py fills
            # checked_at). 0 means the Missing Files view is empty because nobody has
            # run the check, NOT because every file is present -- the view could not say
            # which, since nothing in the window started a check (MORNING_REPORT §4.2).
            verified=sum(1 for t in lib.checked_at if t),
            hours=round(lib.total_seconds / 3600.0, 1),
            gb=round(lib.total_bytes / 1e9, 1),
            genres=len({t for g in lib.gtags for t in g}),
            artists=len({a for a in lib.artist if a}),
            albums=len({a for a in lib.album if a}),
            engine=ctx["engine_name"],
            plex=ctx["plex_configured"],
            playlist_dir=playlist_dir,
            # the actual library roots, read from .env at startup. Kept out of tracked
            # source (leak_check) and handed to the UI here so the export dropdown can
            # show the user where their playlist paths will point.
            roots={
                "local": getattr(ctx["mapper"], "local_root", "") or "",
                "unc": getattr(ctx["mapper"], "unc_root", "") or "",
                "plex": getattr(ctx["mapper"], "plex_root", "") or "",
                # a candidate for an UNSET local root, to show and let the operator accept.
                # Never applied by itself -- ruling (a), MORNING_REPORT §5.1. Empty when the
                # library has no single folder every track sits under.
                "local_suggested": ctx.get("root_suggestion") or "",
            },
            # {"ok": n, "total": n} -- how many pool tracks the local root can actually
            # rewrite. ok < total means the root is set but does not cover the library, so
            # some paths will silently keep their local form on every export.
            root_coverage=ctx.get("root_coverage") or {},
        )

    @bp.get("/api/lib/failures")
    @locked
    def lib_failures():
        """The tracks the library holds that never made the mixable pool.

        Loopback-only, and for the same reason app.py's /api/fs/dirs and applog.py's
        /api/diag/logs are: the rows carry full local file paths. Naming the file IS the
        point of this view -- these tracks appear nowhere else, so "which file is it" is
        the one question it has to answer -- and the LAN server must not answer it for
        another machine (app.py's Plex route strips paths for exactly this reason)."""
        if request.remote_addr not in ("127.0.0.1", "::1"):
            return jsonify(error="only available on the Attune machine itself"), 403
        return jsonify(total=len(lib.failures), pool=lib.n, db_tracks=lib.db_tracks,
                       rows=lib.failures)

    def _multi(key):
        v = request.args.getlist(key)
        return [x for x in v if x]

    @bp.get("/api/lib/facets")
    @locked
    def lib_facets():
        """Counts are computed against the OTHER panes' selection, so each pane shows what
        is still reachable — the cascade the MusicIP panes actually perform."""
        g, a, b = _multi("genre"), _multi("artist"), _multi("album")
        q = request.args.get("q", "")
        gl, _, _ = lib.facets(lib.select(genres=(), artists=a, albums=b, q=q))
        _, al, _ = lib.facets(lib.select(genres=g, artists=(), albums=b, q=q))
        _, _, bl = lib.facets(lib.select(genres=g, artists=a, albums=(), q=q))
        cap = lambda xs: [{"name": n, "n": c} for n, c in xs[:4000]]
        return jsonify(genres=cap(gl), artists=cap(al), albums=cap(bl))

    # Smart views: predicate over the user columns userdata.py fills in. Kept here (not in
    # LibraryIndex.select) because they read live rating/loved/plays that change at runtime.
    SMART = {
        "loved":     lambda i: lib.loved[i],
        "toprated":  lambda i: lib.rating[i] >= 4,
        "unrated":   lambda i: lib.rating[i] == 0,
        "mostplayed": lambda i: lib.plays[i] > 0,
        "neverplayed": lambda i: lib.plays[i] == 0,
        "recent":    lambda i: bool(lib.date_added[i]),
        "missing":   lambda i: lib.missing[i],
    }
    # default sort per smart view, so "Recently Added" actually shows newest first etc.
    SMART_SORT = {"mostplayed": ("plays", False), "recent": ("added", False),
                  "toprated": ("rating", False)}

    @bp.get("/api/lib/tracks")
    @locked
    def lib_tracks():
        g, a, b = _multi("genre"), _multi("artist"), _multi("album")
        q = request.args.get("q", "")
        smart = request.args.get("smart") or ""
        sort = request.args.get("sort", "artist")
        if sort not in LibraryIndex.SORTS:
            sort = "artist"
        desc = (request.args.get("desc") or "").lower() in ("1", "true", "on")
        try:
            offset = max(0, int(request.args.get("offset", 0)))
            limit = min(max(1, int(request.args.get("limit", 200))), 500)
        except ValueError:
            return jsonify(error="bad request"), 400

        rows = lib.select(genres=g, artists=a, albums=b, q=q)
        if smart in SMART:
            pred = SMART[smart]
            rows = [i for i in rows if pred(i)]
            # honour an explicit sort; otherwise use the view's natural order
            if request.args.get("sort") is None and smart in SMART_SORT:
                sort, desc = SMART_SORT[smart]
        # `track` and `status` sorts need the tag read; only pay for it on the page we
        # return unless the sort itself needs it library-wide.
        keyf = LibraryIndex.SORTS[sort]
        rows.sort(key=lambda i: keyf(lib, i), reverse=desc)
        page = rows[offset:offset + limit]
        secs = sum(lib.seconds[i] for i in rows)
        return jsonify(
            total=len(rows), offset=offset,
            seconds=secs,
            rows=[lib.row(i) for i in page])

    @bp.get("/api/lib/rows")
    @locked
    def lib_rows():
        """Hydrate an arbitrary list of pool indices (used by Mix view + playlist view)."""
        try:
            ids = [int(x) for x in request.args.getlist("i")][:500]
        except ValueError:
            return jsonify(error="bad request"), 400
        ids = [i for i in ids if 0 <= i < lib.n]
        return jsonify(rows=[lib.row(i) for i in ids])

    @bp.get("/api/lib/albums")
    @locked
    def lib_albums():
        """Album-grid data: group the (facet/smart-filtered) selection by album, one card
        each with an art seed (a representative track index), artist, track count, length.
        Sorted by album-artist then album so the grid reads like a shelf."""
        g, a, b = _multi("genre"), _multi("artist"), _multi("album")
        q = request.args.get("q", "")
        rows = lib.select(genres=g, artists=a, albums=b, q=q)
        albums = {}
        for i in rows:
            key = (lib.album[i] or "").casefold() + "|" + os.path.dirname(lib.paths[i]).casefold()
            d = albums.get(key)
            if d is None:
                albums[key] = d = {"album": lib.album[i] or "(no album)",
                                   "artist": lib.artist[i], "seed": i,
                                   "n": 0, "seconds": 0, "year": lib.year[i] or None,
                                   "ids": []}
            d["n"] += 1
            d["seconds"] += lib.seconds[i]
            d["ids"].append(i)
            # prefer a track that actually resolves album art: the one whose folder has art
            # is unknowable cheaply, so keep the first — good enough, art falls back to embedded.
        out = sorted(albums.values(),
                     key=lambda d: (_fold(d["artist"]) or "￿", _fold(d["album"])))
        for d in out:
            d["length"] = _seconds_to_len(d["seconds"])
        return jsonify(total=len(out), albums=out)

    @bp.get("/api/lib/folders")
    @locked
    def lib_folders():
        """One level of the folder tree. `?path=` (empty = roots): returns immediate child
        folders under it, each with a recursive track count, plus the pool indices of tracks
        that live DIRECTLY in this folder. Lazy by design — never walks the whole 21k tree."""
        base = request.args.get("path", "")
        base_n = base.rstrip("\\/").lower()
        children = {}          # child-folder-name -> [count, full_child_path]
        here = []              # indices of tracks directly in `base`
        for i, p in enumerate(lib.paths):
            d = os.path.dirname(p)
            dl = d.lower()
            if base == "":
                # roots = the drive/UNC head of each path
                norm = p.replace("/", "\\")
                head = norm.split("\\", 1)[0] + "\\" if "\\" in norm else norm
                c = children.get(head.lower())
                if c is None:
                    children[head.lower()] = [1, head]
                else:
                    c[0] += 1
                continue
            if dl == base_n:
                here.append(i)
                continue
            if dl.startswith(base_n + "\\") or dl.startswith(base_n + "/"):
                rest = d[len(base):].lstrip("\\/")
                seg = rest.replace("/", "\\").split("\\", 1)[0]
                full = os.path.join(base, seg)
                c = children.get(full.lower())
                if c is None:
                    children[full.lower()] = [1, full]
                else:
                    c[0] += 1
        kids = sorted(({"name": os.path.basename(v[1].rstrip("\\/")) or v[1],
                        "path": v[1], "n": v[0]} for v in children.values()),
                      key=lambda x: _fold(x["name"]))
        here.sort(key=lambda i: (lib.fno[i], lib.f_title[i]))
        return jsonify(path=base, folders=kids, rows=[lib.row(i) for i in here[:500]])

    @bp.get("/api/art")
    @locked
    def art_route():
        try:
            i = int(request.args.get("i", ""))
        except ValueError:
            return jsonify(error="bad request"), 400
        if not (0 <= i < lib.n):
            return jsonify(error="unknown track"), 404
        path = eng.paths[i]
        key = os.path.dirname(path).lower()
        hit = art.get(key)
        if hit == "none":
            return Response(status=404)
        if hit:
            data, mime = hit
            return Response(data, mimetype=mime, headers={"Cache-Control": "public, max-age=3600"})
        fa = _folder_art(path)
        if fa:
            art.put(key, None)                       # don't cache big files in RAM
            return send_file(fa, conditional=True)
        emb = _embedded_art(path)
        if emb:
            art.put(key, emb)
            return Response(emb[0], mimetype=emb[1],
                            headers={"Cache-Control": "public, max-age=3600"})
        art.put(key, "none")
        return Response(status=404)

    # ---------------- playlists on disk ----------------
    @bp.get("/api/playlists")
    def playlists():
        return jsonify(dir=playlist_dir, playlists=_scan_playlists(playlist_dir))

    @bp.get("/api/playlist")
    @locked
    def playlist():
        name = request.args.get("name", "")
        if not playlist_dir:
            return jsonify(error="no playlist folder configured"), 400
        try:
            root = os.path.realpath(playlist_dir)
            full = os.path.realpath(os.path.join(root, name.replace("/", os.sep)))
            if os.path.commonpath([root, full]) != root or not os.path.isfile(full):
                raise ValueError("not a playlist in the configured folder")
            ids, missing = _read_playlist(full, lib)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(name=name, rows=[lib.row(i) for i in ids], missing=missing)

    @bp.post("/api/export/m3u_dir")
    @locked
    def export_m3u_dir():
        """Write the current mix (or an explicit id list) as an .m3u8 INTO the configured
        playlist folder. The only write path in this app."""
        if not playlist_dir or not os.path.isdir(playlist_dir):
            return jsonify(ok=False, error="no playlist folder configured"), 400
        body = request.get_json(silent=True) or {}
        flavor = body.get("flavor", "unc")
        if flavor not in ("local", "unc", "plex"):
            flavor = "unc"
        ids = body.get("ids")
        if isinstance(ids, list) and ids:
            try:
                tracks = [eng.paths[int(x)] for x in ids if 0 <= int(x) < lib.n]
            except (ValueError, TypeError):
                return jsonify(ok=False, error="bad ids"), 400
            default_name = "Attune mix"
        else:
            try:
                i = int(body.get("i"))
                size = min(max(int(body.get("size", 25)), 1), 200)
            except (TypeError, ValueError):
                return jsonify(ok=False, error="bad request"), 400
            if not (0 <= i < lib.n):
                return jsonify(ok=False, error="unknown seed"), 404
            tracks = ctx["active_mix_tracks"](i, size, body.get("dedup") or None)
            # Default name follows the operator's own 2021 convention (ruling C9):
            # a mix is "like-<seed>". An explicit client name still wins.
            sm = eng.meta.get(eng.paths[i], {})
            seed_name = (sm.get("title")
                         or os.path.splitext(os.path.basename(eng.paths[i]))[0]).strip()
            default_name = f"like-{seed_name or 'mix'}"
        if not tracks:
            return jsonify(ok=False, error="empty playlist"), 400
        try:
            dest = _safe_out(playlist_dir, body.get("name") or default_name)
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        oneline = lambda s: (s or "").replace("\r", " ").replace("\n", " ")
        # Same one call the download route uses (app.py's /api/export/m3u), so the two
        # buttons cannot disagree about the same mix any more (MORNING_REPORT §5.2). It
        # never raises: a path Attune cannot rewrite honestly keeps its LOCAL form and is
        # counted in `report`, which goes back to the client as `fallback` (ruling (a),
        # §5.1). This route used to 400 the whole save instead.
        converted, report = ctx["mapper"].convert_all(tracks, flavor)
        lines = ["#EXTM3U"]
        for p, out in zip(tracks, converted):
            m = eng.meta.get(p, {})
            lines.append(f"#EXTINF:{lib.seconds[eng.idx[p]] if p in eng.idx else -1},"
                         f"{oneline(m.get('artist') or '?')} - "
                         f"{oneline(m.get('title') or os.path.basename(p))}")
            lines.append(out)
        try:
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as e:
            return jsonify(ok=False, error=f"write failed: {e}"), 500
        ledger("export_dir", dest=dest, name=os.path.basename(dest),
               n=len(tracks), flavor=report["used"],
               tracks=[f"{(eng.meta.get(p, {}).get('artist') or '?')} - "
                       f"{(eng.meta.get(p, {}).get('title') or os.path.basename(p))}"
                       for p in tracks])
        return jsonify(ok=True, path=dest, name=os.path.basename(dest),
                       count=len(tracks), fallback=report)

    @bp.get("/studio")
    def studio():
        return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "static", "studio.html"))

    @bp.get("/studio.js")
    def studio_js():
        return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "static", "studio.js"), mimetype="text/javascript")

    @bp.get("/studio.css")
    def studio_css():
        return send_file(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "static", "studio.css"), mimetype="text/css")

    app.register_blueprint(bp)
    return lib
