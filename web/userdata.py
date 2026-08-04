"""Attune user data — ratings, play counts, tag editing, ReplayGain. The write side.

studio.py's LibraryIndex is a read-only projection; this module owns every per-track
WRITE the app performs, all against one additive `usermeta` table in the same mixer.db
(plus tag writes to the audio files themselves via mutagen, mirrored into `tracks`).

`usermeta` is deliberately OUTSIDE db.py's SCHEMA_VERSION contract: it's user data,
not analysis data — a features rebuild must never delete ratings, and adding this
table must never trip the fail-closed schema guard. Keyed by path like everything else.

Endpoints:
  POST /api/track/rate     {i, rating 0..5}         star rating
  POST /api/track/loved    {i, loved bool}          MusicBee-style heart
  POST /api/track/played   {i}                      play_count += 1, last_played = now
  POST /api/track/skipped  {i}                      skip_count += 1
  GET  /api/track/tags?i=  full tag + tech readout (mutagen)
  POST /api/track/tags     {i, title, artist, ...}  write tags to file + tracks + memory
  GET  /api/track/gain?i=  ReplayGain track/album gain from tags (cached)
  POST /api/reveal         {i}                      Explorer/select (local desktop only)
  POST /api/track/delete   {i}                      remove from tracks/features/clap/
                                                     usermeta/filestate — NEVER the file
                                                     on disk. Loopback-guarded, like
                                                     exportjob.py's copy endpoints: this
                                                     is a this-machine library edit, not
                                                     something a LAN client should drive.

Every id crossing the wire is a pool index into eng.paths — never a raw path.
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import threading
import time

from flask import Blueprint, jsonify, request

_EASY_TAGS = ("title", "artist", "album", "albumartist", "genre", "date",
              "tracknumber", "discnumber", "composer", "comment")

_RG_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def _ensure_schema(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS usermeta (
            path        TEXT PRIMARY KEY,
            rating      INTEGER DEFAULT 0,
            loved       INTEGER DEFAULT 0,
            play_count  INTEGER DEFAULT 0,
            skip_count  INTEGER DEFAULT 0,
            last_played INTEGER,
            date_added  INTEGER
        )""")
        # one-time backfill: date_added <- when the track was analyzed (the closest
        # thing to an import date the old schema recorded). Idempotent via meta flag.
        done = con.execute("SELECT value FROM meta WHERE key='usermeta_backfilled'").fetchone()
        if not done:
            con.execute("""INSERT OR IGNORE INTO usermeta(path, date_added)
                           SELECT path, analyzed_at FROM features""")
            con.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('usermeta_backfilled','1')")
        con.commit()
    finally:
        con.close()


