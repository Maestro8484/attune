r"""Attune hot pool reload -- the wart every scan session has carried, removed.

scanjob.py's own docstring says it plainly: "the running engine loads its pool ONCE
at startup, so tracks this job adds only become mixable after an app restart." This
module removes that restart: POST /api/lib/reload rebuilds the engine from the DB in
a background thread and installs it into the already-running app.

DESIGN -- why in-place reinitialization, not rebinding a new object:
    eng / active / labels / labels_lc (app.py) and lib (studio.py) / ud (userdata.py)
    are captured by closures across FIVE separate blueprint modules (app.py itself,
    studio.py, userdata.py, smartlists.py, recipes.py, exportjob.py), each of which
    read them as free variables. Flask has already wired those blueprints by the time
    a reload can run, so there is no way to make five modules' closures all point at
    a NEW object. Instead, reload reinitializes the SAME objects in place -- exactly
    the pattern scanjob.ScanJob.start() already uses on itself ("self.__init__(...)
    # reset state"), just applied to a handful of shared objects instead of one.

    The EXPENSIVE part -- HybridEngine.__init__ reading the whole DB and rebuilding
    the CLAP matrix, observed ~1 s on the production library since the S9 startup fix
    (it was ~25 s before that; either way the slow part) -- runs against throwaway
    STANDALONE instances of the same classes, holding no lock, so in-flight requests
    against the OLD pool are completely unaffected while it runs. Only the FAST
    "install" step -- copying each throwaway instance's __dict__ onto the live
    object of the same identity -- runs under `engine_lock`, held for a fraction of
    a second.

    Every route that reads eng/active/labels/labels_lc/lib/ud is wrapped with the
    `locked` decorator each module receives via ctx (built once, here, from the SAME
    engine_lock) so a request runs entirely against the pre-reload state or entirely
    against the post-reload state, never a torn mix of the two. A request already
    holding the lock when reload wants to install simply makes reload's install step
    wait for it -- "in-flight requests finish on the old object" falls out of that
    for free, no special-casing needed.

Endpoints:
  POST /api/lib/reload           loopback-guarded, starts a reload if none is running
  GET  /api/lib/reload/status     {running, started, done, error, old_count, new_count}
"""
from __future__ import annotations

import functools
import threading
import time

from flask import Blueprint, jsonify, request

# Module-level, same reasoning as scanjob.py's own _START_LOCK: start() resets
# instance state via self.__init__, so an instance-level lock would wipe itself out
# along with everything else.
_START_LOCK = threading.Lock()


def _label(eng, path):
    """Same "Artist - Title" (falling back to basename) label app.py/engine.py use."""
    import os
    m = eng.meta.get(path, {})
    return f"{m.get('artist') or '?'} - {m.get('title') or os.path.basename(path)}"


def build_engine_bundle(hybrid_mod, eng_iface_mod, db_path, engine_name, musicip_url,
                        log=print, mip=None):
    """(hybrid_eng, labels, labels_lc, active) for a FRESH load of db_path -- the exact
    construction app.py's create_app() performs at startup, factored out so both
    startup and reload build the same shape from one place (prior art before
    invention: this is create_app's own engine-construction block, unchanged, just
    named and reusable).

    `mip`, if given, is an ALREADY-LIVE musicip_engine.MusicIPEngine connection to
    reuse -- reload must not tear down and reconnect a working MusicIP session just
    to refresh the pool.
    """
    hybrid_eng = hybrid_mod.HybridEngine(db_path)
    labels = [_label(hybrid_eng, p) for p in hybrid_eng.paths]
    labels_lc = [s.lower() for s in labels]
    if engine_name == "musicip":
        active = eng_iface_mod.MusicIPAdapter(hybrid_eng.paths, url=musicip_url, mip=mip, log=log)
        active.attach_meta(hybrid_eng.meta)
    elif engine_name == "learned":
        active = eng_iface_mod.LearnedEngine(db_path, hybrid_engine=hybrid_eng, log=log)
    else:
        active = eng_iface_mod.V2Engine(hybrid_engine=hybrid_eng)
    return hybrid_eng, labels, labels_lc, active


