r"""Mirror a folder of loose audio files onto a Plex playlist, from inside Attune.

The manual road this replaces: build a folder of mp3s, copy it onto a USB stick, carry
the stick to the car. The folder stays the roster of record; this makes a Plex playlist
hold the same songs so they can be streamed instead.

Two steps, deliberately, and they are NOT one button:

  POST /api/plexsync/preview   reads the folder and the Plex library, resolves every
                               file, and parks the answer. Changes nothing.
  POST /api/plexsync/apply     writes the playlist -- using EXACTLY the preview that is
                               already parked, never a fresh resolve of its own.

That split is the whole point. Apply refuses unless a completed preview for the same
folder AND the same playlist title is sitting in memory, so the thing that gets written
is the thing the operator actually read. A single "sync" button would let the folder
change between looking and writing, and the operator would be approving a list nobody
had seen.

Shape is cloned from ``exportjob.py``: one background job at a time behind a
module-level ``_START_LOCK``, state read lock-free by ``/status``, a
``register(app, ctx)`` wiring the endpoints and returning the job. A killed preview
costs nothing (nothing was written); a killed apply leaves a partially-updated playlist
that the next preview describes honestly and the next apply finishes.

Read-only with one exception: the Plex playlist. It never opens mixer.db, never writes
a settings file except the two remembered fields, and never touches an audio file for
anything but its tags. The matching itself is ``src/plexmatch.py``.

Endpoints (all loopback-guarded like ``/api/export/copy`` -- reading a local folder and
writing to the household Plex server is a this-machine action, not something a LAN
client should drive):
  GET  /api/plexsync/status
  POST /api/plexsync/preview  {folder, title, order?}
  POST /api/plexsync/apply    {folder, title, prune?}

`title` is a TEMPLATE, not a literal: "DrivingTunesUSB ({date})" resolves to
"DrivingTunesUSB (26-09-01)". It is expanded ONCE, during preview, and the resolved name
is carried to apply -- expanding it again at write time would let a run started at 23:59
create a playlist under a different name than the one that was approved.

The template therefore decides what the sync DOES, and the panel shows the resolved name
before anything is written: a fixed name updates one playlist forever, a name carrying
{date} makes a new one each day and leaves yesterday's standing.
  POST /api/plexsync/cancel
  POST /api/plexsync/rescan   ask Plex to re-read the library from disk
  POST /api/plexsync/open     open the playlist in Plex's own web app, in a real browser

The last two exist because of two things that actually happened. A track restored to
the library minutes ago is genuinely not in Plex's index yet, so the report says "not in
your library" and is honestly, uselessly right -- /rescan is the fix, and the status
payload carries Plex's own scan progress so "still scanning" and "really missing" stop
looking identical. And a count Attune prints is Attune's word for it: /open puts the
real playlist on screen in Plex, so the confirmation is the operator's eyes, not ours.
"""
from __future__ import annotations

import importlib.util
import os
import threading
import time
from collections import deque

from flask import Blueprint, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")

# Module-level for the same reason exportjob.py gives: start() resets instance state, so
# an instance-level lock would wipe itself mid-guard.
_START_LOCK = threading.Lock()


