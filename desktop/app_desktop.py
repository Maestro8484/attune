"""Attune desktop — a native window wrapping the web UI (no browser tab).

Run in place:
    python attune/desktop/app_desktop.py
Build a one-click .exe:
    python attune/desktop/build.py         (writes dist/Attune/Attune.exe)

Finding your library: the app looks for the music DB in this order —
  1. the ATTUNE_DB environment variable,
  2. mixer.db (or data/mixer.db) sitting next to the program,
  3. a mixer-ng/data/mixer.db found by walking up from the program.
So the exe "just works" inside the repo, and stays portable if you copy
mixer.db next to it.
"""
import importlib.util
import os
import sys

# app.py loads hybrid.py / export.py by file path (importlib), so PyInstaller's
# static analysis can't see their imports — pull the heavy runtime deps in here
# explicitly so they get bundled.
import json          # noqa: F401
import sqlite3       # noqa: F401
import flask         # noqa: F401
import numpy         # noqa: F401
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
    # walk up for a repo-style mixer-ng/data/mixer.db, from both the program's
    # folder AND this file's folder — so source mode works from any cwd.
    for start in (home, os.path.dirname(os.path.abspath(__file__))):
        hit = _walk_up_for(start, os.path.join("mixer-ng", "data", "mixer.db"))
        if hit:
            return hit
    return None


def _load_create_app(base):
    path = os.path.join(base, "web", "app.py")
    spec = importlib.util.spec_from_file_location("attune_web_app", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.create_app


def _message_html(msg):
    return (
        "<div style='font-family:Segoe UI,system-ui,sans-serif;padding:34px;"
        "color:#1a1a1a;background:#fafafa;height:100%'>"
        "<div style='font-size:24px;font-weight:600'>Attune</div>"
        f"<p style='font-size:15px;line-height:1.6;margin-top:14px'>{msg}</p></div>"
    )


def main():
    db = _find_db()
    if not db:
        webview.create_window(
            "Attune",
            html=_message_html(
                "No music database found.<br><br>Put your <code>mixer.db</code> "
                "next to this program, or set the <code>ATTUNE_DB</code> environment "
                "variable to its full path."),
            width=640, height=380)
        webview.start()
        return
    print(f"Attune desktop — loading library from {db}")
    try:
        create_app = _load_create_app(_base_dir())
        app = create_app(db)
    except SystemExit as e:
        # _check_db raises SystemExit with a friendly, actionable message
        webview.create_window("Attune", html=_message_html(str(e).replace("\n", "<br>")),
                              width=680, height=420)
        webview.start()
        return
    except Exception as e:
        # missing bundled file, corrupt DB, engine failure — show it instead of a
        # silent exit (a --windowed build has no console to print a traceback to)
        webview.create_window("Attune",
                              html=_message_html("Couldn't start the engine.<br><br>"
                                                 + str(e).replace("\n", "<br>")),
                              width=680, height=420)
        webview.start()
        return
    webview.create_window("Attune", app, width=780, height=920, min_size=(430, 560))
    webview.start()


if __name__ == "__main__":
    main()