class UserData:
    """Write API over `usermeta` + the in-memory mirror the UI reads (via lib.row())."""

    def __init__(self, db_path, eng, lib, on_tags_changed=None):
        self.db_path = db_path
        self.eng = eng
        self.paths = eng.paths
        self.lib = lib
        self.on_tags_changed = on_tags_changed or (lambda i: None)
        self.lock = threading.Lock()
        self._gain_cache = {}
        paths = self.paths
        _ensure_schema(db_path)
        # attach the user columns to LibraryIndex so row()/sorts can serve them.
        n = len(paths)
        lib.rating = [0] * n
        lib.loved = [False] * n
        lib.plays = [0] * n
        lib.last_played = [0] * n
        lib.date_added = [0] * n
        idx = {p: i for i, p in enumerate(paths)}
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            for p, r, lv, pc, lp, da in con.execute(
                    "SELECT path, rating, loved, play_count, last_played, date_added FROM usermeta"):
                i = idx.get(p)
                if i is None:
                    continue
                lib.rating[i] = int(r or 0)
                lib.loved[i] = bool(lv)
                lib.plays[i] = int(pc or 0)
                lib.last_played[i] = int(lp or 0)
                lib.date_added[i] = int(da or 0)
        finally:
            con.close()

    def _write(self, sql, params):
        with self.lock:
            con = sqlite3.connect(self.db_path, timeout=30)
            try:
                con.execute("PRAGMA journal_mode=WAL")
                con.execute(sql, params)
                con.commit()
            finally:
                con.close()

    def set_rating(self, i, rating):
        self._write("""INSERT INTO usermeta(path, rating) VALUES(?,?)
                       ON CONFLICT(path) DO UPDATE SET rating=excluded.rating""",
                    (self.paths[i], rating))
        self.lib.rating[i] = rating

    def set_loved(self, i, loved):
        self._write("""INSERT INTO usermeta(path, loved) VALUES(?,?)
                       ON CONFLICT(path) DO UPDATE SET loved=excluded.loved""",
                    (self.paths[i], 1 if loved else 0))
        self.lib.loved[i] = bool(loved)

    def played(self, i):
        now = int(time.time())
        self._write("""INSERT INTO usermeta(path, play_count, last_played) VALUES(?,1,?)
                       ON CONFLICT(path) DO UPDATE SET
                         play_count = COALESCE(play_count,0) + 1,
                         last_played = excluded.last_played""",
                    (self.paths[i], now))
        self.lib.plays[i] += 1
        self.lib.last_played[i] = now
        return self.lib.plays[i]

    def skipped(self, i):
        self._write("""INSERT INTO usermeta(path, skip_count) VALUES(?,1)
                       ON CONFLICT(path) DO UPDATE SET
                         skip_count = COALESCE(skip_count,0) + 1""",
                    (self.paths[i],))

    # ------------------------------------------------------------------ tags
    def read_tags(self, i):
        import mutagen
        path = self.paths[i]
        f = mutagen.File(path, easy=True)
        tags = {}
        if f is not None and f.tags:
            for k in _EASY_TAGS:
                v = f.tags.get(k)
                if v:
                    tags[k] = str(v[0])
        info = {}
        if f is not None and f.info:
            info = {
                "length": round(getattr(f.info, "length", 0) or 0, 1),
                "bitrate": getattr(f.info, "bitrate", 0) or 0,
                "sample_rate": getattr(f.info, "sample_rate", 0) or 0,
                "channels": getattr(f.info, "channels", 0) or 0,
                "codec": type(f).__name__,
            }
        return {"tags": tags, "info": info,
                "file": os.path.basename(path),
                "folder": os.path.dirname(path),
                "bytes": os.path.getsize(path) if os.path.exists(path) else 0}

    def write_tags(self, i, new_tags):
        """Write the given easy-tag fields to the file, mirror them into `tracks`
        and the in-memory LibraryIndex. Returns the updated row dict."""
        import mutagen
        path = self.paths[i]
        f = mutagen.File(path, easy=True)
        if f is None:
            raise ValueError("unsupported or unreadable audio file")
        if f.tags is None:
            f.add_tags()
        for k in _EASY_TAGS:
            if k not in new_tags:
                continue
            v = str(new_tags[k]).strip()
            if v:
                f.tags[k] = [v]
            elif k in f.tags:
                del f.tags[k]
        f.save()

        # mirror into tracks (the columns it has) + the live index
        year = None
        m = re.match(r"\s*(\d{4})", str(new_tags.get("date", "")))
        if m:
            year = int(m.group(1))
        fields = {
            "artist": new_tags.get("artist"),
            "album": new_tags.get("album"),
            "title": new_tags.get("title"),
            "genre": new_tags.get("genre"),
            "year": year,
        }
        sets = ", ".join(f"{k}=?" for k, v in fields.items() if v is not None)
        vals = [v for v in fields.values() if v is not None]
        if sets:
            self._write(f"UPDATE tracks SET {sets} WHERE path=?", (*vals, path))
        changed = {k: v for k, v in fields.items() if v is not None}
        self.lib.update_row(i, changed)
        # keep the engine's own metadata (labels, export #EXTINF lines) in step
        m = self.eng.meta.get(path)
        if m is not None:
            m.update(changed)
        self.on_tags_changed(i)
        return self.lib.row(i)

    # ------------------------------------------------------------------ replaygain
    def gain(self, i):
        """ReplayGain track/album gain in dB read from the file's tags, or nulls.
        Cached — tag reads cost real I/O and gain never changes underneath us."""
        if i in self._gain_cache:
            return self._gain_cache[i]
        import mutagen
        out = {"track_gain": None, "album_gain": None}
        try:
            f = mutagen.File(self.paths[i])
            if f is not None and f.tags is not None:
                def _find(name):
                    for k in f.tags.keys():
                        ks = str(k).lower()
                        if name in ks:
                            v = f.tags[k]
                            txt = str(v.text[0] if hasattr(v, "text") else
                                      (v[0] if isinstance(v, list) else v))
                            m = _RG_RE.search(txt)
                            if m:
                                return float(m.group(1))
                    return None
                out["track_gain"] = _find("replaygain_track_gain")
                out["album_gain"] = _find("replaygain_album_gain")
        except Exception:
            pass
        self._gain_cache[i] = out
        return out

    # ------------------------------------------------------------------ delete
    # Per-track tables a delete must clear. Kept as one list so a future additive
    # table only needs adding here (and to libverify.py's _rekey_path).
    PER_TRACK_TABLES = ("tracks", "features", "clap", "usermeta", "filestate")

    def delete_track(self, i):
        """Transactionally remove track i's rows from every per-track table. NEVER
        touches the audio file on disk — the LAW here is "library bookkeeping only",
        the same non-destructive guarantee exportjob.py's copy job documents for its
        own writes. The in-RAM pool (self.eng/self.lib, and whichever engine is
        active) still holds this track until the next hot reload (see libreload.py) —
        deliberately not addressed here; the caller is expected to offer a reload."""
        return self.delete_path(self.paths[i])

    def delete_path(self, path):
        """delete_track's core, addressable by PATH: needed for rows the pool never
        held (analysis failures live in `tracks` but have no pool index — the 199
        _SYNCAPP dead rows, ruling B2 2026-08-04). Same transaction, same tables,
        same never-touches-the-audio-file guarantee."""
        con = sqlite3.connect(self.db_path, timeout=30)
        deleted = {}
        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("BEGIN IMMEDIATE")
            for table in self.PER_TRACK_TABLES:
                try:
                    cur = con.execute(f"DELETE FROM {table} WHERE path=?", (path,))
                    deleted[table] = cur.rowcount
                except sqlite3.OperationalError:
                    deleted[table] = 0     # table doesn't exist in this DB — nothing to drop
            con.commit()
        except Exception:
            con.rollback()
            raise
        finally:
            con.close()
        return {"path": path, "deleted": deleted}


