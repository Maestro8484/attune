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
import urllib.request

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


def _musicip_alive(url="http://localhost:10002/api/version", timeout=2.0):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return b"MusicIP" in r.read(64)
    except Exception:
        return False


def _load_create_app(base):
    path = os.path.join(base, "web", "app.py")
    spec = importlib.util.spec_from_file_location("attune_web_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.create_app


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


def main():
    db = _find_db()
    if not db:
        _fail("No music database found.<br><br>Put your <code>mixer.db</code> next to this "
              "program, or set the <code>ATTUNE_DB</code> environment variable to its full path.")
        return

    # Pick the engine that is actually available, don't assume one and crash.
    engine = "musicip" if _musicip_alive() else "v2"
    playlists = _find_playlists()
    print(f"Attune desktop — db={db}")
    print(f"  engine={engine}  (MusicIP {'detected' if engine == 'musicip' else 'not running -> built-in V2'})")
    print(f"  playlists={playlists or '(none configured)'}")

    try:
        create_app = _load_create_app(_base_dir())
        app = create_app(db, engine_name=engine, playlist_dir=playlists)
    except SystemExit as e:
        # If MusicIP vanished between the probe and load, or the DB is unusable, try one
        # clean fall back to V2 before giving up.
        if engine == "musicip":
            try:
                app = create_app(db, engine_name="v2", playlist_dir=playlists)
            except Exception as e2:
                _fail("Couldn't start the engine.<br><br>" + str(e2))
                return
        else:
            _fail(str(e))
            return
    except Exception as e:
        _fail("Couldn't start the engine.<br><br>" + str(e))
        return

    webview.create_window("Attune Studio", app, width=1360, height=880, min_size=(900, 620))
    webview.start()


if __name__ == "__main__":
    main()