def _load(name):
    """Import a src/ module by file path -- the pattern app.py already uses, and the
    reason every one of these files is listed in desktop/build.py's GUI_DATA.

    Called once per module, at register() time. It used to run on every request and
    every worker run, re-executing the file each time and minting a fresh module object
    -- harmless here since neither module keeps state, but pointless work on a path
    that polls every 700 ms."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(SRC, name + ".py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Filled by register(); the worker and the routes read these, never _load() again.
_MODS = {}


class PlexSyncJob:
    """At most one preview or apply runs at a time; the last preview is kept."""

    def __init__(self):
        self.thread = None
        self.running = False
        self.phase = ""            # 'reading plex' | 'matching' | 'writing' | ''
        self.cancelled = False
        self.error = ""
        self.folder = ""
        self.title = ""
        self.indexed = 0           # tracks read out of the Plex library
        self.index_total = 0
        self.started = 0
        self.finished = 0
        self.lines = deque(maxlen=40)
        self.preview = None        # the parked report, see _shape()
        self.applied = None        # what the last apply actually did
        self.order = "folder"      # running order asked for
        self.scanning = False      # Plex is re-reading the library right now
        self.scan_pct = None       # its own progress number, when it gives one
        self._lock = threading.Lock()

    def _log(self, msg):
        with self._lock:
            self.lines.append(msg)

    # ------------------------------------------------------------------ preview
    def start_preview(self, folder, title, order, connect):
        with _START_LOCK:
            if self.running:
                raise RuntimeError("a Plex sync is already running")
            keep_applied = self.applied
            self.__init__()
            self.applied = keep_applied
            self.running = True
            self.folder = folder
            self.title = title            # the TEMPLATE as typed
            self.order = order
            self.started = int(time.time())
            self.thread = threading.Thread(target=self._run_preview,
                                           args=(folder, title, order, self.started,
                                                 connect),
                                           daemon=True)
            self.thread.start()

    def _run_preview(self, folder, template, order, seed, connect):
        try:
            plexmatch = _MODS["plexmatch"]
            self.phase = "reading plex"
            self._log("Reading your Plex library…")
            px = connect()

            def progress(done, total):
                self.indexed, self.index_total = done, total

            tracks = px.build_meta_index(progress=progress)
            if self.cancelled:
                raise RuntimeError("cancelled")
            self._log(f"{len(tracks)} tracks in the library.")

            self.phase = "matching"
            self._log(f"Matching the files in {os.path.basename(folder) or folder}…")
            catalog = plexmatch.PlexCatalog(tracks)
            # Everything under the folder counts, subfolders included, dropped in now
            # or later -- operator ruling 2026-09-01. Not a setting, so there is no
            # switch here to get out of step with plexmatch's own default.
            rep = plexmatch.resolve_folder(catalog, folder, recursive=True,
                                           prefer_zone=plexmatch.zone_chooser(px))
            if self.cancelled:
                raise RuntimeError("cancelled")

            # Stamped once, here, and carried to apply -- same reason the shuffle
            # seed is the preview's own start time: the list looked at is the list
            # written.
            title = plexmatch.resolve_title(template)
            rep["resolved"] = plexmatch.order_resolved(rep["resolved"], order, seed)
            existing = px.find_playlist(title)
            now = len(px.playlist_items(existing["ratingKey"])) if existing else None
            # If Plex is mid-scan, anything reported missing may simply not be indexed
            # yet. Recorded on the preview so the UI can say which of the two it is
            # instead of leaving the operator to guess.
            self.scanning, self.scan_pct = px.scan_activity()
            self.preview = _shape(rep, title, now,
                                  px.web_url(existing["ratingKey"]) if existing else "",
                                  self.scanning, template, order)
            c = rep["counts"]
            self._log(f"{c['resolved']} of {c['source']} matched, "
                      f"{c['residue']} not in your library.")
        except Exception as e:                                  # noqa: BLE001
            self.error = "" if self.cancelled else _friendly(e)
        finally:
            self.phase = ""
            self.running = False
            self.finished = int(time.time())

    # -------------------------------------------------------------------- apply
    def start_apply(self, folder, template, prune, connect):
        """Refuses anything but the parked preview for this exact folder and template.

        Matched on the TEMPLATE the operator has on screen, then written under the
        RESOLVED name the preview stamped -- so the playlist created is the one the panel
        named, even if the clock has since rolled past midnight.
        """
        with _START_LOCK:
            if self.running:
                raise RuntimeError("a Plex sync is already running")
            p = self.preview
            if not p:
                raise RuntimeError("look at the folder first, then apply what you saw")
            if p["folder"] != folder or p["template"] != template:
                raise RuntimeError("that preview was for a different folder or playlist "
                                   "name -- run the check again")
            if not p["keys"]:
                raise RuntimeError("nothing in that folder could be matched, so there is "
                                   "nothing to put in the playlist")
            self.running = True
            self.cancelled = False
            self.error = ""
            self.folder = folder
            self.title = template
            self.started = int(time.time())
            self.thread = threading.Thread(target=self._run_apply,
                                           args=(p["title"], list(p["keys"]), prune,
                                                 connect),
                                           daemon=True)
            self.thread.start()

    def _run_apply(self, title, keys, prune, connect):
        try:
            self.phase = "writing"
            self._log(f"Updating '{title}' on Plex…")
            px = connect()

            def moving(done, total):
                self.indexed, self.index_total = done, total

            res = px.sync_playlist(title, keys, prune=prune, order=True,
                                   progress=moving)
            if res.get("error"):
                raise RuntimeError(res["error"])
            self.applied = {"title": title, "playlist": res["playlist"],
                            "created": res["created"], "before": res["before"],
                            "after": res["after"], "added": res["added"],
                            "removed": res["removed"], "web_url": res.get("web_url", ""),
                            "ordered": res.get("ordered"),
                            "asked": len(keys), "when": int(time.time())}
            verb = "Created" if res["created"] else "Updated"
            self._log(f"{verb} '{title}': {res['before']} → {res['after']} tracks, "
                      f"{res['added']} added, {res['removed']} removed"
                      + (", in the order you asked for."
                         if res.get("ordered") else "."))
            if res.get("ordered") is False:
                # Said out loud rather than swallowed: the tracks are all there, the
                # running order is not what was asked for. Read back off the server.
                self._log("The tracks are all there, but Plex did not keep the "
                          "running order. Try the update again.")
        except Exception as e:                                  # noqa: BLE001
            self.error = _friendly(e)
        finally:
            self.phase = ""
            self.running = False
            self.finished = int(time.time())

    def cancel(self):
        # Honoured between the two long steps of a preview. An apply is a couple of
        # short calls and is never interrupted part-way -- a half-written playlist is
        # worse than a finished one -- so the panel hides the Cancel button while
        # writing rather than showing one that does nothing.
        self.cancelled = True

    def status(self):
        with self._lock:
            lines = list(self.lines)
        return {"running": self.running, "phase": self.phase, "error": self.error,
                "folder": self.folder, "title": self.title,
                "indexed": self.indexed, "index_total": self.index_total,
                "started": self.started, "finished": self.finished,
                "lines": lines, "preview": self.preview, "applied": self.applied,
                "scanning": self.scanning, "scan_pct": self.scan_pct,
                "done": bool(self.started) and not self.running}


def _shape(rep, title, existing_count, web_url="", scanning=False, template="",
           order="folder"):
    """The preview, trimmed to what the UI shows and what apply needs.

    Carries `keys` -- the exact ratingKeys apply will write -- so the list that was
    looked at and the list that gets written are the same object, not two resolves that
    could disagree."""
    def row(r, status):
        t = r.get("track") or {}
        return {"file": r["file"], "status": status, "grade": r.get("grade"),
                "tier": r.get("tier"), "drift": r.get("drift"),
                "artist": t.get("artist"), "title": t.get("title"),
                "plex": (t.get("files") or [""])[0],
                "tied": r.get("tied") or [],
                "looked_for": f"{r.get('artist') or '?'} / {r.get('title') or '?'}",
                "secs": r.get("secs")}

    flagged_files = {f["file"] for f in rep["flagged"]}
    matched = [row(r, "flagged" if r["file"] in flagged_files else "matched")
               for r in rep["resolved"]]
    return {"folder": rep["folder"], "title": title, "template": template,
            "order": order,
            "counts": rep["counts"], "existing": existing_count,
            "web_url": web_url, "scanning": bool(scanning),
            "keys": [r["track"]["rk"] for r in rep["resolved"]],
            "matched": matched,
            "missing": [row(r, "missing") for r in rep["residue"]],
            "dupes": rep["dupes"],
            "flagged": [row(f, "flagged") for f in rep["flagged"]]}


def _friendly(e):
    """Turn the usual failures into something that names the fix, not the exception."""
    s = str(e) or e.__class__.__name__
    low = s.lower()
    if "cancelled" in low:
        return ""
    if "getaddrinfo" in low or "refused" in low or "timed out" in low or "urlopen" in low:
        return ("Could not reach your Plex server. Is it switched on, and is the "
                "address in .env still right?")
    if "401" in s or "unauthorized" in low:
        return "Plex refused the token in .env. It may have expired."
    if "winerror 53" in low or "cannot find the path" in low or "no such file" in low:
        return "That folder is not reachable. Is the drive connected?"
    return s


def register(app, ctx):
    """ctx: dict(cfgmod, locked). Connection settings come from .env via src/export.py,
    exactly as /api/export/plex already does -- the token never moves into settings.json.
    """
    cfgmod = ctx["cfg"]
    locked = ctx["locked"]
    job = PlexSyncJob()
    bp = Blueprint("plexsyncjob", __name__)
    _MODS["plexmatch"] = _load("plexmatch")
    _MODS["export"] = _load("export")

    def _connect():
        """Build a PlexExporter, or raise a message naming what is missing."""
        export = _MODS["export"]
        env = export.load_env()
        settings = cfgmod.load()
        mapper = export.mapper_from_settings(settings, env)
        missing = [k for k in ("PLEX_URL", "PLEX_ACCOUNT_TOKEN", "PLEX_MACHINE_ID")
                   if not env.get(k)]
        if missing:
            raise RuntimeError("Plex is not set up on this machine yet: .env is missing "
                               + ", ".join(missing))
        return export.PlexExporter(env["PLEX_URL"], env["PLEX_ACCOUNT_TOKEN"],
                                   env.get("PLEX_SECTION_KEY", "1"),
                                   env["PLEX_MACHINE_ID"], mapper, timeout=60)

    def _guard():
        # Reading a local folder and writing to the household Plex server is a
        # this-machine action, same reasoning as /api/export/copy.
        return request.remote_addr in ("127.0.0.1", "::1")

    def _remember(folder, title, order):
        """Persist the panel's three fields so it comes back filled in. Best effort: a
        settings write that fails must not fail the sync the operator asked for.

        `title` is stored as the TEMPLATE he typed, not the name it resolved to -- storing
        the resolved name would silently freeze today's date into tomorrow's run."""
        try:
            s = cfgmod.load()
            if (s.get("plex_sync_folder") != folder or s.get("plex_sync_title") != title
                    or s.get("plex_sync_order") != order):
                s["plex_sync_folder"] = folder
                s["plex_sync_title"] = title
                s["plex_sync_order"] = order
                cfgmod.save(s)
        except Exception:                                       # noqa: BLE001
            pass

    def _fields():
        body = request.get_json(silent=True) or {}
        folder = (body.get("folder") or "").strip()
        title = (body.get("title") or "").strip()
        return body, folder, title

    @bp.get("/api/plexsync/status")
    def ps_status():
        if not _guard():
            return jsonify(error="only available on the Attune machine itself"), 403
        return jsonify(job.status())

    @bp.post("/api/plexsync/preview")
    @locked
    def ps_preview():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        body, folder, title = _fields()
        if not folder or not os.path.isdir(folder):
            return jsonify(ok=False, error="that folder was not found"), 400
        if not title:
            return jsonify(ok=False, error="give the playlist a name"), 400
        try:
            order = body.get("order")
            if order not in _MODS["plexmatch"].ORDERS:
                order = "folder"
            job.start_preview(folder, title, order, _connect)
        except RuntimeError as e:
            return jsonify(ok=False, error=str(e)), 409
        _remember(folder, title, order)
        return jsonify(ok=True)

    @bp.post("/api/plexsync/apply")
    @locked
    def ps_apply():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        body, folder, title = _fields()
        try:
            job.start_apply(folder, title, not body.get("no_prune"), _connect)
        except RuntimeError as e:
            return jsonify(ok=False, error=str(e)), 409
        return jsonify(ok=True)

    @bp.post("/api/plexsync/cancel")
    def ps_cancel():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        job.cancel()
        return jsonify(ok=True)

    @bp.post("/api/plexsync/rescan")
    def ps_rescan():
        """Ask Plex to re-read the library. Returns immediately; the scan runs on the
        server for minutes. The next check reports its progress rather than pretending
        a mid-scan library is the final answer."""
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        try:
            px = _connect()
            px.rescan()
            scanning, pct = px.scan_activity()
        except Exception as e:                                  # noqa: BLE001
            return jsonify(ok=False, error=_friendly(e)), 502
        return jsonify(ok=True, scanning=scanning, pct=pct)

    @bp.post("/api/plexsync/open")
    def ps_open():
        """Open the playlist in Plex's own web app, in the operator's real browser.

        Deliberately server-side (``webbrowser.open``) rather than a link in the page.
        Attune's shipped surface is a pywebview window, where a plain external link can
        land in the embedded webview or nowhere at all; handing the URL to the OS puts it
        in the real browser every time. Loopback-guarded like everything else here --
        opening a window is a this-machine action.
        """
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        body = request.get_json(silent=True) or {}
        url = (body.get("url") or "").strip()
        # Only ever open a URL this module produced, pointed at the configured server.
        # A url straight off a request body is caller-controlled; refusing anything that
        # is not our own Plex web link keeps this from becoming an open-anything hole.
        try:
            base = _connect().url
        except Exception as e:                                  # noqa: BLE001
            return jsonify(ok=False, error=_friendly(e)), 502
        if not url or not url.startswith(base + "/web/"):
            return jsonify(ok=False, error="that is not a Plex link for your server"), 400
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception as e:                                  # noqa: BLE001
            return jsonify(ok=False, error=str(e)), 500
        return jsonify(ok=True, url=url)

    app.register_blueprint(bp)
    return job