class ReloadJob:
    """At most one reload runs at a time; status is read lock-free (plain-attribute
    reads, GIL-safe, matching scanjob.py's own documented style)."""

    def __init__(self, ctx):
        self.ctx = ctx
        self.thread = None
        self.running = False
        self.started = 0
        self.finished = 0
        self.error = ""
        self.old_count = 0
        self.new_count = 0

    def start(self):
        with _START_LOCK:
            if self.running:
                raise RuntimeError("a reload is already running")
            ctx = self.ctx
            self.__init__(ctx)          # reset state (scanjob.py's own pattern)
            self.running = True
            self.started = int(time.time())
            self.old_count = len(ctx["eng"].paths)
            self.thread = threading.Thread(target=self._run, daemon=True)
            self.thread.start()

    def _run(self):
        ctx = self.ctx
        try:
            mip = None
            if ctx["engine_name"] == "musicip" and hasattr(ctx["active"], "mip"):
                mip = ctx["active"].mip
            hybrid_eng, labels, labels_lc, active = build_engine_bundle(
                ctx["hybrid_mod"], ctx["eng_iface_mod"], ctx["db_path"],
                ctx["engine_name"], ctx["musicip_url"], log=print, mip=mip)
            # LibraryIndex construction (the DB read for seconds/analyzed/etc) is cheap
            # relative to the engine load above, but still done off-lock -- no reason
            # to make readers wait for it when the eng build already dwarfs it.
            tmp_lib = ctx["studio_mod"].LibraryIndex(ctx["db_path"], hybrid_eng.paths, hybrid_eng.meta)

            with ctx["engine_lock"]:
                eng = ctx["eng"]
                eng.__dict__ = hybrid_eng.__dict__
                ctx["labels"][:] = labels
                ctx["labels_lc"][:] = labels_lc
                ctx["active"].__dict__ = active.__dict__
                lib = ctx["lib"]
                lib.__dict__ = tmp_lib.__dict__
                # usermeta.py's own __init__ re-attaches lib.rating/loved/plays/etc
                # (freshly sized by the lib swap above) and rebuilds ud.paths from the
                # now-live eng -- same trick scanjob.ScanJob.start() plays on itself.
                ud = ctx["ud"]
                ud.__init__(ctx["db_path"], eng, lib, ud.on_tags_changed)
                # filestate.py's FileState re-attaches lib.missing/checked_at the same way.
                filestate = ctx.get("filestate")
                if filestate is not None:
                    filestate.reload()
                self.new_count = len(eng.paths)
        except (Exception, SystemExit) as e:
            self.error = str(e)
        finally:
            self.finished = int(time.time())
            self.running = False

    def status(self):
        return {
            "running": self.running,
            "started": self.started,
            "finished": self.finished,
            "done": bool(self.finished) and not self.running,
            "error": self.error,
            "old_count": self.old_count,
            "new_count": self.new_count,
        }


def register(app, ctx):
    """ctx: dict(db_path, engine_name, musicip_url, hybrid_mod, eng_iface_mod,
    studio_mod, eng, active, labels, labels_lc, lib, ud, engine_lock, filestate?).
    Returns the ReloadJob instance."""
    job = ReloadJob(ctx)
    bp = Blueprint("libreload", __name__)

    def _guard():
        # Rebuilding the live engine is a this-machine action, same rule as
        # /api/fs/dirs and exportjob.py's copy endpoints.
        return request.remote_addr in ("127.0.0.1", "::1")

    @bp.post("/api/lib/reload")
    def reload_start():
        if not _guard():
            return jsonify(ok=False, error="only available on the Attune machine itself"), 403
        try:
            job.start()
        except RuntimeError as e:
            return jsonify(ok=False, error=str(e)), 409
        return jsonify(ok=True)

    @bp.get("/api/lib/reload/status")
    def reload_status():
        return jsonify(job.status())

    app.register_blueprint(bp)
    return job
