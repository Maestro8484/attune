r"""Attune audio-file detail -- real bitrate, sample rate, channels, codec.

WHY THIS EXISTS AT ALL: `tracks` stores path/artist/album/title/genre/year/seconds/
bytes/mtime and nothing about the encoding. Windows Explorer and MusicBee both show a
bitrate column without breaking a sweat, and Attune could not, so the library browser
could not answer "is this the 320 rip or the 128 one?".

WHY NOT bytes*8/seconds: because that is an AVERAGE over the whole file including the
ID3 tag and any embedded cover art, so a 2 MB jacket on a four-minute track inflates it
by ~66 kbps. It is also useless for telling CBR from VBR. This module reads the real
header instead, which is what every other player does.

PRIOR ART, stated per CLAUDE.md section 7: the existing solution is **mutagen**, which
is ALREADY a dependency of the lean runtime venv (1.48.1, verified 2026-08-07) and
already used by userdata.py for tag writes. Using it, not ffprobe. That matters twice
over: no new dependency, and no entanglement with ruling B5, which wants ffmpeg/ffprobe
dropped from the packaged bundle. Timed on the operator's own NAS library: 5.0 ms per
file over SMB, 200 files in 1.00 s, so a full 21k backfill is ~1.8 min single-threaded.

`audioinfo` is a NEW table, additive and OUTSIDE db.py's SCHEMA_VERSION guard -- same
rationale as filestate/usermeta/smartlists/recipes (see their module docstrings): this
is app-derived bookkeeping read back off the files, not analysis data, so a features
rebuild must never wipe it and its absence must never refuse a DB load.

THE JOB NEVER RUNS IN A REQUEST THREAD. libverify.py's docstring makes the rule
explicit and it applies identically here: the library is on an SMB share, so every read
happens on a background thread and a slow share degrades to "the bitrate column fills
in over the next minute", never to a blocked page render. Columns read blank until the
row lands, which is why row() emits None rather than 0 for an unread track -- 0 kbps is
a claim, blank is the truth.

Endpoints:
  POST /api/lib/audioinfo          loopback-guarded, starts a fill pass
  GET  /api/lib/audioinfo/status   {running, read, total, failed, started, finished}
  POST /api/lib/audioinfo/cancel   loopback-guarded
"""
from __future__ import annotations

import os
import sqlite3
import threading
import time

from flask import Blueprint, jsonify, request

_START_LOCK = threading.Lock()   # scanjob.py / libverify.py pattern: module-level

# Written in batches rather than libverify's one-transaction-per-file: that pass does a
# single os.stat per row and its cost is the stat, while this one already pays 5 ms of
# SMB header read per file, so 21k separate WAL commits on top would dominate. Same
# 3-attempt retry though -- the transient "readonly database" that aborted a verify
# pass at 16,671/21,236 on 2026-08-04 is a property of the WAL handle, not of libverify.
_BATCH = 200


