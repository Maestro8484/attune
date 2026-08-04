r"""Attune library verification -- missing-file detection + manual relink.

The library lives on L:\ (SMB/NAS, see CLAUDE.md's machine list): files get moved,
renamed, or the share drops, and until now nothing in the app noticed -- a missing
file just failed silently at play time (a 404 on /audio) or quietly dropped out of a
mix with no explanation anywhere in the UI.

`filestate` is a NEW table, additive and OUTSIDE db.py's SCHEMA_VERSION guard -- same
rationale as usermeta/smartlists/recipes (see their own module docstrings): this is
app-derived bookkeeping, not analysis data, so a features rebuild must never wipe it.

The verify job mirrors scanjob.py's shape (module _START_LOCK, lock-free status()
reads of plain attributes) and walks every track path with a plain os.stat, so a slow
network share degrades to "verify takes a while", never to a blocked request thread.

Relink RE-KEYS the track's path across every per-track table (tracks/features/clap/
usermeta/filestate) rather than only updating `tracks.path` -- the analyzed CLAP/
librosa vectors belong to the audio content, not to whatever path currently names it,
and a relink that left them behind would silently evict the track from the mixable
pool (HybridEngine's pool membership comes from the `clap`/`features` tables, not
`tracks`) the next time the library reloads. Tags are then re-read from the (now
correctly pathed) file and mirrored in with userdata.py's own write_tags() -- reused,
not reinvented -- so `tracks`/the in-memory index/labels all agree with the file.

Endpoints:
  POST /api/lib/verify                loopback-guarded, starts a verify pass
  GET  /api/lib/verify/status          {running, checked, total, missing, started, finished}
  POST /api/lib/verify/cancel          loopback-guarded
  POST /api/track/relink {i,new_path}  loopback-guarded; verify+rekey+retag+unmark
  GET  /api/fs/files?path=             sibling of app.py's /api/fs/dirs: lists AUDIO
                                        files (not subfolders) directly in one folder --
                                        backs the "Locate file..." filename picker.

Known limitation (documented, not engineered around -- both triggers are rare, manual,
same-operator actions): a verify pass in flight while a hot reload (libreload.py)
reorders the pool may attribute a couple of straggler results to the wrong pool index
in the LIVE array (the DB write is always keyed by the actual path checked, so it is
never wrong); a second verify pass after the reload settles it.
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from flask import Blueprint, jsonify, request

# Mirrors autoscan.py's own local copy of scan.py's AUDIO_EXTS (kept local rather than
# imported, same reasoning: this module must import cleanly without touching src/).
_AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".wav"}

# Per-track tables a relink's path rename must follow into. Kept as one list so a
# future additive table only needs adding here (and to userdata.py's delete_track).
_PER_TRACK_TABLES = ("tracks", "features", "clap", "usermeta", "filestate")

_START_LOCK = threading.Lock()   # scanjob.py's own pattern: module-level, not instance


def _ensure_schema(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS filestate (
            path       TEXT PRIMARY KEY,
            missing    INTEGER DEFAULT 0,
            checked_at INTEGER
        )""")
        con.commit()
    finally:
        con.close()


def _rekey_path(db_path, old_path, new_path):
    """Rename `old_path` to `new_path` as the key across every per-track table, inside
    one transaction. UPDATE OR IGNORE + a follow-up DELETE handles the (rare) case
    where a row already exists at new_path: the pre-existing new_path row wins, the
    stale old_path row is dropped rather than erroring the whole relink."""
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("BEGIN IMMEDIATE")
        for table in _PER_TRACK_TABLES:
            try:
                con.execute(f"UPDATE OR IGNORE {table} SET path=? WHERE path=?",
                            (new_path, old_path))
                con.execute(f"DELETE FROM {table} WHERE path=?", (old_path,))
            except sqlite3.OperationalError:
                pass    # table doesn't exist in this DB yet -- nothing to rekey there
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.close()


class FileState:
    """Attaches lib.missing / lib.checked_at from the `filestate` table -- the exact
    "attach columns to LibraryIndex" pattern userdata.py's UserData uses for rating/
    loved/plays/etc, applied to file-presence bookkeeping instead of user data."""

    def __init__(self, db_path, lib):
        self.db_path = db_path
        self.lib = lib
        self._load()

    def _load(self):
        lib = self.lib
        n = lib.n
        lib.missing = [False] * n
        lib.checked_at = [0] * n
        idx = {p: i for i, p in enumerate(lib.paths)}
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            for p, m, ca in con.execute("SELECT path, missing, checked_at FROM filestate"):
                i = idx.get(p)
                if i is not None:
                    lib.missing[i] = bool(m)
                    lib.checked_at[i] = int(ca or 0)
        finally:
            con.close()

    def reload(self):
        """Re-run after libreload.py has swapped `self.lib`'s __dict__ for a freshly
        sized instance -- re-sizes + repopulates missing/checked_at to match."""
        self._load()

    def set(self, i, path, missing, checked_at):
        """Write the verified state for `path` (the path actually checked -- NOT
        necessarily self.lib.paths[i], which may have moved on if a reload raced this
        write; see the module docstring's documented limitation) to the DB, and best-
        effort mirror it into the live lib arrays at index i."""
        # A verify pass makes ~21k rapid open/write/close cycles against a WAL db;
        # on Windows a single transient "attempt to write a readonly database" (the
        # -shm handle racing another opener) used to abort the WHOLE pass at
        # whatever file it reached (observed 2026-08-04 at 16,671/21,236). One
        # retried write is cheap; a dead pass costs a full re-run.
        for attempt in (1, 2, 3):
            con = sqlite3.connect(self.db_path, timeout=30)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute(
                    """INSERT INTO filestate(path, missing, checked_at) VALUES(?,?,?)
                       ON CONFLICT(path) DO UPDATE SET missing=excluded.missing,
                         checked_at=excluded.checked_at""",
                    (path, 1 if missing else 0, checked_at))
                con.commit()
                break
            except sqlite3.OperationalError:
                if attempt == 3:
                    raise
                time.sleep(0.2 * attempt)
            finally:
                con.close()
        if 0 <= i < len(self.lib.missing):
            self.lib.missing[i] = bool(missing)
            self.lib.checked_at[i] = checked_at


