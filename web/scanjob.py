r"""Attune scan job — wires the EXISTING incremental analyze pipeline to a button.

The pipeline (src/scan.py import-folder -> analyze, src/embed.py) is already
incremental, resumable, and safe to re-run (mtime-skip logic verified in db.py).
So this module deliberately stays dumb: it runs those CLIs as subprocesses under
the configured ML venv's python, captures their stdout, and serves progress to
the UI. No queue system, no service, no IPC protocol — a killed subprocess costs
nothing because the NEXT run picks up where it died.

Stages:  import (per library folder)  ->  analyze (librosa)  ->  embed (CLAP)

Interpreter routing for the heavy (analyze/embed) stages:
  - ml_venv_python configured  -> reference/retrain path: that venv runs scan.py
    analyze + embed.py (the original torch/transformers CLAP encoder).
  - no ml_venv_python (dev)     -> standalone path: the app's OWN interpreter runs
    scan.py analyze + embed_onnx.py (numpy + librosa + onnxruntime, torch-free —
    the ONNX CLAP twin proven bit-faithful in ROADMAP_STANDALONE Phase A). No
    second Python environment to install or configure.
  - no ml_venv_python (frozen)  -> the analyzer program: analyzer\AttuneAnalyzer.exe,
    which ships in a subfolder beside the GUI exe and carries librosa/onnxruntime,
    the CLAP encoder and a bundled ffmpeg. sys.executable is the packaged GUI exe,
    which no longer contains any of that (it is ~800 MB the mixing path never uses),
    so the work goes to the analyzer instead of re-entering this exe. If that
    subfolder is absent, refuse honestly and name the expected path.

The standalone path needs librosa + onnxruntime importable by the app's own
interpreter; embed_onnx.py exits with an honest message if librosa is absent.

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

# Module-level (NOT an instance attribute — start() calls self.__init__, which would
# wipe an instance-level lock along with everything else it resets). Guards the whole
# check-then-reset-then-launch body of start(): with autoscan.py now able to trigger
# a scan from its own background thread alongside the HTTP /api/scan/start endpoint,
# two callers could both pass the `if self.running` check before either set it True.
_START_LOCK = threading.Lock()

# The analyzer ships as its own program in a subfolder next to the frozen GUI exe
# (see desktop/analyzer_main.py). Kept as a function, not a constant, because
# sys.executable is only meaningful at call time (and the tests fake it).
ANALYZER_SUBDIR = "analyzer"
ANALYZER_EXE = "AttuneAnalyzer.exe"


def _analyzer_exe():
    return os.path.join(os.path.dirname(os.path.abspath(sys.executable)),
                        ANALYZER_SUBDIR, ANALYZER_EXE)


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
        with _START_LOCK:
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
            # Interpreter + embed script for the heavy (analyze/embed) stages.
            #   ml_python set        -> reference/retrain path: ML venv + embed.py
            #                           (torch/transformers, the original encoder).
            #   no ml_python, dev    -> standalone path: the app's OWN interpreter +
            #                           embed_onnx.py (numpy+librosa+onnxruntime), so
            #                           there is no second Python environment to set up.
            #   no ml_python, frozen -> analyzer program: sys.executable is the packaged
            #                           GUI exe, not a Python, and the lean GUI bundle no
            #                           longer carries librosa/onnxruntime/the CLAP model.
            #                           analyzer\AttuneAnalyzer.exe (beside the GUI exe)
            #                           does, and takes the same tool argv the old
            #                           `--attune-worker` sentinel took.
            frozen = getattr(sys, "frozen", False)
            worker_mode = frozen and not ml_python
            analyzer = _analyzer_exe() if worker_mode else None
            if worker_mode and not os.path.isfile(analyzer):
                self.error = ("the analyzer component is missing — expected "
                              f"{analyzer}. Reinstall Attune, or set an ML venv in "
                              "Preferences → Advanced to analyze with your own Python.")
                return
            if ml_python:
                heavy_python = ml_python
                embed_script = os.path.join(SRC, "embed.py")
                embed_label = "embed"
            elif worker_mode:
                heavy_python = None     # unused: _scan_argv/_embed_argv route via worker_mode
                embed_script = None
                embed_label = "embed (onnx)"
            else:
                heavy_python = sys.executable
                embed_script = os.path.join(SRC, "embed_onnx.py")
                embed_label = "embed (onnx)"
            imp_python = ml_python or sys.executable

            def _scan_argv(python_exe, *scan_args):
                """argv for a scan.py invocation. In worker_mode, hand the work to the
                analyzer program (which contains scan.py and its heavy deps);
                otherwise run scan.py directly under python_exe."""
                if worker_mode:
                    return [analyzer, "scan", *scan_args]
                return [python_exe, os.path.join(SRC, "scan.py"), *scan_args]

            def _embed_argv():
                if worker_mode:
                    return [analyzer, "embed_onnx", "--db", self.db_path]
                return [heavy_python, embed_script, "--db", self.db_path]

            for d in folders:
                if self.cancelled:
                    return
                if not os.path.isdir(d):
                    self.lines.append(f"skipping missing folder: {d}")
                    continue
                rc = self._exec(f"import {os.path.basename(d) or d}",
                                _scan_argv(imp_python, "import-folder", d,
                                           "--db", self.db_path))
                if rc != 0:
                    self.error = f"import failed (rc={rc}) — see log tail"
                    return
            if self.cancelled:
                return
            rc = self._exec("analyze", _scan_argv(heavy_python, "analyze",
                                                   "--db", self.db_path))
            if rc != 0:
                self.error = f"analyze failed (rc={rc}) — see log tail"
                return
            if self.cancelled:
                return
            rc = self._exec(embed_label, _embed_argv())
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
