r"""Attune structured logging -- first slice (AUDIT_FABLE_2026-07-28.md S2 item 9:
"zero logging-module usage across web/, desktop/, src/ (grep-verified). Instrument
scanjob's already-captured subprocess output into %APPDATA%\Attune\logs\ + a read-only
Diagnostics view. Prove the pattern on one path before spreading.").

Prior art before invention: stdlib `logging` + `logging.handlers.RotatingFileHandler`,
not a hand-rolled writer. One log file per component under <config_dir()>/logs/
(src/config.py's config_dir() -- %APPDATA%\Attune on Windows, $XDG_CONFIG_HOME/attune
elsewhere). Bounded: each file rotates at ROTATE_BYTES, keeping BACKUP_COUNT old
copies, so total on-disk use per component is capped at roughly
ROTATE_BYTES * (BACKUP_COUNT + 1). The directory is created lazily, on the first
actual log write (see _LazyRotatingFileHandler) -- a user who never triggers a scan
never gets an empty logs/ folder.

This proves the pattern on exactly ONE path: scanjob.py's subprocess-output capture,
which was already there (see scanjob.py's own module docstring) -- this module only
routes it into a file, with timestamps and the stage it came from. It changes no scan
behaviour, no scan return values, and not the S13 cancelled-vs-failed guard in
scanjob.py: applog only OBSERVES via logging calls, it never participates in control
flow, and stdlib logging itself swallows handler errors (Handler.handleError) rather
than raising, so a logging failure can never take the scan down with it.

Two read-only diagnostics endpoints. This contract is FIXED for this overnight build --
a parallel stream builds the browser Diagnostics view against this exact shape, so
changing it here would break that other stream:
  GET /api/diag/logs
      -> {"dir": "<abs path>", "files": [{"name","size","mtime"}, ...]}  newest first
      -> {"dir": "<abs path>", "files": []}  if the logs dir doesn't exist yet (NOT an error)
  GET /api/diag/logs/tail?name=<file>&lines=<n>     (lines: default 200, max 2000)
      -> {"name": "...", "lines": ["...", ...], "truncated": true|false}
      -> 400 {"error": "..."} on a bad name, a name outside the logs dir, or lines that
         doesn't parse as an integer

Containment on `name` follows the SAME sanitise-then-verify-on-realpath proof app.py's
folder picker already uses for a user-supplied path component (_fs_resolve_child):
a strict allow-list regex first (which alone rejects any path separator, so a
Windows OR POSIX traversal attempt and any absolute path already fail), then a
realpath containment check that also catches a dots-only name like ".." (legal under
the regex on its own, since '.' is an allowed character) from escaping via the parent
directory.

Both endpoints are loopback-only (same guard app.py's /api/fs/dirs already uses for
this exact class of "exposes something about this machine's filesystem" endpoint):
log lines can contain full library file paths, so a --host 0.0.0.0 LAN server must not
let another machine on the LAN read them.

Read-only. No delete endpoint, no write endpoint, on purpose.
"""
from __future__ import annotations

import logging
import os
import re
from collections import deque
from logging.handlers import RotatingFileHandler

from flask import Blueprint, jsonify, request

LOG_SUBDIR = "logs"
SCAN_LOG_NAME = "scan.log"
ROTATE_BYTES = 2 * 1024 * 1024   # 2 MB per file
BACKUP_COUNT = 5                 # + the live file = 6 files -> ~12 MB cap per component

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

# Guards against attaching a second RotatingFileHandler to the same logger if
# configure_scan_logger() is called more than once in a process (e.g. a test harness
# building create_app() twice) -- logging.Logger objects are process-wide singletons
# keyed by name, so a naive addHandler() on every call would duplicate every future
# line by the number of calls.
_configured_paths = set()


class _LazyRotatingFileHandler(RotatingFileHandler):
    """RotatingFileHandler that also creates its own directory lazily.

    The caller passes delay=True, which already defers opening the log file until the
    first emit; stdlib's _open() then does a plain open(), which raises
    FileNotFoundError if <config_dir>/logs/ doesn't exist yet. This override creates
    that directory at the same lazy moment.
    """

    def _open(self):
        os.makedirs(os.path.dirname(self.baseFilename), exist_ok=True)
        return super()._open()


