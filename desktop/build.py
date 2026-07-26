"""Build Attune for Windows with PyInstaller — TWO programs, one install.

    python attune/desktop/build.py                 # both bundles
    python attune/desktop/build.py --gui-only      # skip the (slow) analyzer rebuild
    python attune/desktop/build.py --analyzer-only # rebuild ONLY the analyzer, in place

Output layout:

    dist/Attune/
      Attune.exe                 the GUI: flask + numpy + mutagen + requests + webview
      _internal/…                and onnxruntime for the learned engine
      analyzer/
        AttuneAnalyzer.exe       the audio analyzer: librosa + numba + scipy +
        _internal/…              onnxruntime + the 276 MB CLAP encoder + ffmpeg/ffprobe

WHY TWO. Analyzing new audio needs ~800 MB of machinery that PLAYING and MIXING
never touch — the vectors a mix is built from are already in mixer.db. Shipping it
all inside Attune.exe made the program the user launches every day eight times
bigger than it needs to be, and gave the virus scanner that much more to walk on a
cold first run. So the GUI carries only what the GUI uses, and web/scanjob.py hands
analysis to AttuneAnalyzer.exe (frozen + no ML venv). Everything is still in the
one install folder: nothing to download, no second install step.

The analyzer is a CONSOLE build on purpose. A --windowed PyInstaller exe has no
usable stdout, so the progress lines scanjob.py streams into the UI would go
nowhere; scanjob passes CREATE_NO_WINDOW, so no console window appears anyway.

Paths resolve from this file, so it runs from any working directory.
Needs: pip install pywebview pyinstaller
NEVER run this while Attune.exe is running — PyInstaller's clean step half-deletes
the live install and then dies on the locked .pyd.
"""
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ATT = os.path.dirname(HERE)                       # .../attune
DIST = os.path.join(ATT, "dist")
GUI_DIR = os.path.join(DIST, "Attune")
STAGE = os.path.join(DIST, "_analyzer_stage")
ANALYZER_DEST = os.path.join(GUI_DIR, "analyzer")

# ---------------------------------------------------------------- GUI bundle
# (source path relative to attune/, destination dir inside the bundle)
# EVERY file app.py / studio.py load by importlib-path or serve statically must be
# here — PyInstaller cannot see importlib-by-path loads, and a missing static file
# 404s at runtime (that exact gap shipped a dead UI once already).
GUI_DATA = [
    ("web/app.py",             "attune/web"),
    ("web/studio.py",          "attune/web"),
    # backend modules app.py pulls in by file path
    ("web/userdata.py",        "attune/web"),
    ("web/scanjob.py",         "attune/web"),
    ("web/autoscan.py",        "attune/web"),
    ("web/exportjob.py",       "attune/web"),
    ("web/smartlists.py",      "attune/web"),
    ("web/recipes.py",         "attune/web"),
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
    # Analyzer SCRIPTS (not the analyzer PAYLOAD). When a user configures an ML venv,
    # scanjob runs these under THEIR python, which supplies librosa/torch — so the
    # scripts must exist on disk here even though this bundle carries no heavy libs.
    # They are kilobytes. embed.py in particular: leaving it out is what made a
    # packaged scan die at the embed stage for anyone with an ML venv configured.
    ("src/scan.py",            "attune/src"),
    ("src/features.py",        "attune/src"),
    ("src/db.py",              "attune/src"),
    ("src/embed.py",           "attune/src"),
    # learned-metric engine (`engine: learned` in settings) — onnxruntime + this head
    # are the only ONNX pieces the GUI itself can use. The 276 MB CLAP encoder is
    # analysis-only and lives with the analyzer.
    ("src/models/metric_head.onnx",  "attune/src/models"),
    ("src/models/learned_norm.json", "attune/src/models"),
]

# imported dynamically / not statically visible (watchdog picks its platform backend
# by conditional import; autoscan.py itself is loaded by file path)
GUI_HIDDEN = ["mutagen", "requests", "numpy", "watchdog"]
GUI_COLLECT_ALL = ["onnxruntime"]          # learned engine; everything else is analysis
# The analyzer stack must NEVER land in the GUI bundle — that is the entire point of
# the split. torch/transformers are the reference encoder (embed.py runs under the
# user's own ML venv, never in-process here).
GUI_EXCLUDE = ["torch", "transformers", "librosa", "numba", "llvmlite", "scipy",
               "sklearn", "soundfile", "audioread", "pooch", "lazy_loader"]