def _ensure_schema(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS audioinfo (
            path        TEXT PRIMARY KEY,
            bitrate     INTEGER,
            sample_rate INTEGER,
            channels    INTEGER,
            mode        TEXT,
            codec       TEXT,
            read_at     INTEGER
        )""")
        con.commit()
    finally:
        con.close()


def _probe(path):
    """Read one file's header. Returns (bitrate_kbps, sample_rate, channels, mode,
    codec) or None if the file is unreadable / unrecognised.

    `mode` is mutagen's bitrate_mode where the format reports one. On this library it
    is often UNKNOWN, because an mp3 only advertises CBR/VBR when a Xing or LAME header
    is present and plenty of older rips have neither. That is why the UI shows the mode
    as a suffix ONLY when it is actually known -- inventing "CBR" for a file that never
    said so would be a fabrication, and the bitrate number itself is correct either way.
    """
    try:
        import mutagen
        f = mutagen.File(path)
        if f is None or not getattr(f, "info", None):
            return None
        i = f.info
        br = int(getattr(i, "bitrate", 0) or 0) // 1000
        sr = int(getattr(i, "sample_rate", 0) or 0)
        ch = int(getattr(i, "channels", 0) or 0)
        # mutagen's BitrateMode LOOKS like a stdlib enum -- repr is `<BitrateMode.CBR: 1>`
        # -- but it is a bare `int` subclass with NO `.name` attribute (checked against
        # mutagen 1.48.1, 2026-08-07: `BitrateMode.__mro__` is (BitrateMode, int, object)
        # and `isinstance(BitrateMode, enum.EnumMeta)` is False). Reading `.name` off it
        # silently yields None for every file, which is exactly the bug this comment
        # exists to stop coming back. `str()` gives "BitrateMode.CBR", so split that;
        # the four defined members are UNKNOWN/CBR/VBR/ABR (ints 0-3).
        mode = getattr(i, "bitrate_mode", None)
        mode = str(mode).rsplit(".", 1)[-1] if mode is not None else ""
        if mode not in ("CBR", "VBR", "ABR"):
            mode = ""      # UNKNOWN, or a format that reports no mode at all (flac)
        codec = type(i).__module__.rsplit(".", 1)[-1]
        return (br, sr, ch, mode, codec)
    except Exception:
        return None


class AudioInfo:
    """Attaches lib.bitrate / sample_rate / channels / brmode / codec from the
    `audioinfo` table -- the same "attach columns to LibraryIndex" pattern userdata.py's
    UserData and libverify.py's FileState use, applied to encoding detail."""

    def __init__(self, db_path, lib):
        self.db_path = db_path
        self.lib = lib
        _ensure_schema(db_path)
        self._load()

    def _load(self):
        lib = self.lib
        n = lib.n
        # None, not 0: an unread track has no bitrate, and 0 kbps would render as a
        # number the file never claimed. studio.py's row() passes these straight out.
        lib.bitrate = [None] * n
        lib.sample_rate = [None] * n
        lib.channels = [None] * n
        lib.brmode = [""] * n
        lib.codec = [""] * n
        idx = {p: i for i, p in enumerate(lib.paths)}
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            for p, br, sr, ch, mode, codec in con.execute(
                    "SELECT path, bitrate, sample_rate, channels, mode, codec FROM audioinfo"):
                i = idx.get(p)
                if i is not None:
                    lib.bitrate[i] = br
                    lib.sample_rate[i] = sr
                    lib.channels[i] = ch
                    lib.brmode[i] = mode or ""
                    lib.codec[i] = codec or ""
        except sqlite3.OperationalError:
            pass        # table not created yet -- every column stays blank, no crash
        finally:
            con.close()

    def reload(self):
        """Re-run after libreload.py swaps `self.lib`'s __dict__ for a freshly sized
        instance -- same contract as FileState.reload()."""
        self._load()

    def unread_paths(self):
        """Pool paths with no audioinfo row yet, in pool order."""
        have = set()
        con = sqlite3.connect(f"file:{self.db_path}?mode=ro", uri=True)
        try:
            have = {p for (p,) in con.execute("SELECT path FROM audioinfo")}
        except sqlite3.OperationalError:
            pass
        finally:
            con.close()
        return [p for p in self.lib.paths if p not in have]

    def write_batch(self, rows):
        """rows: [(path, br, sr, ch, mode, codec)]. One transaction, retried."""
        if not rows:
            return
        now = int(time.time())
        for attempt in (1, 2, 3):
            con = sqlite3.connect(self.db_path, timeout=30)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.executemany(
                    """INSERT INTO audioinfo(path, bitrate, sample_rate, channels,
                                             mode, codec, read_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(path) DO UPDATE SET bitrate=excluded.bitrate,
                         sample_rate=excluded.sample_rate, channels=excluded.channels,
                         mode=excluded.mode, codec=excluded.codec,
                         read_at=excluded.read_at""",
                    [(p, br, sr, ch, mode, codec, now) for p, br, sr, ch, mode, codec in rows])
                con.commit()
                break
            except sqlite3.OperationalError:
                if attempt == 3:
                    raise
                time.sleep(0.2 * attempt)
            finally:
                con.close()
        # mirror into the live arrays so the columns fill in without a reload
        idx = self.lib.idx
        for p, br, sr, ch, mode, codec in rows:
            i = idx.get(p)
            if i is not None and i < len(self.lib.bitrate):
                self.lib.bitrate[i] = br
                self.lib.sample_rate[i] = sr
                self.lib.channels[i] = ch
                self.lib.brmode[i] = mode or ""
                self.lib.codec[i] = codec or ""


