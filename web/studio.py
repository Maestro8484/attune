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

        self._trackno = {}
        self._trackno_lock = threading.Lock()
        self.total_seconds = sum(self.seconds)
        self.total_bytes = 0
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        row = con.execute("SELECT SUM(bytes) FROM tracks").fetchone()
        con.close()
        self.total_bytes = int(row[0] or 0)

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
        "length": lambda s, i: s.seconds[i],
        "year": lambda s, i: s.year[i],
        "status": lambda s, i: (not s.analyzed[i], s.f_artist[i]),
    }

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
            "status": "Analyzed" if self.analyzed[i] else "Not analyzed",
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

    @bp.get("/api/lib/stats")
    def lib_stats():
        analyzed = sum(lib.analyzed)
        return jsonify(
            songs=lib.n, analyzed=analyzed,
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
            },
        )

    def _multi(key):
        v = request.args.getlist(key)
        return [x for x in v if x]

    @bp.get("/api/lib/facets")
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

    @bp.get("/api/lib/tracks")
    def lib_tracks():
        g, a, b = _multi("genre"), _multi("artist"), _multi("album")
        q = request.args.get("q", "")
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
    def lib_rows():
        """Hydrate an arbitrary list of pool indices (used by Mix view + playlist view)."""
        try:
            ids = [int(x) for x in request.args.getlist("i")][:500]
        except ValueError:
            return jsonify(error="bad request"), 400
        ids = [i for i in ids if 0 <= i < lib.n]
        return jsonify(rows=[lib.row(i) for i in ids])

    @bp.get("/api/art")
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
        else:
            try:
                i = int(body.get("i"))
                size = min(max(int(body.get("size", 25)), 1), 200)
            except (TypeError, ValueError):
                return jsonify(ok=False, error="bad request"), 400
            if not (0 <= i < lib.n):
                return jsonify(ok=False, error="unknown seed"), 404
            tracks = ctx["active_mix_tracks"](i, size, body.get("dedup") or None)
        if not tracks:
            return jsonify(ok=False, error="empty playlist"), 400
        try:
            dest = _safe_out(playlist_dir, body.get("name") or "Attune mix")
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        oneline = lambda s: (s or "").replace("\r", " ").replace("\n", " ")
        lines = ["#EXTM3U"]
        try:
            for p in tracks:
                m = eng.meta.get(p, {})
                lines.append(f"#EXTINF:{lib.seconds[eng.idx[p]] if p in eng.idx else -1},"
                             f"{oneline(m.get('artist') or '?')} - "
                             f"{oneline(m.get('title') or os.path.basename(p))}")
                lines.append(ctx["mapper"].convert(p, flavor))
        except ValueError as e:                       # unconfigured flavor root
            return jsonify(ok=False, error=str(e)), 400
        try:
            with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                fh.write("\n".join(lines) + "\n")
        except OSError as e:
            return jsonify(ok=False, error=f"write failed: {e}"), 500
        return jsonify(ok=True, path=dest, name=os.path.basename(dest), count=len(tracks))

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
