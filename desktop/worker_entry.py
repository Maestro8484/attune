r"""The headless analyzer worker body, shared by both frozen entry points.

Two programs need to run the analyze/embed tools in-process:

  * `app_desktop.py --attune-worker <tool> ...`  — the original self-reinvocation
    contract (still the path a DEV run uses, and kept so the contract does not
    change out from under anything that calls it).
  * `analyzer_main.py <tool> ...`                — AttuneAnalyzer.exe, the split-out
    analyzer that ships in an `analyzer\` subfolder beside the lean GUI exe.

The logic is identical in both (bundled-ffmpeg PATH, bundled-src sys.path, dispatch
to the tool's main()), and it resolves everything from `sys._MEIPASS`, which is
per-process — so the SAME function is correct inside either bundle. It lives here
once rather than being copy-pasted into the second entry point.
"""
import os
import sys

# scan.py / db.py / features.py / embed_onnx.py ship as DATA files and are imported by
# bare name at run time, so PyInstaller never parses them and never sees what they need.
# Anything they import must be named here or it is simply absent from the bundle — that
# is how the first analyzer build shipped without sqlite3 and could not open a database.
# (Same pattern, same reason, as the explicit import block in app_desktop.py.)
import argparse       # noqa: F401  scan.py, embed_onnx.py
import concurrent.futures  # noqa: F401  scan.py
import json           # noqa: F401  scan.py, embed_onnx.py
import queue          # noqa: F401  embed_onnx.py
import sqlite3        # noqa: F401  db.py, embed_onnx.py
import subprocess     # noqa: F401  scan.py (ffprobe)
import threading      # noqa: F401  embed_onnx.py
import numpy          # noqa: F401  db.py, features.py, embed_onnx.py
import mutagen        # noqa: F401  tag reads

TOOLS = ("scan", "embed_onnx")


def bundle_dir(fallback_root):
    """Root of the bundled `attune` tree (frozen), else the real attune/ dir."""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, "attune")
    return fallback_root


def user_bin():
    r"""%APPDATA%\Attune\bin if it exists, else None.

    The release build ships no ffmpeg (282 MB for the pair, operator ruling B5), so
    m4a, aac and wma cannot be decoded out of the box. This folder is the answer a
    person can act on: drop ffmpeg.exe in, rescan, and those tracks analyze. It is
    looked at, never created, and its contents are never inspected."""
    base = os.environ.get("APPDATA")
    if not base:
        return None
    d = os.path.join(base, "Attune", "bin")
    return d if os.path.isdir(d) else None


def is_analyzer_bundle(base):
    r"""True when `base` is the ANALYZER bundle rather than the lean GUI bundle.

    Until 2026-09-20 this was `isdir(base/bin)`, which was only ever true because the
    analyzer bundled ffmpeg into `attune\bin`. With ffmpeg gone that test says "GUI" for
    every analyzer build, and the ImportError message below would tell someone running
    AttuneAnalyzer.exe to go run AttuneAnalyzer.exe. The test now asks what actually
    distinguishes the two bundles: embed_onnx.py is in the analyzer's data list and in
    no other (desktop/build.py ANALYZER_DATA vs GUI_DATA)."""
    return os.path.exists(os.path.join(base, "src", "embed_onnx.py"))


def run(tool, rest, fallback_root, bin_fallback, analyzer=None):
    """Run one analyzer tool in THIS process and exit. Never returns.

    tool          "scan" | "embed_onnx" (module under attune/src whose main() we call)
    rest          argv for that tool
    fallback_root the attune/ dir to use when not frozen
    bin_fallback  the ffmpeg/ffprobe dir to use when not frozen
    analyzer      True/False to state outright which bundle this is; None to work it
                  out. Keyword with a default because app_desktop.py, owned by another
                  session, calls this with four arguments and must keep working.
    """
    # PATH for the decoder, most specific first: the user's own folder, then whatever
    # this bundle happens to carry, then the system. librosa/audioread shell out to
    # `ffmpeg` for what libsndfile cannot read (m4a/aac/wma); tags no longer need
    # ffprobe at all since src/scan.py reads them with mutagen.
    #
    # The release build carries no bin\ folder, so on most machines only the user's
    # folder and the system are in play — which is the whole point of the 282 MB the
    # bundle no longer ships.
    # scanjob.py reads this process's stdout as UTF-8; Python would otherwise encode it
    # in the console codepage (cp1252 here) and mangle every track name with an accent
    # in the progress tail. Say it explicitly rather than relying on the environment.
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

    base = bundle_dir(fallback_root)
    bin_dir = os.path.join(base, "bin") if getattr(sys, "frozen", False) else bin_fallback
    for d in (bin_dir, user_bin()):
        if d and os.path.isdir(d):
            cur = os.environ.get("PATH", "")
            if d.lower() not in [x.lower() for x in cur.split(os.pathsep)]:
                os.environ["PATH"] = d + os.pathsep + cur

    sys.path.insert(0, os.path.join(base, "src"))
    sys.argv = [tool] + list(rest)
    try:
        if tool == "scan":
            import scan
            scan.main()
        elif tool == "embed_onnx":
            import embed_onnx
            embed_onnx.main()
        else:
            print(f"[attune-worker] unknown tool: {tool} (known: {', '.join(TOOLS)})",
                  file=sys.stderr)
            sys.exit(1)
    except SystemExit:
        raise
    except ImportError as e:
        # Two different situations reach here, so say which one it is rather than
        # guessing: the lean GUI bundle deliberately has no analyzer stack, while a
        # missing module inside the ANALYZER bundle means that build is incomplete.
        if not getattr(sys, "frozen", False):
            # A source run. attune/src/embed_onnx.py is on disk either way, so the
            # bundle test below says nothing useful; the real cause is the interpreter.
            here = "this Python"
            hint = ("it has no librosa/onnxruntime — run under the ML venv, or set one "
                    "in Preferences")
        else:
            me = is_analyzer_bundle(base) if analyzer is None else bool(analyzer)
            here = "the analyzer" if me else "this GUI build"
            hint = ("this build is missing a module the analyzer needs — rebuild it"
                    if me else
                    r"analysis lives in analyzer\AttuneAnalyzer.exe — run that instead")
        print(f"[attune-worker] {e}\n[attune-worker] {here}: {hint}", file=sys.stderr)
        sys.exit(2)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