def logs_dir(config_dir):
    """<config_dir>/logs, absolute. Does not touch the filesystem."""
    return os.path.abspath(os.path.join(config_dir, LOG_SUBDIR))


def configure_scan_logger(config_dir):
    """Return the 'attune.scan' logger, attaching its rotating file handler the first
    time this config_dir is used. Idempotent -- safe to call more than once."""
    logger = logging.getLogger("attune.scan")
    logger.setLevel(logging.INFO)
    logger.propagate = False   # never spam the app's own stdout/stderr
    target = os.path.join(logs_dir(config_dir), SCAN_LOG_NAME)
    if target not in _configured_paths:
        handler = _LazyRotatingFileHandler(
            target, maxBytes=ROTATE_BYTES, backupCount=BACKUP_COUNT,
            encoding="utf-8", delay=True)
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))
        logger.addHandler(handler)
        _configured_paths.add(target)
    return logger


def list_logs(config_dir):
    """[{name, size, mtime}], newest first. [] (not an error) if the dir doesn't exist
    yet -- that's the normal state before any scan has ever run."""
    d = logs_dir(config_dir)
    if not os.path.isdir(d):
        return []
    out = []
    with os.scandir(d) as it:
        for e in it:
            try:
                if e.is_file(follow_symlinks=False):
                    st = e.stat()
                    out.append({"name": e.name, "size": st.st_size, "mtime": st.st_mtime})
            except OSError:
                continue   # unreadable entry -- skip, don't fail the whole listing
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def _resolve_log_file(config_dir, name):
    """`name` proven to resolve to a real file INSIDE the logs dir, or ValueError with
    a user-facing message (-> 400 upstream) on anything else."""
    if not name or not _NAME_RE.match(name):
        raise ValueError("invalid log file name")
    root = os.path.realpath(logs_dir(config_dir))
    target = os.path.realpath(os.path.join(root, name))
    # dirname check (not just commonpath) so a dots-only name like ".." -- legal under
    # the regex, since '.' is an allowed character -- can't resolve to the logs dir's
    # own parent and pass a looser containment test.
    if target == root or os.path.dirname(target) != root:
        raise ValueError("invalid log file name")
    if not os.path.isfile(target):
        raise ValueError("no such log file")
    return target


def tail_log(config_dir, name, lines=200):
    """{name, lines:[...], truncated}. Raises ValueError (-> 400) on a bad name or a
    missing file. `truncated` is True when the file held more lines than requested."""
    path = _resolve_log_file(config_dir, name)
    buf = deque(maxlen=lines)
    total = 0
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            total += 1
            buf.append(line.rstrip("\n"))
    return {"name": name, "lines": list(buf), "truncated": total > len(buf)}


def register(app, ctx):
    """ctx: dict(config_dir=callable -> str). Registers the two read-only /api/diag/logs*
    routes. Called from its own block at the end of app.py's create_app(), separate from
    where configure_scan_logger() is wired into scanjob's ctx (scanjob needs the logger
    from the start; these HTTP routes can be registered wherever is convenient)."""
    config_dir = ctx["config_dir"]
    bp = Blueprint("applog", __name__)

    def _loopback_only():
        return request.remote_addr in ("127.0.0.1", "::1")

    @bp.get("/api/diag/logs")
    def diag_logs():
        if not _loopback_only():
            return jsonify(error="diagnostics are only available on the Attune machine itself"), 403
        d = config_dir()
        return jsonify(dir=logs_dir(d), files=list_logs(d))

    @bp.get("/api/diag/logs/tail")
    def diag_logs_tail():
        if not _loopback_only():
            return jsonify(error="diagnostics are only available on the Attune machine itself"), 403
        name = request.args.get("name") or ""
        try:
            n = int(request.args.get("lines", 200))
        except ValueError:
            return jsonify(error="lines must be an integer"), 400
        n = min(max(n, 1), 2000)
        try:
            result = tail_log(config_dir(), name, n)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(**result)

    app.register_blueprint(bp)
