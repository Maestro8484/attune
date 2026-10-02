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
startup, so tracks this job adds are not mixable the moment it finishes. Since S11
that no longer means restarting the app -- web/libreload.py rebuilds the pool in
place (POST /api/lib/reload) and the UI offers it as one button. The status payload
carries BOTH deltas so the UI knows there is something to load:

  new_tracks    rows added to the catalog by the import stage
  new_analyzed  rows that GAINED a librosa feature vector
  new_embedded  rows that GAINED a CLAP vector -- the stage that makes a track mixable

All three are needed, and each covers a resume the others miss. A scan resumed after
a cancel imports nothing the second time (every file is already in the catalog), so
new_tracks is 0. Cancel one BETWEEN the analyze and embed stages and run it again and
new_analyzed is 0 as well, while every track in the folder becomes usable. Offering
the reload on new_tracks alone left exactly those runs unmixable until the next
restart. (Raised by the Codex reads of this design and of the change, 2026-09-20.)

Endpoints:
  GET  /api/scan/status
  POST /api/scan/start    {folders?: [...]}   defaults to settings.library_folders
  POST /api/scan/cancel
"""
from __future__ import annotations

import logging
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


def _db_counts(db_path):
    try:
        con = sqlite3.connect(_readonly_uri(db_path), uri=True)
        t = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        a = con.execute("SELECT COUNT(*) FROM features WHERE vec IS NOT NULL").fetchone()[0]
        # The embed stage is the THIRD thing a scan does and the only one that makes a
        # track mixable. Counting it separately is what lets the UI tell a run that did
        # nothing apart from a run whose import and analyze stages were already done:
        # cancel a first scan between analyze and embed, run it again, and both the other
        # deltas come back zero while every track in the folder becomes usable.
        # Its own guard: a library that has never been embedded has no `clap` table at
        # all, and that must not zero the two counts above.
        # (Raised by the Codex audit of this change, 2026-09-20.)
        try:
            e = con.execute("SELECT COUNT(*) FROM clap WHERE vec IS NOT NULL").fetchone()[0]
        except sqlite3.OperationalError:
            e = 0
        con.close()
        return {"tracks": t, "analyzed": a, "embedded": e}
    except sqlite3.Error:
        return {"tracks": 0, "analyzed": 0, "embedded": 0}


# Seconds one song takes to fingerprint on a processor, measured 2026-10-02 over 200 songs on
# one desktop PC (decode, mel and the model). Only for the estimate the window shows before a
# library is fingerprinted again; the live figure during the run comes from the run itself.
FINGERPRINT_SECONDS_A_SONG = 1.6
# The sentence embed_onnx.py's refusal starts with (src/embed_onnx.py, _refuse_to_mix).
_REFUSED_MARK = "NOT fingerprinted"


def fingerprint_state(db_path, weights):
    """What the window needs to know about a library's sound fingerprints. `weights` is the kind
    this build makes. kind is:
      "earlier"  it holds fingerprints and does not say this build's kind made them (a library
                 from Attune 0.1.0 to 0.1.2): new songs cannot be fingerprinted into it until
                 every song is done again;
      "current"  it says this build's kind;
      "empty"    no fingerprint yet and nothing said, which a first scan takes care of.
    `waiting` is how many songs have no fingerprint row at all, `todo` how many the job would do."""
    out = {"kind": "empty", "songs": 0, "fingerprints": 0, "waiting": 0, "todo": 0,
           "seconds_a_song": FINGERPRINT_SECONDS_A_SONG, "est_seconds": 0}
    try:
        con = sqlite3.connect(_readonly_uri(db_path), uri=True)
    except sqlite3.Error:
        return out
    try:
        out["songs"] = con.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        try:
            out["fingerprints"] = con.execute(
                "SELECT COUNT(*) FROM clap WHERE vec IS NOT NULL").fetchone()[0]
            out["waiting"] = con.execute(
                "SELECT COUNT(*) FROM tracks t WHERE NOT EXISTS "
                "(SELECT 1 FROM clap c WHERE c.path = t.path)").fetchone()[0]
        except sqlite3.OperationalError:
            out["waiting"] = out["songs"]
        try:
            row = con.execute("SELECT value FROM meta WHERE key='clap_weights'").fetchone()
        except sqlite3.OperationalError:
            row = None
        says = row[0] if row else None
    except sqlite3.Error:
        return out
    finally:
        con.close()
    if says == weights:
        out["kind"] = "current"
    elif says is not None or out["fingerprints"]:
        out["kind"] = "earlier"
    out["todo"] = out["songs"] if out["kind"] == "earlier" else out["waiting"]
    out["est_seconds"] = int(out["todo"] * FINGERPRINT_SECONDS_A_SONG)
    return out


class ScanJob:
    """At most one scan runs at a time; state is read lock-free by /status (GIL-safe
    reads of plain attributes; the deque bounds memory)."""

    # The module-level start lock, exposed so the ONE other job that must not overlap a
    # scan, the library reload in libreload.py, can take the same lock around its own
    # check-and-start. The two modules are loaded by file path and cannot import each
    # other, so the lock travels on the object instead. ISSUES.md row 2, 2026-09-21.
    start_lock = _START_LOCK

    def __init__(self, db_path, logger=None, load_settings=None):
        self.db_path = db_path
        # Read at the moment a scan runs, by the job itself, so no caller of start() can
        # forget to pass a scan option along (the exclude_folders lesson, autoscan.py).
        self.load_settings = load_settings
        # Structured logging, first slice (AUDIT_FABLE_2026-07-28.md S2 item 9): a
        # logger routed into <config_dir()>/logs/scan.log by applog.py, handed in by
        # register() below. Falls back to a bare, handler-less logger (a silent no-op
        # -- stdlib logging drops records with no handlers anywhere up the hierarchy)
        # so ScanJob stays usable standalone (e.g. from a test) without wiring applog.
        self.logger = logger or logging.getLogger("attune.scan")
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
        self.kind = "scan"             # "scan", or "fingerprints" (start_fingerprints below)

    # ---------------------------------------------------------------- run
    def start_fingerprints(self, ml_python, redo_all):
        """Fingerprint the library again, or carry on a run of that which was stopped: the one
        stage `embed_onnx --start-over`, which copies the library, clears the earlier
        fingerprints and marks the new kind when the library is of an earlier kind, and
        otherwise just fingerprints the songs that have none. Same lock, same status, same
        cancel as a scan, so the two can never overlap. `redo_all` says every song is being
        done again, so every fingerprint the run leaves counts as new."""
        with _START_LOCK:
            if self.running:
                raise RuntimeError("a scan is already running")
            self.__init__(self.db_path, self.logger, self.load_settings)
            self.kind = "fingerprints"
            self.running = True
            self.started = int(time.time())
            self.before = _db_counts(self.db_path)
            if redo_all:
                self.before["embedded"] = 0
            self.logger.info("fingerprints started: redo_all=%s ml_python=%s", redo_all,
                             ml_python or "(standalone/analyzer)")
            self.thread = threading.Thread(target=self._run_fingerprints, args=(ml_python,),
                                           daemon=True)
            self.thread.start()

    def _run_fingerprints(self, ml_python):
        try:
            # Always the ONNX stage: it is the one that carries --start-over, and the one a
            # packaged copy has. Frozen: the analyzer program. From source: the configured
            # analysis Python if there is one (it has librosa and onnxruntime), else this one.
            if getattr(sys, "frozen", False):
                analyzer = _analyzer_exe()
                if not os.path.isfile(analyzer):
                    self.logger.error("analyzer exe missing at %s", analyzer)
                    self.error = ("Part of Attune is missing, so it cannot read music. "
                                  "Reinstalling Attune puts it back.")
                    return
                argv = [analyzer, "embed_onnx", "--db", self.db_path, "--start-over"]
            else:
                argv = [ml_python or sys.executable, os.path.join(SRC, "embed_onnx.py"),
                        "--db", self.db_path, "--start-over"]
            rc = self._exec("fingerprints", argv)
            if rc != 0 and not self.cancelled:
                self.error = "Fingerprinting stopped before it finished. The log below says why."
        finally:
            self._finish()

    def _finish(self):
        self.after = _db_counts(self.db_path)
        self.finished = int(time.time())
        self.running = False
        self.stage = ""
    def start(self, folders, ml_python, exclude=()):
        with _START_LOCK:
            if self.running:
                raise RuntimeError("a scan is already running")
            # reset state, keep the logger and the settings reader
            self.__init__(self.db_path, self.logger, self.load_settings)
            self.running = True
            self.started = int(time.time())
            self.before = _db_counts(self.db_path)
            self.logger.info("scan started: folders=%s ml_python=%s exclude=%s",
                             folders, ml_python or "(standalone/analyzer)",
                             list(exclude) or "(none)")
            self.thread = threading.Thread(
                target=self._run, args=(list(folders), ml_python, list(exclude)),
                daemon=True)
            self.thread.start()

    def _catalog_args(self):
        """["--catalog-tags"] when settings `read_catalog_tags` is on, else nothing. A
        settings file that cannot be read means off, never a failed scan."""
        try:
            on = bool(self.load_settings and self.load_settings().get("read_catalog_tags"))
        except Exception:
            on = False
        return ["--catalog-tags"] if on else []

    def _exec(self, name, argv):
        """Run one stage, streaming stdout into the tail buffer. Returns rc."""
        self.stage = name
        self.progress = None
        t0 = time.time()
        self.lines.append(f"── {name}: {' '.join(os.path.basename(a) for a in argv[:2])} …")
        self.logger.info("stage start: %s  argv=%s", name, argv)
        try:
            self.proc = subprocess.Popen(
                argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace", cwd=SRC,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        except OSError as e:
            self.lines.append(f"failed to start: {e}")
            self.logger.error("stage %s failed to start: %s", name, e)
            self.stages_done.append({"name": name, "rc": -1, "seconds": 0})
            return -1
        for line in self.proc.stdout:
            line = line.rstrip()
            if not line:
                continue
            self.lines.append(line)
            self.logger.info("[%s] %s", name, line)
            m = _PROG_RE.search(line)
            if m:
                self.progress = (int(m.group(1)), int(m.group(2)))
        rc = self.proc.wait()
        self.proc = None
        seconds = round(time.time() - t0, 1)
        self.stages_done.append({"name": name, "rc": rc, "seconds": seconds})
        self.logger.info("stage done: %s rc=%s seconds=%s", name, rc, seconds)
        return rc

    def _run(self, folders, ml_python, exclude=()):
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
                # Said to the PERSON, who cannot act on a file path or on the words "ML
                # venv"; the path they cannot use goes to the scan log, where it is
                # exactly what a bug report needs. (Cold Fable audit, 2026-09-20.)
                self.logger.error("analyzer exe missing at %s", analyzer)
                self.error = ("Part of Attune is missing, so it cannot read new music. "
                              "Reinstalling Attune puts it back.")
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
                excl_args = []
                for e in (exclude or []):
                    excl_args += ["--exclude", e]
                rc = self._exec(f"import {os.path.basename(d) or d}",
                                _scan_argv(imp_python, "import-folder", d,
                                           "--db", self.db_path, *excl_args,
                                           *self._catalog_args()))
                if rc != 0:
                    if not self.cancelled:
                        self.error = f"import failed (rc={rc}) — see log tail"
                    return
            if self.cancelled:
                return
            rc = self._exec("analyze", _scan_argv(heavy_python, "analyze",
                                                   "--db", self.db_path))
            if rc != 0:
                if not self.cancelled:
                    self.error = f"analyze failed (rc={rc}) — see log tail"
                return
            if self.cancelled:
                return
            rc = self._exec(embed_label, _embed_argv())
            if rc != 0:
                if not self.cancelled:
                    if any(_REFUSED_MARK in line for line in self.lines):
                        # Not a failure: the fingerprint stage declined to mix two kinds of
                        # fingerprint. Say what the person can do about it, in the window's
                        # own words, instead of "embed failed (rc=1)".
                        self.error = ("New songs were read but not fingerprinted, so they are "
                                      "not in mixes yet. This library was made by an earlier "
                                      "version of Attune. Open Preferences, Library, Sound "
                                      "fingerprints to bring it across.")
                    else:
                        self.error = f"embed failed (rc={rc}) — see log tail"
        finally:
            self.after = _db_counts(self.db_path)
            self.finished = int(time.time())
            self.running = False
            self.stage = ""
            new_tracks = max(0, (self.after.get("tracks", 0) or 0)
                             - (self.before.get("tracks", 0) or 0))
            new_analyzed = max(0, (self.after.get("analyzed", 0) or 0)
                               - (self.before.get("analyzed", 0) or 0))
            new_embedded = max(0, (self.after.get("embedded", 0) or 0)
                               - (self.before.get("embedded", 0) or 0))
            if self.cancelled:
                self.logger.info("scan cancelled: new_tracks=%s new_analyzed=%s "
                                 "new_embedded=%s", new_tracks, new_analyzed, new_embedded)
            elif self.error:
                self.logger.error("scan failed: %s", self.error)
            else:
                self.logger.info("scan completed: new_tracks=%s new_analyzed=%s "
                                 "new_embedded=%s", new_tracks, new_analyzed, new_embedded)

    def cancel(self):
        self.logger.info("cancel requested (stage=%s)", self.stage or "(none running)")
        self.cancelled = True
        p = self.proc
        if p and p.poll() is None:
            p.terminate()

    def status(self):
        after = self.after if not self.running else _db_counts(self.db_path)
        return {
            "running": self.running,
            "kind": self.kind,
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
            # See the module docstring: a resumed scan adds no catalog rows and would
            # otherwise look like it did nothing, leaving its newly analyzed tracks
            # unmixable until the next restart.
            "new_analyzed": max(0, (after.get("analyzed", 0) or 0)
                                - (self.before.get("analyzed", 0) or 0)),
            "new_embedded": max(0, (after.get("embedded", 0) or 0)
                                - (self.before.get("embedded", 0) or 0)),
        }


def register(app, ctx):
    """ctx: dict(db_path, load_settings=callable->dict, logger=Optional[logging.Logger]).

    `logger` is the 'attune.scan' logger applog.py configures against
    <config_dir()>/logs/scan.log (AUDIT_FABLE_2026-07-28.md S2 item 9) -- app.py builds
    it once (applog.configure_scan_logger) and hands it in here, the same way it hands
    in every other shared dependency (db_path, load_settings) rather than having this
    module reach for applog on its own."""
    # Resolve to an absolute path NOW, in the app's cwd. The scan stages run as
    # subprocesses with cwd=SRC (attune/src), so a relative --db (e.g. the launcher's
    # "mixer-ng/data/mixer.db") would resolve against SRC and fail to open. abspath
    # here binds it to the app's working dir, where it is already known to resolve.
    load_settings = ctx["load_settings"]
    job = ScanJob(os.path.abspath(ctx["db_path"]), ctx.get("logger"), load_settings)
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
        # settings `exclude_folders`: prefixes import-folder never walks (ruling B2)
        excl = [e for e in (s.get("exclude_folders") or [])
                if isinstance(e, str) and e.strip()]
        try:
            job.start(folders, ml, excl)
        except RuntimeError as e:
            return jsonify(ok=False, error=str(e)), 409
        return jsonify(ok=True)

    @bp.post("/api/scan/cancel")
    def scan_cancel():
        job.cancel()
        return jsonify(ok=True)

    # ---- sound fingerprints: bring a library made by an earlier version across
    weights = ctx.get("fingerprint_weights")

    @bp.get("/api/fingerprints/state")
    def fingerprints_state():
        st = fingerprint_state(job.db_path, weights)
        st["running"] = bool(job.running)
        st["running_kind"] = job.kind if job.running else ""
        return jsonify(ok=True, **st)

    @bp.post("/api/fingerprints/renew")
    def fingerprints_renew():
        st = fingerprint_state(job.db_path, weights)
        if not st["todo"]:
            return jsonify(ok=False, error="Every song in this library already has a "
                           "fingerprint of the current kind. There is nothing to do."), 400
        ml = (load_settings().get("ml_venv_python") or "").strip()
        if ml and not os.path.isfile(ml):
            ml = ""
        try:
            job.start_fingerprints(ml, redo_all=(st["kind"] == "earlier"))
        except RuntimeError:
            return jsonify(ok=False, error="A scan is running. Wait for it to finish, or stop "
                           "it, then press this again."), 409
        return jsonify(ok=True, todo=st["todo"], est_seconds=st["est_seconds"])

    app.register_blueprint(bp)
    return job