class VerifyJob:
    """At most one verify pass runs at a time; status is read lock-free (plain
    attributes, GIL-safe), same style as scanjob.ScanJob."""

    def __init__(self, filestate):
        self.filestate = filestate
        self.thread = None
        self.running = False
        self.cancelled = False
        self.started = 0
        self.finished = 0
        self.checked = 0
        self.total = 0
        self.missing = 0
        self.error = ""

    def start(self):
        with _START_LOCK:
            if self.running:
                raise RuntimeError("a verify pass is already running")
            filestate = self.filestate
            self.__init__(filestate)
            self.running = True
            self.started = int(time.time())
            paths = list(filestate.lib.paths)
            self.total = len(paths)
            self.thread = threading.Thread(target=self._run, args=(paths,), daemon=True)
            self.thread.start()

    def _run(self, paths):
        try:
            now = int(time.time())
            for j, p in enumerate(paths):
                if self.cancelled:
                    break
                missing = not os.path.isfile(p)
                if missing:
                    self.missing += 1
                self.filestate.set(j, p, missing, now)
                self.checked += 1
        except Exception as e:
            self.error = str(e)
        finally:
            self.finished = int(time.time())
            self.running = False

    def cancel(self):
        self.cancelled = True

    def status(self):
        return {
            "running": self.running,
            "cancelled": self.cancelled,
            "checked": self.checked,
            "total": self.total,
            "missing": self.missing,
            "started": self.started,
            "finished": self.finished,
            "error": self.error,
        }


def register(app, ctx):
    """ctx: dict(db_path, lib, eng, ud, engine_lock, locked). Returns
    dict(filestate=FileState, job=VerifyJob) -- libreload.py needs `filestate` to
    re-attach lib.missing/checked_at after it swaps `lib` for a freshly sized one."""
    db_path = ctx["db_path"]
    lib = ctx["lib"]
    eng = ctx["eng"]
    ud = ctx["ud"]
    locked = ctx["locked"]
    _ensure_schema(db_path)
    filestate = FileState(db_path, lib)
    job = VerifyJob(filestate)
    bp = Blueprint("libverify", __name__)

    def _guard():
        return request.remote_addr in ("127.0.0.1", "::1")

    @bp.post("/api/lib/verify")
    def verify_start():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        try:
            job.start()
        except RuntimeError as e:
            return jsonify(ok=False, error=str(e)), 409
        return jsonify(ok=True)

    @bp.get("/api/lib/verify/status")
    def verify_status():
        return jsonify(job.status())

    @bp.post("/api/lib/verify/cancel")
    def verify_cancel():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        job.cancel()
        return jsonify(ok=True)

    @bp.post("/api/track/relink")
    @locked
    def relink():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        body = request.get_json(silent=True) or {}
        try:
            i = int(body.get("i"))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="bad request"), 400
        if not (0 <= i < len(eng.paths)):
            return jsonify(ok=False, error="unknown track"), 404
        new_path = os.path.abspath(str(body.get("new_path") or "").strip())
        if not new_path or not os.path.isfile(new_path):
            return jsonify(ok=False, error="file not found"), 400

        old_path = eng.paths[i]
        now = int(time.time())
        if new_path == old_path:
            filestate.set(i, old_path, False, now)
            return jsonify(ok=True, i=i, row=lib.row(i))

        try:
            _rekey_path(db_path, old_path, new_path)
        except sqlite3.Error as e:
            return jsonify(ok=False, error=str(e)), 500

        # live index: eng.paths IS lib.paths (same list, see studio.py's LibraryIndex
        # constructor), so this one assignment updates both.
        eng.paths[i] = new_path
        eng.idx[new_path] = eng.idx.pop(old_path, i)
        eng.meta[new_path] = eng.meta.pop(old_path, {})
        filestate.set(i, new_path, False, now)

        # Re-read tags from the (now correctly pathed) file and mirror them in via
        # userdata.py's existing write_tags() -- reuse, not reinvention: it already
        # updates tracks/lib.update_row/eng.meta/labels in one place.
        try:
            tags = ud.read_tags(i)["tags"]
            row = ud.write_tags(i, tags)
        except Exception:
            row = lib.row(i)
        return jsonify(ok=True, i=i, row=row)

    @bp.get("/api/fs/files")
    def fs_files():
        # Local-first guard, same rule as app.py's /api/fs/dirs: folder/file browsing
        # is a this-machine affordance, never exposed to a LAN client.
        if not _guard():
            return jsonify(error="only available on the Attune machine itself"), 403
        path = (request.args.get("path") or "").strip()
        if not path or not os.path.isdir(path):
            return jsonify(error=f"not a folder: {path}"), 404
        files = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if (e.is_file(follow_symlinks=False)
                                and os.path.splitext(e.name)[1].lower() in _AUDIO_EXTS):
                            files.append({"name": e.name, "path": e.path})
                    except OSError:
                        pass
        except OSError as ex:
            return jsonify(error=f"cannot read folder: {ex}"), 403
        files.sort(key=lambda f: f["name"].lower())
        return jsonify(path=path, files=files)

    app.register_blueprint(bp)
    return {"filestate": filestate, "job": job}