# ----------------------------------------------------------- analyzer bundle
ANALYZER_DATA = [
    ("src/scan.py",       "attune/src"),
    ("src/features.py",   "attune/src"),
    ("src/db.py",         "attune/src"),
    ("src/embed_onnx.py", "attune/src"),
    # embed_onnx.py resolves MODELS_DIR from its own __file__, so this dest makes it
    # land on .../attune/src/models inside the analyzer bundle automatically.
    ("src/models/clap_music.onnx", "attune/src/models"),
    ("src/models/clap_norm.json",  "attune/src/models"),
]
ANALYZER_HIDDEN = ["mutagen", "numpy"]
ANALYZER_COLLECT_ALL = ["librosa", "numba", "llvmlite", "scipy", "soundfile",
                        "onnxruntime", "pooch", "audioread", "lazy_loader"]
ANALYZER_EXCLUDE = ["torch", "transformers", "flask", "webview"]

# Bundled ffmpeg/ffprobe (redistributed as separate unmodified programs, not linked —
# see attune/NOTICE.md). worker_entry.run() prepends this dir to PATH inside the
# analyzer process, so scan.py's ffprobe tag reads and librosa/audioread's ffmpeg
# decode fallback resolve to these instead of a system install. Appended only if
# present, so a machine without them can still produce a build.
FFBIN = [
    ("desktop/ffbin/ffmpeg.exe",  "attune/bin"),
    ("desktop/ffbin/ffprobe.exe", "attune/bin"),
]


def _pyinstaller(name, entry, data, hidden, collect_all, exclude, distpath,
                 workpath, windowed):
    for src, _dest in data:
        full = os.path.join(ATT, src)
        if not os.path.exists(full):
            raise SystemExit(f"[build] missing source file: {full}")
    args = [sys.executable, "-m", "PyInstaller", "--name", name,
            "--windowed" if windowed else "--console",
            "--noconfirm", "--clean", "--noupx",
            "--distpath", distpath, "--workpath", workpath, "--specpath", HERE]
    for m in hidden:
        args += ["--collect-submodules", m]
    for m in collect_all:
        args += ["--collect-all", m]
    for m in exclude:
        args += ["--exclude-module", m]
    for src, dest in data:
        args += ["--add-data", f"{os.path.join(ATT, src)}{os.pathsep}{dest}"]
    args.append(entry)
    print(f"\n[build] === {name} ===\n" + " ".join(args))
    rc = subprocess.call(args)
    if rc != 0:
        raise SystemExit(f"[build] {name} FAILED (rc={rc})")


def build_gui():
    _pyinstaller(
        "Attune", os.path.join(HERE, "app_desktop.py"),
        GUI_DATA, GUI_HIDDEN, GUI_COLLECT_ALL + ["webview"], GUI_EXCLUDE,
        DIST, os.path.join(ATT, "build", "gui"), windowed=True)


def build_analyzer():
    data = list(ANALYZER_DATA)
    for src, dest in FFBIN:
        if os.path.exists(os.path.join(ATT, src)):
            data.append((src, dest))
        else:
            print(f"[build] NOTE: {src} not found — analyzer will rely on a system "
                  f"ffmpeg/ffprobe on PATH")
    if os.path.isdir(STAGE):
        shutil.rmtree(STAGE)
    _pyinstaller(
        "AttuneAnalyzer", os.path.join(HERE, "analyzer_main.py"),
        data, ANALYZER_HIDDEN, ANALYZER_COLLECT_ALL, ANALYZER_EXCLUDE,
        STAGE, os.path.join(ATT, "build", "analyzer"), windowed=False)
    # Land it INSIDE the GUI install, as a subfolder. Built to a staging dir first
    # because the GUI build wipes dist/Attune wholesale (PyInstaller COLLECT removes
    # its output dir), which would take the analyzer with it if it were built first.
    built = os.path.join(STAGE, "AttuneAnalyzer")
    if not os.path.isdir(built):
        raise SystemExit(f"[build] analyzer output not found: {built}")
    if os.path.isdir(ANALYZER_DEST):
        shutil.rmtree(ANALYZER_DEST)
    shutil.move(built, ANALYZER_DEST)
    shutil.rmtree(STAGE, ignore_errors=True)


def main():
    gui_only = "--gui-only" in sys.argv
    analyzer_only = "--analyzer-only" in sys.argv
    if not analyzer_only:
        build_gui()                   # first: it wipes dist/Attune
    elif not os.path.isdir(GUI_DIR):
        raise SystemExit(f"[build] --analyzer-only needs an existing GUI build at {GUI_DIR}")
    if not gui_only:
        build_analyzer()              # second: lands in dist/Attune/analyzer
    exe = os.path.join(GUI_DIR, "Attune.exe")
    ana = os.path.join(ANALYZER_DEST, "AttuneAnalyzer.exe")
    print(f"\n[build] GUI      : {exe}  ({'ok' if os.path.exists(exe) else 'MISSING'})")
    print(f"[build] analyzer : {ana}  "
          f"({'ok' if os.path.exists(ana) else 'skipped' if gui_only else 'MISSING'})")


if __name__ == "__main__":
    main()
