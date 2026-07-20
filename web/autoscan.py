"""Attune auto-scan — "it analyzes new music by itself" wired onto the EXISTING
incremental scan pipeline (scanjob.ScanJob). This module contributes NO new pipeline
logic; it only decides WHEN to call job.start(folders, ml), same as the Rescan button.

Hybrid design, two triggers feeding one job:
  - Launch scan (scan_on_launch): catches everything that changed while Attune was
    NOT running — the common case for a library on a NAS that other machines write to.
    Fires once, ~10s after boot (cosmetic: lets the rest of app startup settle; the
    scan itself runs in subprocesses, so this ordering costs nothing real).
  - Live watcher (watch_folders): catches drops that happen WHILE Attune is running,
    so a dragged-in album shows up without the user remembering to click Rescan.
  Neither trigger replaces the Rescan button or /api/scan/start; both still work and
  still race-guarded via scanjob._START_LOCK.

Per-root observer choice (native vs polling): watchdog's own README calls out that
ReadDirectoryChangesW (the native Windows backend) is unreliable over SMB/CIFS network
shares — events get dropped or silently stop arriving — and recommends PollingObserver
for network filesystems instead. Attune's library_folders are frequently NAS/UNC paths
(see CLAUDE.md machine list), so each watched root is classified independently: local
drive -> native Observer (ReadDirectoryChangesW, near-instant), remote drive/UNC ->
PollingObserver (stat-based, works everywhere, costs one directory walk per timeout).

Why a supervisor thread instead of "start once and trust it": PollingObserver has a
documented failure mode where its emitter thread dies silently on an SMB reconnect
(watchdog gh-452, gh-648) — observer.is_alive() goes False with no exception anywhere
to catch. Nothing else notices a stopped watcher, so a 5s-tick health check recreates
any observer found dead. Native Windows observers can also miss events across a share
that briefly drops, so a periodic settings reconciliation (every 30s) plus the launch
scan act as a safety net that eventually re-syncs even if a watcher wedges silently
between health checks.

Why debounce (20s quiet period) instead of "is the file finished copying": there is no
reliable cross-platform signal for "this file is done being written" — no lock byte,
no rename-on-complete guarantee, and album folders often arrive as many files over
several seconds (drag-drop, rsync, torrent client). Rather than inventing file-readiness
heuristics, this module leans on the fact the underlying pipeline is verified safe to
re-run: import upserts by path+mtime, analyze/embed skip already-current rows. So the
debounce only needs to be "probably settled," not certain — a partially-copied file
just gets picked up cleanly on the NEXT trigger (next drop, next launch, or the next
watch cycle after reconciliation) rather than corrupting anything now.

Why only changed dirs are passed to job.start, not the whole library: import_folder
(src/scan.py) walks its target directory and ffprobes every file under it on every
call. Handing it the full library_folders list on every single-track drop would mean
minutes of ffprobe re-reads for one new file. The watcher instead accumulates the
actual touched directories (event's file's parent, or the created dir itself for a
new album folder) and only imports those; analyze/embed remain DB-wide scans but are
already incremental (mtime/existing-row skip), so full-library correctness is kept
without full-library import cost.

If `watchdog` is not importable, the module still registers /api/watch/status
(reporting watchdog_available: false) and still honors scan_on_launch — only the live
watcher is disabled. Analysis without watchdog installed is therefore "self-analyzing
on launch" but not "self-analyzing live", which is an honest degraded mode, not a
silent no-op.
"""
from __future__ import annotations

import ctypes
import os
import sys
import threading
import time

from flask import Blueprint, jsonify

try:
    import watchdog                      # noqa: F401  (presence check only)
    from watchdog.events import FileSystemEventHandler
    from watchdog.observers import Observer
    from watchdog.observers.polling import PollingObserver
    WATCHDOG_AVAILABLE = True
except ImportError:
    WATCHDOG_AVAILABLE = False
    FileSystemEventHandler = object       # placeholder base so the class below still defines

# Mirrors src/scan.py's AUDIO_EXTS. Kept as a local copy (not imported) because this
# module must import cleanly even in the standalone-lean venv that never imports src/
# at module load time elsewhere in web/ either (scanjob.py runs scan.py as a
# subprocess rather than importing it).
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".wav"}

LAUNCH_SCAN_DELAY = 10.0    # seconds: let boot settle before the launch scan fires
TICK_SECS = 5                # supervisor wake interval
RECONCILE_SECS = 30           # how often settings are re-read and observers reconciled
QUIET_SECS = 20               # debounce window: fire only after this much event silence


