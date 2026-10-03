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
  GET  /api/track/tags?i=  every field a player edits, the rating, the cover (tagfile.py)
  POST /api/track/tags     {i, tags:{key: text}}    write the fields given to the file + tracks + memory
  GET  /api/track/cover?i= the embedded cover image, 404 when none
  POST /api/track/cover    multipart {i, file}      replace the cover (JPEG or PNG); DELETE {i} removes it
  POST /api/ratings/import                          read every file's own rating into Attune (a job)
  GET  /api/ratings/import/status
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
import logging
import re
import threading
import time
import sqlite3
import subprocess
import sys
import threading
import time

from flask import Blueprint, Response, jsonify, request

_HERE = os.path.dirname(os.path.abspath(__file__))


def _tagfile():
    """web/tagfile.py, loaded the way app.py loads this module (by path, so it works
    from the frozen exe and from a test alike)."""
    import importlib.util
    spec = importlib.util.spec_from_file_location("attune_tagfile", os.path.join(_HERE, "tagfile.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tagfile = _tagfile()
log = logging.getLogger("attune.userdata")

_RG_RE = re.compile(r"(-?\d+(?:\.\d+)?)")


def _readonly_uri(path):
    """file: URI for a read-only sqlite3 connect (mode=ro), with the path ESCAPED.
    f"file:{path}?mode=ro" is wrong for any path holding a '#' or a '?': sqlite3
    reads the '#' as the start of a fragment and silently opens a brand-new empty
    database somewhere else, so a real library reads as having no tracks in it.
    Same fix as web/app.py's _readonly_uri (web/app.py:175); duplicated here, not
    imported, because this module is self-contained by design (no intra-web imports,
    nothing under web/ imports src/db.py) and the frozen build loads every web/ and
    src/ module by explicit path, not via sys.path, so a new shared module would need
    build.py/Attune.spec updated to ship it -- out of scope for this fix."""
    from pathlib import Path
    return Path(os.path.abspath(path)).as_uri() + "?mode=ro"


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

    def __init__(self, db_path, eng, lib, on_tags_changed=None, plex_rate=None,
                 on_art_changed=None):
        self.db_path = db_path
        self.eng = eng
        self.paths = eng.paths
        self.lib = lib
        self.on_tags_changed = on_tags_changed or (lambda i: None)
        # plex_rate(path, stars): send the rating to the Plex server, or None when Plex is
        # not set up (app.py decides). Called off the request thread, never waited for.
        self.plex_rate = plex_rate
        self.on_art_changed = on_art_changed or (lambda path: None)
        self.import_job = None
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
        con = sqlite3.connect(_readonly_uri(db_path), uri=True)
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
        # Thousands of rapid open/write/close cycles against a WAL database (the ratings
        # import) hit, on Windows, a single transient "attempt to write a readonly
        # database" (the -shm handle racing another opener), which ended the whole pass
        # at 8,949 of 21,118 on 2026-10-02. Same three-attempt retry libverify.py and
        # audioinfo.py already use for the same reason.
        with self.lock:
            for attempt in (1, 2, 3):
                con = sqlite3.connect(self.db_path, timeout=30)
                try:
                    con.execute("PRAGMA journal_mode=WAL")
                    con.execute(sql, params)
                    con.commit()
                    break
                except sqlite3.OperationalError:
                    if attempt == 3:
                        raise
                    time.sleep(0.2 * attempt)
                finally:
                    con.close()

    def set_rating(self, i, rating, to_file=True, to_plex=True):
        """The stars go three places: the library (always), the file's own tags (so
        every other player sees them, TODO.md row 54) and, when Plex is set up and the
        switch is on, the Plex server. A file that cannot be written (read-only, on an
        unplugged drive) still gets the library rating; the answer says what happened."""
        self._write("""INSERT INTO usermeta(path, rating) VALUES(?,?)
                       ON CONFLICT(path) DO UPDATE SET rating=excluded.rating""",
                    (self.paths[i], rating))
        self.lib.rating[i] = rating
        out = {"file": None, "plex": None}
        if to_file:
            try:
                tagfile.write_rating(self.paths[i], rating)
                out["file"] = "written"
            except (OSError, ValueError) as e:
                out["file"] = f"not written: {e}"
        if to_plex and self.plex_rate is not None:
            path = self.paths[i]
            def _send():
                try:
                    self.plex_rate(path, rating)
                except Exception as e:          # best effort, never the user's problem
                    log.warning("Plex rating not sent for %s: %s", os.path.basename(path), e)
            threading.Thread(target=_send, name="plex-rate", daemon=True).start()
            out["plex"] = "sending"
        return out

    # ------------------------------------------------------------------ ratings in files
    def start_import(self):
        """Read every file's own rating (POPM, RATING) into the library, in a thread.
        The file wins: pressing the button means "what my files say is right". Songs
        whose file holds no rating are left as they are."""
        if self.import_job and self.import_job.get("running"):
            return self.import_job
        job = {"running": True, "cancelled": False, "started": time.time(), "done": 0,
               "total": len(self.paths), "changed": 0, "with_rating": 0, "unreadable": 0,
               "plex_sent": 0, "plex_failed": 0, "finished": None, "error": None}
        self.import_job = job

        def _run():
            changed = []
            try:
                for i, path in enumerate(self.paths):
                    if job["cancelled"]:
                        break
                    stars = tagfile.read_rating(path) if os.path.exists(path) else None
                    if stars is None:
                        job["unreadable"] += 1
                    elif stars > 0:
                        job["with_rating"] += 1
                        if stars != int(self.lib.rating[i] or 0):
                            # the file already holds it; Plex is told below, in this
                            # thread, one at a time, never a thread per song
                            self.set_rating(i, stars, to_file=False, to_plex=False)
                            changed.append((path, stars))
                            job["changed"] += 1
                    job["done"] = i + 1
                if self.plex_rate is not None and not job["cancelled"]:
                    for path, stars in changed:
                        try:
                            self.plex_rate(path, stars)
                            job["plex_sent"] += 1
                        except Exception as e:
                            job["plex_failed"] += 1
                            if job["plex_failed"] <= 3:
                                log.warning("Plex rating not sent for %s: %s", os.path.basename(path), e)
            except Exception as e:                   # pragma: no cover - defensive
                job["error"] = str(e)
            finally:
                job["running"] = False
                job["finished"] = time.time()

        threading.Thread(target=_run, name="ratings-import", daemon=True).start()
        return job

    def cancel_import(self):
        if self.import_job:
            self.import_job["cancelled"] = True
        return self.import_job

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
        """Every field (tagfile.FIELDS), the rating as the FILE holds it beside the
        library's, the cover's presence, the tag format and the file's facts."""
        path = self.paths[i]
        r = tagfile.read(path)
        r["file_rating"] = r.pop("rating", 0)
        r["rating"] = int(self.lib.rating[i] or 0)
        r.update({"file": os.path.basename(path), "folder": os.path.dirname(path),
                  "bytes": os.path.getsize(path) if os.path.exists(path) else 0,
                  "modified": os.path.getmtime(path) if os.path.exists(path) else 0})
        return r

    def write_tags(self, i, new_tags):
        """Write the fields given to the file (tagfile.write: the file's own tag
        version kept, its modified time put back), mirror the columns the library has
        into `tracks` and the in-memory LibraryIndex. Returns the updated row dict."""
        path = self.paths[i]
        tagfile.write(path, new_tags)

        # mirror into tracks (the columns it has) + the live index
        year = tagfile.year_of(new_tags.get("date", "")) if "date" in new_tags else None
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

    # ------------------------------------------------------------------ cover art
    def read_cover(self, i):
        return tagfile.read_cover(self.paths[i])

    def write_cover(self, i, data):
        tagfile.write_cover(self.paths[i], data)
        self.on_art_changed(self.paths[i])

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
    ud = UserData(ctx["db_path"], eng, lib, ctx.get("on_tags_changed"),
                  plex_rate=ctx.get("plex_rate"), on_art_changed=ctx.get("on_art_changed"))
    bp = Blueprint("userdata", __name__)
    app.extensions["attune_userdata"] = ud      # reachable by tests and by other modules

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
        where = ud.set_rating(i, r)
        return jsonify(ok=True, i=i, rating=r, **where)

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

    @bp.get("/api/track/cover")
    @locked
    def get_cover():
        try:
            i = _idx(request.args)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        try:
            c = ud.read_cover(i)
        except (OSError, ValueError) as e:
            return jsonify(error=str(e)), 500
        if not c:
            return Response(status=404)
        return Response(c[0], mimetype=c[1], headers={"Cache-Control": "no-store"})

    @bp.post("/api/track/cover")
    @locked
    def set_cover():
        """Replace the embedded cover with the JPEG or PNG uploaded as `file`. A
        this-machine action, like every other write to a song file."""
        if not _guard():
            return jsonify(error="only available on the Attune machine itself"), 403
        try:
            i = _idx(request.form)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        up = request.files.get("file")
        if up is None:
            return jsonify(error="expected a file"), 400
        data = up.read()
        if len(data) > 10 * 1024 * 1024:
            return jsonify(error="the image is over 10 MB"), 400
        try:
            ud.write_cover(i, data)
        except (OSError, ValueError) as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, i=i, bytes=len(data))

    @bp.delete("/api/track/cover")
    @locked
    def drop_cover():
        if not _guard():
            return jsonify(error="only available on the Attune machine itself"), 403
        body = request.get_json(silent=True) or {}
        try:
            i = _idx(body)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown track"), 404
        try:
            ud.write_cover(i, b"")
        except (OSError, ValueError) as e:
            return jsonify(error=str(e)), 400
        return jsonify(ok=True, i=i)

    @bp.post("/api/ratings/import")
    def ratings_import():
        if not _guard():
            return jsonify(error="only available on the Attune machine itself"), 403
        if (request.get_json(silent=True) or {}).get("cancel"):
            return jsonify(ud.cancel_import() or {"running": False})
        return jsonify(ud.start_import())

    @bp.get("/api/ratings/import/status")
    def ratings_import_status():
        return jsonify(ud.import_job or {"running": False, "finished": None})

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
