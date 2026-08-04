"""Attune export-copy job — "Take It With You": copy a mix's ACTUAL audio files
plus a self-contained relative-path .m3u8 into a folder or USB stick, so the folder
plays in order on a dumb car head unit / phone / anything.

This is the missing third export destination. The existing playlist-file exports
(``/api/export/m3u``, ``/api/export/m3u_dir``, ``/api/export/plex``) only write
PATHS — they assume the player can already reach the library. A USB stick in a car
cannot, so this job copies the files themselves.

Shape is cloned deliberately from ``scanjob.py``: one background job at a time
(module-level ``_START_LOCK``), state read lock-free by ``/status`` (GIL-safe reads
of plain attributes), a ``register(app, ctx)`` that wires three endpoints and returns
the job. No queue, no service — a killed copy costs nothing; the folder is just a
partial the user can re-copy.

Non-destructive, absolutely: sources are opened READ-ONLY (``shutil.copy2``, which
copies + preserves mtime and never touches the source). Destination is confined to
the user-picked directory via a realpath+commonpath containment check (same proof
``studio.py._safe_out`` uses). Filenames are sanitised for FAT32/exFAT so the stick
mounts everywhere.

The track list is resolved by the CALLER on the request thread (Flask ``request``
context is unavailable in the worker thread), then handed to this job as concrete
(src, artist, title) items.

Endpoints (all loopback-guarded like ``/api/fs/dirs`` — copying files is a
this-machine action, never something a LAN client should drive):
  GET  /api/export/copy/status
  POST /api/export/copy    {dest, folder?, layout?, ids?|(i,size,dedup?)}
  POST /api/export/copy/cancel
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from collections import deque

from flask import Blueprint, jsonify, request

# Module-level, same reasoning as scanjob.py's lock: start() resets instance state,
# so an instance-level lock would wipe itself. Guards the whole
# check-then-reset-then-launch body of start() against two overlapping callers.
_START_LOCK = threading.Lock()

# Chars illegal on FAT32/exFAT/NTFS, plus control chars. `/` and `\` included so a
# tag value like "AC/DC" can never introduce a path separator.
_FAT_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')

# Windows reserved device names (case-insensitive, with or without an extension).
# A file literally named "CON.mp3" is unopenable. The NN order prefix on flat-layout
# filenames already defuses this (a name starting with a digit is never reserved),
# but subfolder names in tree layout are guarded by the same check.
_RESERVED = {"CON", "PRN", "AUX", "NUL",
             *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}

# Refuse a copy unless this much headroom remains after it (guards against filling a
# stick to the last byte, which some FAT drivers choke on).
_FREE_MARGIN = 16 * 1024 * 1024      # 16 MB


def _sanitize_component(s):
    """One path component (a filename stem or a folder name), made FAT-safe.
    Returns "" if nothing survives, so callers can substitute a fallback."""
    s = re.sub(r"\s+", " ", str(s or ""))    # tabs/newlines/runs -> one space first
    s = _FAT_ILLEGAL.sub("_", s).strip()     # then remaining illegal + control chars -> _
    # Truncate BEFORE the final dot/space strip, so a cut that lands on a "." or space
    # can't leave the component ending in one (Windows silently drops trailing dots/spaces,
    # which would desync the name from what we wrote). Keep well under the 255 limit.
    s = s.strip(". ")[:120].strip(". ")
    if not s:
        return ""
    stem = s.rsplit(".", 1)[0] if "." in s else s
    if stem.upper() in _RESERVED:            # re-checked on the FINAL, truncated component
        s = "_" + s
    return s


def _clean_ext(path):
    ext = os.path.splitext(path)[1]
    return _FAT_ILLEGAL.sub("_", ext) if ext else ""


def _unique(directory, filename, used):
    """Return `filename`, or `stem (2).ext`, `stem (3).ext`, ... — first name not
    already produced this job (per-directory `used` set) nor already on disk. Never
    overwrites silently. Uses `lexists`, not `exists`, so a pre-existing symlink at the
    name — even a DANGLING one — counts as a collision and we never copy THROUGH it."""
    key = os.path.normcase
    stem, ext = os.path.splitext(filename)
    cand, n = filename, 2
    while key(cand) in used or os.path.lexists(os.path.join(directory, cand)):
        cand = f"{stem} ({n}){ext}"
        n += 1
    used.add(key(cand))
    return cand


def _contained(root_real, path):
    """True iff `path`, with every symlink/junction along it resolved, stays inside
    `root_real`. Unreadable or on a different drive -> False (treated as an escape).
    This is the write-time half of the containment proof: `_resolve_target` checks the
    top-level dir, but tree-layout subdirs come from track metadata, so each final path
    is re-checked before it is created/written."""
    try:
        real = os.path.realpath(path)
    except OSError:
        return False
    try:
        return os.path.commonpath([root_real, real]) == root_real
    except ValueError:                       # different drive -> definitely outside
        return False


class ExportJob:
    """At most one copy runs at a time."""

    def __init__(self):
        self.thread = None
        self.cancelled = False
        self.running = False
        self.dest = ""
        self.layout = "flat"
        self.total = 0            # files intended to copy
        self.copied = 0
        self.total_bytes = 0
        self.copied_bytes = 0
        self.current = ""         # basename being copied right now
        self.skipped = []         # [{name, reason}] — unreadable/missing sources
        self.m3u8 = ""            # path to the written playlist, once written
        self.playlist_wanted = True   # False = selection-scope copy, files only (ruling B4)
        self.playlist_stem = ""       # optional .m3u8 stem override, like-<seed> (ruling C9)
        self.error = ""
        self.started = 0
        self.finished = 0
        self.lines = deque(maxlen=60)
        self._lock = threading.Lock()     # guards lines/skipped mutation + status() snapshot

    def _log(self, msg):
        with self._lock:
            self.lines.append(msg)

    def _skip(self, name, reason):
        with self._lock:
            self.skipped.append({"name": name, "reason": reason})

    # Set once by register(); a CLASS attribute so start()'s self.__init__() reset
    # cannot clear it. Called after a copy lands (ruling B1).
    _ledger = None

    # ---------------------------------------------------------------- run
    def start(self, items, target_dir, layout, playlist=True, playlist_stem=""):
        """items: [{src, artist, title}] resolved by the caller (request thread).
        target_dir: an ALREADY-containment-checked absolute directory. layout: flat|tree.
        playlist=False skips the .m3u8 entirely -- selection-scope sends, where the
        numbered filenames already carry play order (ruling B4). playlist_stem, when
        given, names the .m3u8 (like-<seed>, ruling C9) instead of the folder name."""
        with _START_LOCK:
            if self.running:
                raise RuntimeError("a copy is already running")
            self.__init__()
            self.running = True
            self.playlist_wanted = bool(playlist)
            self.playlist_stem = playlist_stem or ""
            self.dest = target_dir
            self.layout = layout
            self.total = len(items)
            self.started = int(time.time())
            self.thread = threading.Thread(
                target=self._run, args=(list(items), target_dir, layout), daemon=True)
            self.thread.start()

    def _plan(self, items, target_dir, layout):
        """Resolve every item to (src, out_dir, out_name, bytes), sanitising names and
        de-colliding. Unreadable/missing sources are recorded in `skipped` and dropped.
        Returns (plan, entries) where entries are the m3u8 rows (rel_path, artist, title)."""
        width = max(2, len(str(len(items))))
        used = {}                         # out_dir -> set of normcased names
        plan, entries = [], []
        order = 0
        for it in items:
            src = it["src"]
            try:
                size = os.path.getsize(src)
            except OSError:
                self._skip(os.path.basename(src), "unreadable or missing")
                continue
            order += 1
            artist = it.get("artist") or ""
            title = it.get("title") or os.path.splitext(os.path.basename(src))[0]
            ext = _clean_ext(src)
            if layout == "tree":
                a = _sanitize_component(artist) or "Unknown Artist"
                b = _sanitize_component(it.get("album") or "Unknown Album") or "Unknown Album"
                out_dir = os.path.join(target_dir, a, b)
                stem = _sanitize_component(f"{order:0{width}d} {title}") or f"{order:0{width}d}"
            else:                          # flat (default) — plays in mix order
                label = f"{artist} - {title}".strip(" -") or title
                stem = _sanitize_component(label) or "track"
                out_dir = target_dir
                stem = f"{order:0{width}d} {stem}"
            names = used.setdefault(os.path.normcase(out_dir), set())
            name = _unique(out_dir, stem + ext, names)
            rel = os.path.relpath(os.path.join(out_dir, name), target_dir).replace("\\", "/")
            plan.append((src, out_dir, name, size))
            entries.append((rel, artist, title))
            self.total_bytes += size
        self.total = len(plan)
        return plan, entries

    def _run(self, items, target_dir, layout):
        try:
            plan, entries = self._plan(items, target_dir, layout)
            if not plan:
                self.error = "no copyable tracks (all sources unreadable)"
                return
            # Preflight free space against the volume the destination lives on.
            try:
                free = shutil.disk_usage(os.path.dirname(target_dir) or target_dir).free
            except OSError as e:
                self.error = f"cannot check free space: {e}"
                return
            need = self.total_bytes + _FREE_MARGIN
            if need > free:
                self.error = (f"not enough space: need {_human(self.total_bytes)} "
                              f"(+{_human(_FREE_MARGIN)} margin), only {_human(free)} free")
                return
            try:
                os.makedirs(target_dir, exist_ok=True)
            except OSError as e:
                self.error = f"cannot create destination: {e}"
                return
            root_real = os.path.realpath(target_dir)
            copied_entries = []
            for src, out_dir, name, size in plan:
                if self.cancelled:
                    break
                self.current = name
                final = os.path.join(out_dir, name)
                # Write-time containment (defense-in-depth for the non-destructive LAW):
                # the RESOLVED destination must stay inside the picked root, checked BEFORE
                # makedirs so a prepared artist/album junction can't redirect the copy — or
                # the directory creation — outside it.
                if not _contained(root_real, final):
                    self._skip(name, "would write outside the destination folder")
                    self._log(f"skip {name}: escapes destination")
                    continue
                try:
                    os.makedirs(out_dir, exist_ok=True)
                    shutil.copy2(src, final)                  # read-only on src
                except OSError as e:
                    self._skip(name, str(e))
                    self._log(f"skip {name}: {e}")
                    continue
                self.copied += 1
                self.copied_bytes += size
                rel = os.path.relpath(final, target_dir).replace("\\", "/")
                m = next((e for e in entries if e[0] == rel), None)
                copied_entries.append(m or (rel, "", os.path.splitext(name)[0]))
                self._log(f"copied {name}")
            self.current = ""
            # Write the self-contained playlist for whatever actually landed (even a
            # cancelled partial is left playable and honest). Selection-scope sends
            # skip it: the numbered filenames already carry the order (ruling B4).
            if copied_entries and self.playlist_wanted:
                self._write_m3u8(target_dir, copied_entries)
            if copied_entries and ExportJob._ledger:
                ExportJob._ledger(
                    "export_copy", dest=target_dir, n=self.copied,
                    playlist=os.path.basename(self.m3u8) if self.m3u8 else "",
                    tracks=[(f"{a} - {t}" if a else t)
                            for _rel, a, t in copied_entries])
        except Exception as e:                    # never let the worker die silently
            self.error = self.error or f"copy failed: {e}"
        finally:
            self.finished = int(time.time())
            self.running = False
            self.current = ""

    def _write_m3u8(self, target_dir, entries):
        stem = _sanitize_component(self.playlist_stem
                                   or os.path.basename(target_dir)) or "Attune Mix"
        oneline = lambda s: (s or "").replace("\r", " ").replace("\n", " ")
        lines = ["#EXTM3U"]
        for rel, artist, title in entries:
            who = oneline(artist) or "?"
            what = oneline(title) or rel
            lines.append(f"#EXTINF:-1,{who} - {what}")
            lines.append(rel)
        # De-collide the playlist name too (never silently overwrite an existing .m3u8),
        # consistent with how the audio files are handled.
        dest = os.path.join(target_dir, _unique(target_dir, f"{stem}.m3u8", set()))
        try:
            # UTF-8 with BOM + CRLF: the format the most stereo/car firmwares accept.
            with open(dest, "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write("\n".join(lines) + "\n")
            self.m3u8 = dest
        except OSError as e:
            self._log(f"m3u8 write failed: {e}")
            # The playlist is the whole point of a self-contained folder, so a failure here
            # is a real, surfaced error even though the audio files copied fine.
            self.error = self.error or f"copied the files, but the playlist could not be written: {e}"

    def cancel(self):
        self.cancelled = True

    def status(self):
        # Snapshot the two mutable containers under the lock the worker appends with, so a
        # poll can't hit "deque mutated during iteration" and 500 mid-copy.
        with self._lock:
            lines = list(self.lines)
            skipped = list(self.skipped)
        return {
            "running": self.running,
            "cancelled": self.cancelled,
            "error": self.error,
            "dest": self.dest,
            "layout": self.layout,
            "total": self.total,
            "copied": self.copied,
            "total_bytes": self.total_bytes,
            "copied_bytes": self.copied_bytes,
            "current": self.current,
            "skipped": skipped,
            "m3u8": self.m3u8,
            "started": self.started,
            "finished": self.finished,
            "lines": lines,
            "done": bool(self.started) and not self.running,
        }


def _human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024


def _resolve_target(dest, folder):
    """Realpath-contained target dir. `dest` is the user-picked existing directory;
    optional `folder` is a subfolder NAME (one component) to create inside it. The
    containment proof mirrors studio.py._safe_out — sanitising alone is not a proof
    (8.3 short names, symlinks), so we verify on the real path."""
    root = os.path.realpath(dest)
    if not folder:
        return root
    name = _sanitize_component(folder)
    if not name:
        return root
    target = os.path.realpath(os.path.join(root, name))
    if target == root or os.path.commonpath([root, target]) != root:
        raise ValueError("invalid destination folder name")
    return target


def register(app, ctx):
    """ctx: dict(eng, active_mix_tracks=callable(i,size,field)->[paths], locked)."""
    eng = ctx["eng"]
    active_mix_tracks = ctx["active_mix_tracks"]
    locked = ctx["locked"]
    job = ExportJob()
    ExportJob._ledger = ctx.get("ledger")
    bp = Blueprint("exportjob", __name__)

    def _guard():
        # Copying files is a this-machine action. Even when the server is LAN-bound
        # (--host 0.0.0.0), a remote client must not drive a copy on this box.
        return request.remote_addr in ("127.0.0.1", "::1")

    @bp.get("/api/export/copy/status")
    def copy_status():
        if not _guard():
            return jsonify(error="only available on the Attune machine itself"), 403
        return jsonify(job.status())

    @bp.post("/api/export/copy")
    @locked
    def copy_start():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        body = request.get_json(silent=True) or {}
        dest = (body.get("dest") or "").strip()
        if not dest or not os.path.isdir(dest):
            return jsonify(ok=False, error="destination folder not found"), 400
        layout = body.get("layout") if body.get("layout") in ("flat", "tree") else "flat"

        # Resolve the track list NOW, on the request thread (request context is not
        # available in the worker). `ids` (the exact on-screen rows) is primary; the
        # i/size form re-derives via the same single source of truth /api/mix uses.
        ids = body.get("ids")
        try:
            if isinstance(ids, list) and ids:
                paths, seen = [], set()
                for x in ids:
                    xi = int(x)
                    if 0 <= xi < len(eng.paths) and xi not in seen:
                        seen.add(xi)
                        paths.append(eng.paths[xi])
            else:
                i = int(body.get("i"))
                size = min(max(int(body.get("size", 100)), 1), 150)
                if not (0 <= i < len(eng.paths)):
                    return jsonify(ok=False, error="unknown seed"), 404
                paths = active_mix_tracks(i, size, (body.get("dedup") or None))
        except (ValueError, TypeError):
            return jsonify(ok=False, error="bad request"), 400
        if not paths:
            return jsonify(ok=False, error="empty playlist"), 400

        items = []
        for p in paths:
            m = eng.meta.get(p, {})
            items.append({"src": p,
                          "artist": (m.get("artist") or "").strip(),
                          "title": (m.get("title") or "").strip(),
                          "album": (m.get("album") or "").strip()})

        try:
            target = _resolve_target(dest, body.get("folder"))
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        # Selection scope (`ids`) copies files only -- no .m3u8, the numbered
        # filenames carry the order (ruling B4). Whole-mix names its playlist
        # like-<seed>, the operator's own 2021 convention (ruling C9).
        selection = isinstance(ids, list) and bool(ids)
        stem = ""
        if not selection:
            sm = eng.meta.get(eng.paths[i], {})
            seed_name = (sm.get("title")
                         or os.path.splitext(os.path.basename(eng.paths[i]))[0]).strip()
            stem = f"like-{seed_name or 'mix'}"
        try:
            job.start(items, target, layout, playlist=not selection,
                      playlist_stem=stem)
        except RuntimeError as e:
            return jsonify(ok=False, error=str(e)), 409
        return jsonify(ok=True, dest=target, total=len(items),
                       playlist=not selection)

    @bp.post("/api/export/copy/cancel")
    def copy_cancel():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        job.cancel()
        return jsonify(ok=True)

    app.register_blueprint(bp)
    return job
