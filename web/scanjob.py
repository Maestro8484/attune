"""Attune scan job — wires the EXISTING incremental analyze pipeline to a button.

The pipeline (src/scan.py import-folder -> analyze, src/embed.py) is already
incremental, resumable, and safe to re-run (mtime-skip logic verified in db.py).
So this module deliberately stays dumb: it runs those CLIs as subprocesses under
the configured ML venv's python, captures their stdout, and serves progress to
the UI. No queue system, no service, no IPC protocol — a killed subprocess costs
nothing because the NEXT run picks up where it died.

Stages:  import (per library folder)  ->  analyze (librosa)  ->  embed (CLAP)

The import stage can run under the app's own interpreter (mutagen/ffprobe only);
analyze and embed genuinely need the heavy venv (librosa / torch). If
ml_venv_python is not configured, we run import-only and say so honestly.

Engine note (stated, not hidden): the running engine loads its pool ONCE at
startup, so tracks this job adds only become mixable after an app restart. The
status payload carries `new_tracks` so the UI can tell the user exactly that.

Endpoints:
  GET  /api/scan/status
  POST /api/scan/start    {folders?: [...]}   defaults to settings.library_folders
  POST /api/scan/cancel
"""
from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import threading
import time
from collections import deque

from flask import Blueprint, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")

_PROG_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def _db_counts(db_path):
    try:
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        t = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        a = con.execute("SELECT COUNT(*) FROM features WHERE vec IS NOT NULL").fetchone()[0]
        con.close()
        return {"tracks": t, "analyzed": a}
    except sqlite3.Error:
        return {"tracks": 0, "analyzed": 0}


class ScanJob:
    """At most one scan runs at a time; state is read lock-free by /status (GIL-safe
    reads of plain attributes; the deque bounds memory)."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.thread = None
        self.proc = None
        self.cancelled = False
        self.running = False
        self.stage = ""
        self.stages_done = []          # [{name, rc, seconds}]
        self.lines = deque(maxlen=60)  # rolling tail across all stages
        self.progress = None           # (cur, total) from the last progress-looking line
        self.started = 0
        self.finished = 0
        self.error = ""
        self.before = {}
        self.after = {}

    # ---------------------------------------------------------------- run
    def start(self, folders, ml_python):
        if self.running:
            raise RuntimeError("a scan is already running")
        self.__init__(self.db_path)     # reset state
        self.running = True
        self.started = int(time.time())
        self.before = _db_counts(self.db_path)
        self.thread = threading.Thread(
            target=self._run, args=(list(folders), ml_python), daemon=True)
        self.thread.start()

    def _exec(self, name, argv):
        """Run one stage, streaming stdout into the tail buffer. Returns rc."""
        self.stage = name
        self.progress = None
        t0 = time.time()
        self.lines.append(f"── {name}: {' '.join(os.path.basename(a) for a in argv[:2])} …")
        try:
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", cwd=SRC,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as e:
            self.lines.append(f"failed to start: {e}")
            self.stages_done.append({"name": name, "rc": -1, "seconds": 0})
            return -1
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            self.lines.append(line)
            m = _PROG_RE.search(line)
            if m:
                self.progress = (int(m.group(1)), int(m.group(2)))
        rc = self.proc.wait()
        self.proc = None
        self.stages_done.append({"name": name, "rc": rc,
                                 "seconds": round(time.time() - t0, 1)})
        return rc

    def _run(self, folders, ml_python):
        try:
            imp_python = ml_python or sys.executable
            heavy = bool(ml_python)
            for d in folders:
                if self.cancelled:
                    return
                if not os.path.isdir(d):
                    self.lines.append(f"skipping missing folder: {d}")
                    continue
                rc = self._exec(f"import {os.path.basename(d) or d}",
                                [imp_python, os.path.join(SRC, "scan.py"),
                                 "import-folder", d, "--db", self.db_path])
                if rc != 0:
                    self.error = f"import failed (rc={rc}) — see log tail"
                    return
            if not heavy:
                self.lines.append("No ML venv configured (Preferences → Advanced): "
                                  "imported metadata only; analyze/embed skipped.")
                return
            if self.cancelled:
                return
            rc = self._exec("analyze", [ml_python, os.path.join(SRC, "scan.py"),
                                        "analyze", "--db", self.db_path])
            if rc != 0:
                self.error = f"analyze failed (rc={rc}) — see log tail"
                return
            if self.cancelled:
                return
            rc = self._exec("embed", [ml_python, os.path.join(SRC, "embed.py"),
                                      "--db", self.db_path])
            if rc != 0:
                self.error = f"embed failed (rc={rc}) — see log tail"
        finally:
            self.after = _db_counts(self.db_path)
            self.finished = int(time.time())
            self.running = False
            self.stage = ""

    def cancel(self):
        self.cancelled = True
        p = self.proc
        if p and p.poll() is None:
            p.terminate()

    def status(self):
        after = self.after if not self.running else _db_counts(self.db_path)
        return {
            "running": self.running,
            "stage": self.stage,
            "stages": self.stages_done,
            "progress": self.progress,
            "lines": list(self.lines),
            "started": self.started,
            "finished": self.finished,
            "cancelled": self.cancelled,
            "error": self.error,
            "before": self.before,
            "counts": after,
            "new_tracks": max(0, (after.get("tracks", 0) or 0)
                              - (self.before.get("tracks", 0) or 0)),
        }


def register(app, ctx):
    """ctx: dict(db_path, load_settings=callable->dict)."""
    # Resolve to an absolute path NOW, in the app's cwd. The scan stages run as
    # subprocesses with cwd=SRC (attune/src), so a relative --db (e.g. the launcher's
    # "mixer-ng/data/mixer.db") would resolve against SRC and fail to open. abspath
    # here binds it to the app's working dir, where it is already known to resolve.
    job = ScanJob(os.path.abspath(ctx["db_path"]))
    load_settings = ctx["load_settings"]
    bp = Blueprint("scanjob", __name__)

    @bp.get("/api/scan/status")
    def scan_status():
        return jsonify(job.status())

    @bp.post("/api/scan/start")
    def scan_start():
        body = request.get_json(silent=True) or {}
        s = load_settings()
        folders = body.get("folders") or s.get("library_folders") or []
        folders = [f for f in folders if isinstance(f, str) and f.strip()]
        if not folders:
            return jsonify(ok=False, error="no library folders configured "
                           "(Preferences → Library)"), 400
        ml = (s.get("ml_venv_python") or "").strip()
        if ml and not os.path.isfile(ml):
            return jsonify(ok=False, error=f"ml_venv_python not found: {ml}"), 400
        try:
            job.start(folders, ml)
        except RuntimeError as e:
            return jsonify(ok=False, error=str(e)), 409
        return jsonify(ok=True)

    @bp.post("/api/scan/cancel")
    def scan_cancel():
        job.cancel()
        return jsonify(ok=True)

    app.register_blueprint(bp)
    return job