class AudioInfoJob:
    """At most one fill pass at a time; status read lock-free (plain attributes,
    GIL-safe), same style as scanjob.ScanJob and libverify.VerifyJob."""

    def __init__(self, info):
        self.info = info
        self.thread = None
        self.running = False
        self.cancelled = False
        self.started = 0
        self.finished = 0
        self.read = 0
        self.failed = 0
        self.total = 0
        self.error = ""

    def status(self):
        return {"running": self.running, "read": self.read, "failed": self.failed,
                "total": self.total, "started": self.started,
                "finished": self.finished, "error": self.error}

    def start(self, paths=None):
        with _START_LOCK:
            if self.running:
                raise RuntimeError("an audio-info pass is already running")
            info = self.info
            self.__init__(info)
            todo = list(paths) if paths is not None else info.unread_paths()
            self.total = len(todo)
            if not todo:
                self.finished = int(time.time())
                return
            self.running = True
            self.started = int(time.time())
            self.thread = threading.Thread(target=self._run, args=(todo,), daemon=True)
            self.thread.start()

    def _run(self, paths):
        try:
            batch = []
            for p in paths:
                if self.cancelled:
                    break
                got = _probe(p) if os.path.isfile(p) else None
                if got is None:
                    # Row still written, with NULLs: a file that cannot be read must not
                    # be re-probed on every launch. read_at records that we looked.
                    self.failed += 1
                    batch.append((p, None, None, None, "", ""))
                else:
                    batch.append((p,) + got)
                self.read += 1
                if len(batch) >= _BATCH:
                    self.info.write_batch(batch)
                    batch = []
            self.info.write_batch(batch)
        except Exception as e:
            self.error = str(e)
        finally:
            self.running = False
            self.finished = int(time.time())


def register(app, ctx):
    """ctx: {db_path, lib, locked}. Returns {"info": AudioInfo, "job": AudioInfoJob}
    so app.py can hand `info` to libreload for re-attachment after a pool swap."""
    db_path = ctx["db_path"]
    lib = ctx["lib"]
    locked = ctx["locked"]

    info = AudioInfo(db_path, lib)
    job = AudioInfoJob(info)

    bp = Blueprint("audioinfo", __name__)

    @bp.post("/api/lib/audioinfo")
    @locked
    def start_fill():
        """?force=1 re-reads EVERY file instead of only the ones with no row yet.
        Needed because a pass that wrote wrong values is otherwise permanent: the
        default fill skips any path that already has a row, so there would be no way
        to correct it short of hand-editing the DB."""
        force = request.args.get("force") in ("1", "true", "yes")
        try:
            job.start(list(lib.paths) if force else None)
        except RuntimeError as e:
            return jsonify(error=str(e)), 409
        return jsonify(job.status())

    @bp.get("/api/lib/audioinfo/status")
    def fill_status():
        return jsonify(job.status())

    @bp.post("/api/lib/audioinfo/cancel")
    @locked
    def cancel_fill():
        job.cancelled = True
        return jsonify(job.status())

    app.register_blueprint(bp)

    # Auto-fill once at boot when rows are missing. Explorer and MusicBee do not make
    # you press a button to see a bitrate, and this costs ~1.8 min ONCE on a cold
    # library, on a daemon thread, writing to an additive table. Subsequent launches
    # find nothing to do and start no thread at all.
    try:
        missing = info.unread_paths()
        if missing:
            job.start(missing)
    except Exception:
        pass        # never let bookkeeping stop the app from serving

    return {"info": info, "job": job}