def _is_remote_drive(folder):
    """True if `folder` lives on a network filesystem (UNC path or a mapped/attached
    drive Windows reports as DRIVE_REMOTE). Non-Windows: never remote here — the
    native Observer is used everywhere off Windows (this module targets the Windows
    ReadDirectoryChangesW-vs-SMB problem specifically)."""
    if os.name != "nt":
        return False
    if folder.startswith("\\\\") or folder.startswith("//"):
        return True
    drive = os.path.splitdrive(os.path.abspath(folder))[0]
    if not drive:
        return False
    try:
        return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == 4  # DRIVE_REMOTE
    except Exception:
        return False


class _Handler(FileSystemEventHandler):
    """Cheap, never-raising event recorder. Does no I/O beyond a set/dict update under
    a lock — the actual scan work happens later, out of band, in the supervisor tick."""

    def __init__(self, state):
        self._state = state

    def _record(self, path, is_dir_creation=False):
        try:
            target = path if is_dir_creation else os.path.dirname(path)
            if not target:
                return
            with self._state.lock:
                self._state.pending.add(target)
                self._state.last_event = time.time()
        except Exception:
            pass   # handlers must never raise — a bad path is not worth crashing the watcher

    def _maybe_file(self, path):
        if os.path.splitext(path)[1].lower() in AUDIO_EXTS:
            self._record(path)

    def on_created(self, event):
        try:
            if event.is_directory:
                self._record(event.src_path, is_dir_creation=True)
            else:
                self._maybe_file(event.src_path)
        except Exception:
            pass

    def on_modified(self, event):
        try:
            if not event.is_directory:
                self._maybe_file(event.src_path)
        except Exception:
            pass

    def on_moved(self, event):
        try:
            dest = getattr(event, "dest_path", None) or event.src_path
            if event.is_directory:
                self._record(dest, is_dir_creation=True)
            else:
                self._maybe_file(dest)
        except Exception:
            pass


class _State:
    """Shared mutable state between the supervisor thread and /api/watch/status.
    Plain-attribute reads outside the lock are fine (GIL-safe), matching scanjob.py's
    own documented style; only the pending-set mutations go through the lock."""

    def __init__(self):
        self.lock = threading.Lock()
        self.pending = set()          # dirs touched since the last successful fire
        self.last_event = 0.0
        self.last_trigger = 0.0
        self.last_trigger_dirs = []
        self.restarts = 0             # count of dead-observer recreations
        self.last_error = ""
        self.observers = {}           # folder -> {"observer": obj, "mode": "native"|"polling"}
        self.watch_folders = False
        self.watchdog_available = WATCHDOG_AVAILABLE


def _make_observer(folder):
    """One Observer instance per folder (native or polling depending on the folder's
    own filesystem, not a single global choice) — keeps per-root recreation on health-
    check failure simple: discard and rebuild just that one, not everyone's watch."""
    remote = _is_remote_drive(folder)
    observer = PollingObserver(timeout=60) if remote else Observer()
    return observer, ("polling" if remote else "native")


def _start_watch(state, folder, handler):
    try:
        observer, mode = _make_observer(folder)
        observer.schedule(handler, folder, recursive=True)
        observer.start()              # watchdog observers are daemon threads by default
        state.observers[folder] = {"observer": observer, "mode": mode}
    except Exception as e:
        state.last_error = f"failed to watch {folder}: {e}"


def _stop_watch(state, folder):
    entry = state.observers.pop(folder, None)
    if not entry:
        return
    try:
        entry["observer"].stop()
    except Exception:
        pass


def _reconcile(state, handler, library_folders):
    wanted = {f for f in library_folders if f and os.path.isdir(f)}
    current = set(state.observers)
    for folder in current - wanted:
        _stop_watch(state, folder)
    for folder in wanted - current:
        _start_watch(state, folder, handler)


def _health_check(state, handler):
    for folder, entry in list(state.observers.items()):
        observer = entry["observer"]
        if not observer.is_alive():
            state.restarts += 1
            _stop_watch(state, folder)
            _start_watch(state, folder, handler)


def _under_any(path, roots):
    """Prefix check, case-insensitive on Windows (os.path.normcase folds case there and
    is a no-op elsewhere). Used to drop pending dirs that belong to a folder that was
    just removed from library_folders — those must not be scanned."""
    npath = os.path.normcase(os.path.abspath(path))
    for root in roots:
        nroot = os.path.normcase(os.path.abspath(root))
        if npath == nroot or npath.startswith(nroot + os.sep):
            return True
    return False


def _resolve_ml(state, settings):
    """Same rule scanjob.scan_start applies to a manual scan: an empty ml_venv_python
    is fine (routes to the standalone ONNX path); a NON-empty one that doesn't exist on
    disk is a misconfiguration, and firing a scan against it would just fail loudly in
    a subprocess. Caught here instead so the failure shows up as last_error, not a
    silently wasted trigger."""
    ml = (settings.get("ml_venv_python") or "").strip()
    if ml and not os.path.isfile(ml):
        state.last_error = f"ml_venv_python not found: {ml}"
        return None, False
    return ml, True


