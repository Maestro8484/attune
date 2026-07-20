"""Build Attune.exe from app_desktop.py with PyInstaller.

    python attune/desktop/build.py             # one-dir  -> attune/dist/Attune/Attune.exe
    python attune/desktop/build.py --onefile   # single    -> attune/dist/Attune.exe

Paths are resolved from this file, so it works from any working directory.
Needs: pip install pywebview pyinstaller

Bundles the whole Attune Studio: app.py + studio.py + the static UI, and every src/ module
app.py loads by file path (hybrid, engine, musicip_engine, export). Miss one and the frozen
app import-errors at launch, because PyInstaller can't see importlib-by-path loads.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ATT = os.path.dirname(HERE)                       # .../attune

# (source path relative to attune/, destination dir inside the bundle)
# EVERY file app.py / studio.py load by importlib-path or serve statically must be here —
# PyInstaller can't see importlib-by-path loads, and a missing static file 404s at runtime.
DATA = [
    ("web/app.py",             "attune/web"),
    ("web/studio.py",          "attune/web"),
    # backend modules app.py pulls in by file path (settings, user data, scan, auto-playlists)
    ("web/userdata.py",        "attune/web"),
    ("web/scanjob.py",         "attune/web"),
    ("web/autoscan.py",        "attune/web"),
    ("web/exportjob.py",       "attune/web"),
    ("web/smartlists.py",      "attune/web"),
    ("src/config.py",          "attune/src"),
    # the Studio front-end: studio.html loads all five scripts + the stylesheet
    ("web/static/studio.html", "attune/web/static"),
    ("web/static/studio.css",  "attune/web/static"),
    ("web/static/studio.js",   "attune/web/static"),
    ("web/static/player.js",   "attune/web/static"),
    ("web/static/prefs.js",    "attune/web/static"),
    ("web/static/smartlist.js","attune/web/static"),
    ("web/static/boot.js",     "attune/web/static"),
    ("src/hybrid.py",          "attune/src"),
    ("src/engine.py",          "attune/src"),
    ("src/musicip_engine.py",  "attune/src"),
    ("src/export.py",          "attune/src"),
    # torch-free analyzer worker modules — app_desktop.py's --attune-worker sentinel
    # imports these by bare name (`import scan`, `import embed_onnx`) after inserting
    # this dir at sys.path[0], so PyInstaller's static analysis can't see them either.
    ("src/scan.py",            "attune/src"),
    ("src/features.py",        "attune/src"),
    ("src/db.py",              "attune/src"),
    ("src/embed_onnx.py",      "attune/src"),
    # ONNX CLAP twin + its norm constants (embed_onnx.py resolves MODELS_DIR relative
    # to its own __file__, which inside the bundle is .../attune/src/embed_onnx.py, so
    # this dest path makes MODELS_DIR resolve to .../attune/src/models automatically).
    ("src/models/clap_music.onnx",   "attune/src/models"),
    ("src/models/clap_norm.json",    "attune/src/models"),
    ("src/models/metric_head.onnx",  "attune/src/models"),
    ("src/models/learned_norm.json", "attune/src/models"),
]

# Bundled ffmpeg/ffprobe (redistributed as separate unmodified programs, not linked —
# see attune/NOTICE.md). The worker (app_desktop.py:_run_worker) prepends the bundled
# dir "attune/bin" to PATH, so scan.py's ffprobe tag reads and librosa/audioread's
# ffmpeg decode fallback resolve to these instead of a system install. Appended only if
# present (a non-Windows build won't have these Windows .exe's), so the build still runs.
FFBIN = [
    ("desktop/ffbin/ffmpeg.exe",  "attune/bin"),
    ("desktop/ffbin/ffprobe.exe", "attune/bin"),
]

# imported dynamically / not statically visible from app_desktop.py's import list
# (watchdog: autoscan.py is loaded by file path, and watchdog.observers picks its
# platform backend via conditional imports — both invisible to static analysis)
HIDDEN = ["mutagen", "requests", "numpy", "watchdog"]

# packages with fiddly native/data payloads PyInstaller's default import hooks tend
# to miss (native DLLs, data files, lazy submodules) — collect everything for each.
COLLECT_ALL = ["librosa", "numba", "llvmlite", "scipy", "soundfile", "onnxruntime",
               "pooch", "audioread", "lazy_loader"]

# torch/transformers are the reference/training-path encoder (embed.py) and must
# NEVER end up in the frozen bundle — the whole point of the ONNX twin is to not
# need them. Exclude explicitly so a transitive import can't sneak them in.
EXCLUDE = ["torch", "transformers"]


def main():
    for src, _dest in DATA:
        full = os.path.join(ATT, src)
        if not os.path.exists(full):
            raise SystemExit(f"[build] missing source file: {full}")

    args = [sys.executable, "-m", "PyInstaller", "--name", "Attune", "--windowed",
            "--noconfirm", "--clean", "--noupx",
            "--distpath", os.path.join(ATT, "dist"),
            "--workpath", os.path.join(ATT, "build"),
            "--specpath", HERE,
            "--collect-all", "webview"]
    for m in HIDDEN:
        args += ["--collect-submodules", m]
    for m in COLLECT_ALL:
        args += ["--collect-all", m]
    for m in EXCLUDE:
        args += ["--exclude-module", m]
    if "--onefile" in sys.argv:
        args.append("--onefile")
    for src, dest in DATA:
        args += ["--add-data", f"{os.path.join(ATT, src)}{os.pathsep}{dest}"]
    for src, dest in FFBIN:
        full = os.path.join(ATT, src)
        if os.path.exists(full):
            args += ["--add-data", f"{full}{os.pathsep}{dest}"]
        else:
            print(f"[build] NOTE: {full} not found — building WITHOUT bundled ffmpeg "
                  f"(scan will rely on a system ffmpeg/ffprobe on PATH)")
    args.append(os.path.join(HERE, "app_desktop.py"))

    print("running:", " ".join(args))
    raise SystemExit(subprocess.call(args))


if __name__ == "__main__":
    main()
