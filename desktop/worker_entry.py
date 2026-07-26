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


def run(tool, rest, fallback_root, bin_fallback):
    """Run one analyzer tool in THIS process and exit. Never returns.

    tool          "scan" | "embed_onnx" (module under attune/src whose main() we call)
    rest          argv for that tool
    fallback_root the attune/ dir to use when not frozen
    bin_fallback  the ffmpeg/ffprobe dir to use when not frozen
    """
    # Bundled ffmpeg/ffprobe must beat any system install for this process: scan.py
    # shells out to `ffprobe` for tags, and librosa/audioread fall back to `ffmpeg`
    # to decode what libsndfile cannot (m4a/aac/wma). Prepending here — worker only,
    # the GUI never decodes — is what lets a first-run scan need NO system ffmpeg.
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
    if os.path.isdir(bin_dir):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ.get("PATH", "")

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
        here = "the analyzer" if os.path.isdir(os.path.join(base, "bin")) else "this GUI build"
        hint = ("this build is missing a module the analyzer needs — rebuild it"
                if here == "the analyzer" else
                r"analysis lives in analyzer\AttuneAnalyzer.exe — run that instead")
        print(f"[attune-worker] {e}\n[attune-worker] {here}: {hint}", file=sys.stderr)
        sys.exit(2)
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