def _supervisor(state, job, load_settings, handler_factory):
    handler = handler_factory(state)
    last_reconcile = 0.0
    while True:
        time.sleep(TICK_SECS)
        now = time.time()

        if now - last_reconcile >= RECONCILE_SECS:
            last_reconcile = now
            try:
                settings = load_settings()
            except Exception as e:
                state.last_error = f"load_settings failed: {e}"
                settings = None
            if settings is not None:
                folders = [f for f in (settings.get("library_folders") or [])
                           if isinstance(f, str) and f.strip()]
                enabled = bool(settings.get("watch_folders")) and bool(folders)
                state.watch_folders = enabled
                if enabled:
                    _reconcile(state, handler, folders)
                elif state.observers:
                    for folder in list(state.observers):
                        _stop_watch(state, folder)

        if state.watchdog_available and state.observers:
            _health_check(state, handler)

        with state.lock:
            has_pending = bool(state.pending)
            quiet_ok = has_pending and (now - state.last_event) >= QUIET_SECS
            if quiet_ok:
                dirs = sorted(state.pending)
                state.pending.clear()
            else:
                dirs = None

        if dirs is None:
            continue

        try:
            settings = load_settings()
        except Exception as e:
            state.last_error = f"load_settings failed: {e}"
            with state.lock:
                state.pending.update(dirs)   # retry later
            continue

        roots = [f for f in (settings.get("library_folders") or [])
                 if isinstance(f, str) and f.strip()]
        dirs = [d for d in dirs if os.path.isdir(d) and _under_any(d, roots)]
        if not dirs:
            continue   # everything filtered out (e.g. folder removed mid-debounce)

        ml, ok = _resolve_ml(state, settings)
        if not ok:
            continue   # last_error already set; drop this batch rather than crash-loop it

        try:
            job.start(dirs, ml)
        except RuntimeError:
            # a scan (manual or the launch scan) is already running — put the dirs back
            # and let a later tick retry once it finishes.
            with state.lock:
                state.pending.update(dirs)
        else:
            state.last_trigger = time.time()
            state.last_trigger_dirs = dirs


def _launch_scan(job, load_settings, state):
    try:
        settings = load_settings()
    except Exception as e:
        state.last_error = f"load_settings failed: {e}"
        return
    folders = [f for f in (settings.get("library_folders") or [])
               if isinstance(f, str) and f.strip()]
    if not settings.get("scan_on_launch") or not folders:
        return
    ml, ok = _resolve_ml(state, settings)
    if not ok:
        return
    try:
        job.start(folders, ml)
    except RuntimeError:
        pass   # a user-started scan already won the race; nothing to do


def register(app, ctx):
    """ctx: dict(job=<ScanJob>, load_settings=callable->dict).

    Registers GET /api/watch/status and starts one daemon supervisor thread. Safe to
    call even when watchdog is not installed: the endpoint still exists and reports
    watchdog_available: false; scan_on_launch still fires via the 10s timer.
    """
    job = ctx["job"]
    load_settings = ctx["load_settings"]
    state = _State()

    bp = Blueprint("autoscan", __name__)

    @bp.get("/api/watch/status")
    def watch_status():
        # list() snapshot: the supervisor thread mutates this dict; iterating it live
        # can raise "dictionary changed size during iteration".
        watching = [{"folder": f, "mode": e["mode"], "alive": e["observer"].is_alive()}
                    for f, e in list(state.observers.items())]
        with state.lock:
            pending = sorted(state.pending)
        return jsonify(
            enabled=state.watch_folders,
            watchdog_available=state.watchdog_available,
            watching=watching,
            pending=pending,
            last_event=state.last_event,
            last_trigger=state.last_trigger,
            last_trigger_dirs=state.last_trigger_dirs,
            restarts=state.restarts,
            last_error=state.last_error,
        )

    app.register_blueprint(bp)

    # Launch scan: fires once regardless of watchdog availability (it does not need
    # the watcher, only scan_on_launch). Reads settings once, up front — deliberately
    # not re-read at fire time, since the delay is cosmetic ordering, not a real wait.
    try:
        settings = load_settings()
    except Exception as e:
        state.last_error = f"load_settings failed: {e}"
        settings = {}
    if settings.get("scan_on_launch") and settings.get("library_folders"):
        timer = threading.Timer(LAUNCH_SCAN_DELAY, _launch_scan, args=(job, load_settings, state))
        timer.daemon = True
        timer.start()

    if not WATCHDOG_AVAILABLE:
        return

    def handler_factory(st):
        return _Handler(st)

    t = threading.Thread(target=_supervisor, args=(state, job, load_settings, handler_factory),
                         daemon=True)
    t.start()