def register(app, ctx):
    """ctx: dict(db_path, eng, lib, engine_lock, locked, on_tags_changed?). Returns the
    UserData instance."""
    eng = ctx["eng"]
    lib = ctx["lib"]
    locked = ctx["locked"]
    ud = UserData(ctx["db_path"], eng, lib, ctx.get("on_tags_changed"))
    bp = Blueprint("userdata", __name__)

    def _guard():
        # Deleting library rows is a this-machine action, same rule as
        # exportjob.py's copy endpoints and app.py's /api/fs/dirs.
        return request.remote_addr in ("127.0.0.1", "::1")

    def _idx(source):
        i = int(source.get("i"))
        if not (0 <= i < len(eng.paths)):
            raise IndexError
        return i

    @bp.post("/api/track/rate")
    @locked
    def rate():
        body = request.get_json(silent=True) or {}
        try:
            i = _idx(body)
            r = int(body.get("rating"))
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        if not (0 <= r <= 5):
            return jsonify(error="rating must be 0-5"), 400
        ud.set_rating(i, r)
        return jsonify(ok=True, i=i, rating=r)

    @bp.post("/api/track/loved")
    @locked
    def loved():
        body = request.get_json(silent=True) or {}
        try:
            i = _idx(body)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        ud.set_loved(i, bool(body.get("loved")))
        return jsonify(ok=True, i=i, loved=lib.loved[i])

    @bp.post("/api/track/played")
    @locked
    def played():
        body = request.get_json(silent=True) or {}
        try:
            i = _idx(body)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        return jsonify(ok=True, i=i, plays=ud.played(i))

    @bp.post("/api/track/skipped")
    @locked
    def skipped():
        body = request.get_json(silent=True) or {}
        try:
            i = _idx(body)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        ud.skipped(i)
        return jsonify(ok=True)

    @bp.get("/api/track/tags")
    @locked
    def get_tags():
        try:
            i = _idx(request.args)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        try:
            return jsonify(i=i, **ud.read_tags(i))
        except OSError as e:
            return jsonify(error=str(e)), 500

    @bp.post("/api/track/tags")
    @locked
    def set_tags():
        body = request.get_json(silent=True) or {}
        try:
            i = _idx(body)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        tags = body.get("tags")
        if not isinstance(tags, dict):
            return jsonify(error="expected {i, tags:{...}}"), 400
        try:
            row = ud.write_tags(i, tags)
        except (OSError, ValueError) as e:
            return jsonify(error=str(e)), 500
        return jsonify(ok=True, row=row)

    @bp.get("/api/track/gain")
    @locked
    def get_gain():
        try:
            i = _idx(request.args)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        return jsonify(i=i, **ud.gain(i))

    @bp.post("/api/reveal")
    @locked
    def reveal():
        """Open Explorer with the file selected. Windows + local use only — this runs
        on the machine the server runs on, which for the desktop app is the user's."""
        if sys.platform != "win32":
            return jsonify(error="reveal is Windows-only"), 501
        body = request.get_json(silent=True) or {}
        try:
            i = _idx(body)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        path = eng.paths[i]
        if not os.path.isfile(path):
            return jsonify(error="file missing on disk"), 404
        subprocess.Popen(["explorer", "/select,", path])
        return jsonify(ok=True)

    @bp.post("/api/track/delete")
    @locked
    def delete():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        body = request.get_json(silent=True) or {}
        # {path} form: for rows the pool never held (analysis failures — they have
        # no index anywhere; /api/lib/failures is where a caller learns the path).
        # Same guard, same lock, same transactional core (ruling B2 2026-08-04).
        if "path" in body and "i" not in body:
            p = str(body.get("path") or "")
            if not p:
                return jsonify(ok=False, error="bad request"), 400
            try:
                result = ud.delete_path(p)
            except sqlite3.Error as e:
                return jsonify(ok=False, error=str(e)), 500
            if not result["deleted"].get("tracks"):
                return jsonify(ok=False, error="unknown path"), 404
            return jsonify(ok=True, path=result["path"], deleted=result["deleted"])
        try:
            i = _idx(body)
        except (TypeError, ValueError):
            return jsonify(ok=False, error="bad request"), 400
        except IndexError:
            return jsonify(ok=False, error="unknown track"), 404
        row = lib.row(i)
        try:
            result = ud.delete_track(i)
        except sqlite3.Error as e:
            return jsonify(ok=False, error=str(e)), 500
        label = f"{row.get('artist') or '?'} - {row.get('title') or ''}"
        return jsonify(ok=True, i=i, path=result["path"], deleted=result["deleted"], label=label)

    app.register_blueprint(bp)
    return ud
