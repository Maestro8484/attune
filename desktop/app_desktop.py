"""Attune desktop — a native window wrapping Attune Studio (no browser tab).

Run in place:
    python attune/desktop/app_desktop.py
Build a one-click .exe:
    python attune/desktop/build.py         (writes dist/Attune/Attune.exe)

Self-contained by design. The bundled app needs NO external service to run:
  * Engine: it probes for a live MusicIP Mixer on localhost:10002 and uses it if present,
    otherwise it falls back to the built-in V2 engine (CLAP/librosa vectors already stored
    in mixer.db — no torch, no librosa, no network). So it always starts and always mixes.
  * Library DB (mixer.db): found via ATTUNE_DB, then next to the program, then by walking up
    the repo tree.
  * Playlist folder: ATTUNE_PLAYLIST_DIR, or a "Playlists" folder next to the program.

What it CANNOT do standalone (stated honestly, not hidden):
  * bundle or launch MusicIP Mixer itself — it is closed third-party software.
  * analyze brand-new tracks — that needs the heavy torch/librosa/CLAP stack, which this
    build deliberately omits. It plays and mixes an already-analyzed library.
  * play audio whose files aren't present at their stored paths (the music itself is not
    bundled; the app streams it live from disk).
"""
import importlib.util
import os
import sys
import threading
import urllib.request

from werkzeug.serving import make_server

# app.py loads hybrid.py / engine.py / musicip_engine.py / export.py / studio.py by file
# path (importlib), so PyInstaller's static analysis can't see their imports — pull the
# heavy runtime deps in here explicitly so they get bundled.
import json          # noqa: F401
import sqlite3       # noqa: F401
import flask         # noqa: F401
import numpy         # noqa: F401
import mutagen       # noqa: F401
import requests      # noqa: F401  (MusicIP adapter + Plex export)
import webview


def _base_dir():
    """Root of the bundled 'attune' tree (frozen) or the real attune/ dir (source)."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "attune")
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _home_dir():
    """Folder the program lives in (next to the .exe when frozen)."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.getcwd()


def _walk_up_for(start, rel, levels=8):
    d = start
    for _ in range(levels):
        cand = os.path.join(d, rel)
        if os.path.exists(cand):
            return os.path.abspath(cand)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    return None


def _find_db():
    home = _home_dir()
    for c in (os.environ.get("ATTUNE_DB"),
              os.path.join(home, "mixer.db"),
              os.path.join(home, "data", "mixer.db")):
        if c and os.path.exists(c):
            return os.path.abspath(c)
    for start in (home, os.path.dirname(os.path.abspath(__file__))):
        hit = _walk_up_for(start, os.path.join("mixer-ng", "data", "mixer.db"))
        if hit:
            return hit
    return None


def _find_playlists():
    home = _home_dir()
    for c in (os.environ.get("ATTUNE_PLAYLIST_DIR"),
              os.path.join(home, "Playlists")):
        if c and os.path.isdir(c):
            return os.path.abspath(c)
    return None


def _port_open(host, port, timeout=0.25):
    """Cheap 'is anything listening' pre-check.

    MEASURED (2026-07-22, this machine): with MusicIP not running, port 10002 does
    not REFUSE the connection -- the SYN is silently dropped -- so every connect
    burns its full timeout. urllib against "localhost" then does it twice (::1 and
    127.0.0.1), which cost 4.13s on EVERY launch, probing for software that wasn't
    running. A short-timeout socket probe bounds that to <=2*timeout; a MusicIP that
    IS running answers a loopback connect in single-digit ms, so 0.25s is generous.
    """
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _musicip_alive(url="http://localhost:10002", timeout=2.0):
    """True only if a live MusicIP Mixer answers. Fast-fails when nothing listens."""
    from urllib.parse import urlsplit
    u = urlsplit(url if "//" in url else "//" + url, scheme="http")
    host, port = u.hostname or "127.0.0.1", u.port or 10002
    if not _port_open(host, port):
        return False
    try:
        with urllib.request.urlopen(f"http://{host}:{port}/api/version", timeout=timeout) as r:
            return b"MusicIP" in r.read(64)
    except Exception:
        return False


def _load_create_app(base):
    path = os.path.join(base, "web", "app.py")
    spec = importlib.util.spec_from_file_location("attune_web_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.create_app


def _load_config(base):
    """config.py is the one settings store shared with the web app + analyzer."""
    path = os.path.join(base, "src", "config.py")
    spec = importlib.util.spec_from_file_location("attune_config", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SPLASH_HTML = """<!doctype html><meta charset="utf-8"><title>Attune</title>
<style>
 html,body{margin:0;height:100%;background:#0e1014;color:#e7e9ee;
   font-family:'Segoe UI',system-ui,sans-serif;overflow:hidden}
 .w{height:100%;display:flex;flex-direction:column;align-items:center;
   justify-content:center;gap:18px}
 .logo{font-size:34px;font-weight:600;color:#f0a92e;letter-spacing:.5px}
 .bar{width:260px;height:3px;background:#23262e;border-radius:2px;overflow:hidden}
 .bar i{display:block;height:100%;width:40%;background:#f0a92e;border-radius:2px;
   animation:slide 1.1s ease-in-out infinite}
 @keyframes slide{0%{transform:translateX(-110%)}100%{transform:translateX(360%)}}
 .msg{font-size:13px;color:#8b93a3;min-height:18px}
 .err{color:#ff6b6b;max-width:520px;text-align:center;line-height:1.6;font-size:14px}
</style>
<div class="w">
  <div class="logo">&#9670; Attune</div>
  <div class="bar"><i></i></div>
  <div class="msg" id="m">Starting&hellip;</div>
</div>
<script>
(async function(){
  const m = document.getElementById('m');
  for(;;){
    try{
      const r = await fetch('/api/boot', {cache:'no-store'});
      const j = await r.json();
      if(j.error){
        document.querySelector('.bar').style.display='none';
        m.className='err'; m.textContent = j.error; return;
      }
      m.textContent = j.phase || 'Loading\\u2026';
      if(j.ready){ location.replace('/studio'); return; }
    }catch(e){ /* server not up yet */ }
    await new Promise(r=>setTimeout(r,150));
  }
})();
</script>
"""


class _BootGate:
    """WSGI gate so the WINDOW can open before the library is loaded.

    Prior art, not invention: this is werkzeug's DispatcherMiddleware shape (a WSGI
    callable that delegates to another app) plus a readiness endpoint -- the same
    pattern the web launcher already uses when it polls /api/lib/stats before opening
    a browser. Until the real Flask app exists, every request gets the splash and
    /api/boot reports progress; afterwards every request is delegated unchanged, so
    the app's own routes are untouched.

    Why it matters: the old code called create_app() BEFORE create_window(), so the
    user saw nothing at all until the whole library was in memory -- the single
    biggest reason Attune did not feel like a normal desktop app.
    """

    def __init__(self):
        self.real = None
        self.phase = "Starting…"
        self.error = None

    def _text(self, start_response, body, ctype, status="200 OK"):
        data = body.encode("utf-8")
        start_response(status, [("Content-Type", ctype),
                                ("Content-Length", str(len(data))),
                                ("Cache-Control", "no-store")])
        return [data]

    def __call__(self, environ, start_response):
        path = environ.get("PATH_INFO", "")
        if path == "/api/boot":
            payload = {"ready": self.real is not None,
                       "phase": self.phase, "error": self.error}
            return self._text(start_response, json.dumps(payload), "application/json")
        if self.real is not None:
            return self.real(environ, start_response)
        return self._text(start_response, _SPLASH_HTML, "text/html; charset=utf-8")


def _message_html(msg):
    return (
        "<div style='font-family:Segoe UI,system-ui,sans-serif;padding:34px;"
        "color:#e7e9ee;background:#0e1014;height:100%'>"
        "<div style='font-size:24px;font-weight:600;color:#6d8cff'>◆ Attune</div>"
        f"<p style='font-size:15px;line-height:1.6;margin-top:14px'>{msg}</p></div>"
    )


def _fail(msg, w=680, h=420):
    webview.create_window("Attune", html=_message_html(msg.replace("\n", "<br>")),
                          width=w, height=h)
    webview.start()


def _run_worker():
    """Re-enter this frozen (or dev) exe as a headless analyzer worker.

    Invoked as: Attune.exe --attune-worker <tool> <tool argv...>
    tool is one of "scan" / "embed_onnx" — the module name under attune/src whose
    main() we call. This lets scanjob.py drive the heavy analyze/embed stages by
    re-launching sys.executable (the packaged exe itself) instead of a system
    python, which a frozen build does not have.
    """
    # Make the bundled ffmpeg/ffprobe (attune/bin) win over any system install for this
    # worker process: scan.py shells out to `ffprobe` for tags and librosa/audioread fall
    # back to `ffmpeg` to decode formats libsndfile can't (m4a/aac/wma). Prepending here
    # (worker only — the GUI never decodes) means a first-run scan needs NO system ffmpeg.
    bin_dir = (os.path.join(sys._MEIPASS, "attune", "bin") if getattr(sys, "frozen", False)
               else os.path.join(os.path.dirname(os.path.abspath(__file__)), "ffbin"))
    if os.path.isdir(bin_dir):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

    src_dir = (os.path.join(sys._MEIPASS, "attune", "src") if getattr(sys, "frozen", False)
               else os.path.join(_base_dir(), "src"))
    sys.path.insert(0, src_dir)
    tool = sys.argv[2]
    rest = sys.argv[3:]
    sys.argv = [tool] + rest
    try:
        if tool == "scan":
            import scan
            scan.main()
        elif tool == "embed_onnx":
            import embed_onnx
            embed_onnx.main()
        else:
            print(f"[attune-worker] unknown tool: {tool}", file=sys.stderr)
            sys.exit(1)
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--attune-worker":
        _run_worker()
        return

    cfg = _load_config(_base_dir())
    settings = cfg.load()

    # DB: settings.json first (the one source of truth), then ATTUNE_DB, then the legacy
    # walk-up discovery. Whatever we discover, write it back so the next launch is instant.
    db = cfg.effective(None, "ATTUNE_DB", "db_path", settings) or _find_db()
    if not db:
        _fail("No music database found.<br><br>Open Preferences and set your library "
              "database, put <code>mixer.db</code> next to this program, or set the "
              "<code>ATTUNE_DB</code> environment variable to its full path.")
        return
    if db and settings.get("db_path") != db:
        try:
            cfg.update({"db_path": db})
        except OSError:
            pass

    want_engine = cfg.effective(None, "ATTUNE_ENGINE", "engine", settings) or "auto"
    playlists = cfg.effective(None, "ATTUNE_PLAYLIST_DIR", "playlist_dir", settings) \
        or _find_playlists()
    mip_url = cfg.effective(None, "ATTUNE_MUSICIP_URL", "musicip_url", settings) \
        or "http://localhost:10002"
    print(f"Attune desktop — db={db}")
    print(f"  playlists={playlists or '(none configured)'}")

    # ---- window first, library second -------------------------------------------------
    # Serve a boot gate immediately on a free loopback port, open the window against it,
    # and do ALL slow work (MusicIP probe, engine load) on a worker thread. The user sees
    # Attune within a second instead of staring at nothing until the library is in memory.
    gate = _BootGate()
    srv = make_server("127.0.0.1", 0, gate, threaded=True)
    port = srv.server_port
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def _load():
        try:
            # Engine: 'auto' (default) probes MusicIP and falls back to V2; an explicit
            # 'musicip'/'v2' is honoured as-is. Deliberately NOT on the window's critical
            # path -- see _port_open: a dropped-SYN probe used to cost 4.13s per launch.
            if want_engine == "auto":
                gate.phase = "Looking for MusicIP…"
                engine = "musicip" if _musicip_alive(mip_url) else "v2"
                print(f"  engine=auto -> {engine}")
            else:
                engine = want_engine
            gate.phase = "Loading your library…"
            create_app = _load_create_app(_base_dir())
            try:
                app = create_app(db, engine_name=engine, musicip_url=mip_url,
                                 playlist_dir=playlists)
            except SystemExit as e:
                # If MusicIP vanished between the probe and load, or the DB is unusable,
                # try one clean fall back to V2 before giving up.
                if engine == "musicip":
                    app = create_app(db, engine_name="v2", playlist_dir=playlists)
                else:
                    raise
            gate.real = app                     # publish last: readiness flips atomically
            gate.phase = "Ready"
        except BaseException as e:              # SystemExit included — report, never hang
            gate.error = f"Couldn't start the engine: {e}"

    webview.create_window("Attune Studio", f"http://127.0.0.1:{port}/",
                          width=1360, height=880, min_size=(900, 620))
    # webview.start(func) runs func on a worker thread only AFTER the GUI is up --
    # pywebview's own documented idiom. Starting the loader any earlier makes it
    # contend for the GIL with window creation and delays the very thing we want
    # on screen first (measured: ~4s later when loaded before create_window).
    webview.start(_load)


if __name__ == "__main__":
    import multiprocessing
    multiprocessing.freeze_support()
    main()
