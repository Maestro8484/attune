r"""Build Attune for Windows with PyInstaller — TWO programs, one install.

    python attune/desktop/build.py                 # both bundles
    python attune/desktop/build.py --gui-only      # rebuild the GUI, KEEP the analyzer
    python attune/desktop/build.py --analyzer-only # rebuild ONLY the analyzer, in place
    python attune/desktop/build.py --dist D --work W    # build somewhere else entirely

--gui-only rebuilds Attune.exe and keeps whatever analyzer\ is already in the output
folder. It has to be said explicitly because PyInstaller's COLLECT step deletes
dist\Attune wholesale before it writes, which used to take the analyzer with it and
leave an install that could not analyze anything — a silent 700 MB hole, since the GUI
runs perfectly well without it until you press Rescan. The analyzer is moved to a
uniquely named sibling folder first and moved back afterwards, restored even if the
build dies; if the restore itself fails the backup is kept and its path printed.

--dist and --work put the output and the scratch anywhere, so a release or test build
never has to touch the daily install. Defaults are unchanged (attune\dist,
attune\build), which is what BUILD_ATTUNE.bat relies on.

Output layout:

    dist/Attune/
      Attune.exe                 the GUI: flask + numpy + mutagen + requests + webview
      _internal/…                and onnxruntime for the learned engine
      analyzer/
        AttuneAnalyzer.exe       the audio analyzer: librosa + numba + scipy +
        _internal/…              onnxruntime, the 276 MB CLAP encoder, and ffmpeg.exe

ffprobe.exe is gone: 141 MB for one job, reading tags, which src/scan.py now does with
mutagen (operator ruling B5 of 2026-08-04, executed for v0.1.0). ffmpeg.exe STAYS. The
v0.1.0 proof measured 22 of a random 300 mp3 files (7.33%) from a real library that
libsndfile alone cannot decode; dropping ffmpeg too would have cost a stranger about one
mp3 in fourteen. That is the fallback the release rulings sheet recorded for exactly this
outcome (item 7, 2026-09-20). Saving: 141 MB installed, 42 MB of download.

A build therefore needs desktop\ffbin\ffmpeg.exe. It is gitignored and absent from any
fresh clone or worktree, so a build without it is REFUSED rather than quietly shipping an
app that cannot read 7% of a library. Pass --no-ffmpeg to build one deliberately.

WHY TWO. Analyzing new audio needs ~500 MB of machinery that PLAYING and MIXING
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
import argparse
import contextlib
import glob
import os
import shutil
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ATT = os.path.dirname(HERE)                       # .../attune
DEFAULT_DIST = os.path.join(ATT, "dist")
DEFAULT_WORK = os.path.join(ATT, "build")


def gui_dir(dist):
    return os.path.join(dist, "Attune")


def stage_dir(dist):
    return os.path.join(dist, "_analyzer_stage")


def analyzer_dir(dist):
    return os.path.join(gui_dir(dist), "analyzer")

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
    ("web/libreload.py",       "attune/web"),
    ("web/libverify.py",       "attune/web"),
    ("web/applog.py",          "attune/web"),
    ("web/ledger.py",          "attune/web"),
    ("web/audioinfo.py",       "attune/web"),
    ("web/plexsyncjob.py",     "attune/web"),
    ("src/config.py",          "attune/src"),
    # the Studio front-end: studio.html loads all five scripts + the stylesheet
    ("web/static/studio.html", "attune/web/static"),
    ("web/static/studio.css",  "attune/web/static"),
    ("web/static/studio.js",   "attune/web/static"),
    ("web/static/player.js",   "attune/web/static"),
    ("web/static/prefs.js",    "attune/web/static"),
    ("web/static/smartlist.js","attune/web/static"),
    ("web/static/boot.js",     "attune/web/static"),
    ("web/static/tips.js",     "attune/web/static"),   # hover help, 2026-09-22
    ("src/hybrid.py",          "attune/src"),
    ("src/engine.py",          "attune/src"),
    ("src/musicip_engine.py",  "attune/src"),
    ("src/export.py",          "attune/src"),
    # plexsyncjob.py loads this by file path, exactly the way app.py loads the web
    # modules above -- PyInstaller cannot see an importlib-by-path load, and a missing
    # one is a runtime crash, not a build error (the ledger.py and audioinfo.py
    # incidents were both this).
    ("src/plexmatch.py",       "attune/src"),
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
GUI_HIDDEN = ["mutagen", "requests", "numpy", "watchdog", "logging.handlers"]
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

# Bundled ffmpeg (redistributed as a separate unmodified program, not linked — see
# attune/NOTICE.md). worker_entry.run() prepends this dir to PATH inside the analyzer
# process, so librosa/audioread's decode fallback resolves to it rather than a system
# install. Added only if present, so a machine without it can still produce a build.
#
# ffprobe.exe was dropped on 2026-09-20: 141 MB for one job, reading tags, which
# src/scan.py now does with mutagen. Measured on 238 real library files the same day,
# mutagen was never worse and was right where the two disagreed.
#
# ffmpeg.exe STAYS, against the original intent of ruling B5, because the proof said
# so: on an unbiased random sample of 300 mp3 files from the operator's library, 22
# (7.33%) could not be decoded by libsndfile alone and needed ffmpeg's more forgiving
# mp3 decoder. flac was 0 of 59. Dropping both would have quietly cost a stranger
# roughly one mp3 in fourteen. This is the fallback the release rulings sheet already
# recorded for exactly this outcome (item 7, 2026-09-20).
FFBIN = [
    ("desktop/ffbin/ffmpeg.exe",  "attune/bin"),
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


def build_gui(dist, work):
    _pyinstaller(
        "Attune", os.path.join(HERE, "app_desktop.py"),
        GUI_DATA, GUI_HIDDEN, GUI_COLLECT_ALL + ["webview"], GUI_EXCLUDE,
        dist, os.path.join(work, "gui"), windowed=True)


def check_inputs(allow_no_ffmpeg):
    """Refuse a build whose inputs are missing, BEFORE anything is deleted.

    This runs at the very top of main(). It used to live inside build_analyzer(), which
    is called AFTER build_gui() has already wiped dist\\Attune -- so on a default build
    the refusal arrived too late and destroyed the very install it exists to protect.
    A guard that fires after the damage is worse than no guard: it reads as safety."""
    # The model first, because there is no --no-model and there should not be: a build
    # without the encoder cannot analyze a single file, so it is not a reduced build,
    # it is a broken one. It was left out of this list until 2026-09-21, and the only
    # thing that noticed was _pyinstaller's own per-file check inside build_analyzer()
    # -- which runs AFTER build_gui() has wiped dist/Attune. So the case this function
    # was written for, a fresh worktree or a clone, where the 276 MB model is absent by
    # design, destroyed the existing install and only then said why.
    no_model = [src for src, _dest in ANALYZER_DATA
                if src.startswith("src/models/")
                and not os.path.exists(os.path.join(ATT, src))]
    if no_model:
        raise SystemExit(
            f"[build] REFUSING to build: {', '.join(no_model)} is missing. NOTHING has "
            f"been deleted or changed.\n"
            f"[build]   looked in: {os.path.join(ATT, no_model[0])}\n"
            f"[build] The 276 MB listening model is not kept in the repository, so a "
            f"fresh clone or a git worktree never has it. Run tools/fetch_model.py, or "
            f"copy src/models/ in from the main checkout.")

    missing = [src for src, _dest in FFBIN
               if not os.path.exists(os.path.join(ATT, src))]
    if not missing:
        return
    if allow_no_ffmpeg:
        for src in missing:
            print(f"[build] --no-ffmpeg: building WITHOUT {src}. About 7% of real-world "
                  f"mp3 files will not analyze in this build. Do not ship it.")
        return
    raise SystemExit(
        f"[build] REFUSING to build: {', '.join(missing)} is missing. NOTHING has been "
        f"deleted or changed.\n"
        f"[build]   looked in: {os.path.join(ATT, missing[0])}\n"
        f"[build] Without it roughly one mp3 in fourteen cannot be decoded (measured 22 "
        f"of 300, 2026-09-20), and nothing at run time would say so. desktop\\ffbin\\ is "
        f"gitignored, so a fresh clone or a git worktree never has it: copy ffmpeg.exe "
        f"in from the main checkout.\n"
        f"[build] To build without it on purpose, pass --no-ffmpeg.")


def build_analyzer(dist, work, allow_no_ffmpeg=False):
    data = list(ANALYZER_DATA)
    for src, dest in FFBIN:
        if os.path.exists(os.path.join(ATT, src)):
            data.append((src, dest))
        elif not allow_no_ffmpeg:
            # check_inputs() already refused at the top of main(); this is the belt to
            # that braces, for a caller that reaches build_analyzer() another way.
            raise SystemExit(f"[build] {src} is missing and --no-ffmpeg was not given")
    stage = stage_dir(dist)
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    _pyinstaller(
        "AttuneAnalyzer", os.path.join(HERE, "analyzer_main.py"),
        data, ANALYZER_HIDDEN, ANALYZER_COLLECT_ALL, ANALYZER_EXCLUDE,
        stage, os.path.join(work, "analyzer"), windowed=False)
    # Land it INSIDE the GUI install, as a subfolder. Built to a staging dir first
    # because the GUI build wipes dist/Attune wholesale (PyInstaller COLLECT removes
    # its output dir), which would take the analyzer with it if it were built first.
    built = os.path.join(stage, "AttuneAnalyzer")
    if not os.path.isdir(built):
        raise SystemExit(f"[build] analyzer output not found: {built}")
    dest = analyzer_dir(dist)
    if os.path.isdir(dest):
        shutil.rmtree(dest)
    shutil.move(built, dest)
    shutil.rmtree(stage, ignore_errors=True)


@contextlib.contextmanager
def preserve_analyzer(dist, enabled):
    """Carry an existing analyzer\\ folder across a GUI rebuild that would delete it.

    The backup is a uniquely named sibling of dist\\Attune, so it is on the same volume
    (the move is a rename, not a copy of 700 MB) and outside everything PyInstaller
    removes. Rules, in order of how badly they would hurt:

      - cannot move it aside  -> refuse to build at all, rather than destroy it.
      - build fails or is interrupted -> still put it back (finally, and SystemExit
        counts: _pyinstaller raises exactly that on a non-zero return code).
      - cannot put it back    -> keep the backup, print where it is, and say what to do.

    Doing nothing at all is the right behavior when there is no analyzer to keep, or
    when the caller is about to rebuild it anyway."""
    src = analyzer_dir(dist)
    if not enabled or not os.path.isdir(src):
        yield None
        return
    keep = os.path.join(dist, f"_analyzer_keep_{os.getpid()}_{int(time.time())}")
    print(f"[build] --gui-only: holding the existing analyzer at {keep}")
    try:
        shutil.move(src, keep)
    except OSError as e:
        raise SystemExit(
            f"[build] REFUSING to build: cannot move the existing analyzer aside.\n"
            f"[build]   {src}\n[build]   {e}\n"
            f"[build] Close Attune.exe and AttuneAnalyzer.exe and try again, or build "
            f"to a different --dist.")
    try:
        yield keep
    finally:
        try:
            if os.path.isdir(src):
                # Nothing in a GUI build writes analyzer\, so this should be
                # unreachable. If it happens, destroy neither copy and say so.
                print(f"[build] WARNING: something else created {src} during the build. "
                      f"Your original analyzer is still at {keep}; compare them and "
                      f"delete the one you do not want.")
            else:
                shutil.move(keep, src)
                print(f"[build] analyzer restored to {src}")
        except OSError as e:
            print(f"[build] WARNING: could not restore the analyzer: {e}")
            print(f"[build] Your analyzer is SAFE but in the wrong place. Move it back "
                  f"by hand:\n[build]   from: {keep}\n[build]   to  : {src}")


def main(argv=None):
    ap = argparse.ArgumentParser(
        description="Build Attune's two Windows programs with PyInstaller.")
    ap.add_argument("--gui-only", action="store_true",
                    help="rebuild Attune.exe only, keeping the analyzer folder that is "
                         "already in the output")
    ap.add_argument("--analyzer-only", action="store_true",
                    help="rebuild AttuneAnalyzer.exe only, into an existing GUI build")
    ap.add_argument("--dist", default=DEFAULT_DIST,
                    help=f"output folder (default {DEFAULT_DIST}). The built app lands "
                         f"in <dist>\\Attune.")
    ap.add_argument("--work", default=DEFAULT_WORK,
                    help=f"PyInstaller scratch folder (default {DEFAULT_WORK})")
    ap.add_argument("--no-ffmpeg", action="store_true",
                    help="build the analyzer without desktop/ffbin/ffmpeg.exe. About 7%% "
                         "of real mp3 files will not analyze. Never ship the result.")
    a = ap.parse_args(argv)
    if a.gui_only and a.analyzer_only:
        raise SystemExit("[build] --gui-only and --analyzer-only are opposites; pick one")

    dist = os.path.abspath(a.dist)
    work = os.path.abspath(a.work)
    gui = gui_dir(dist)
    print(f"[build] dist: {dist}\n[build] work: {work}")

    # A backup left behind by a build that was killed between move-aside and restore.
    # Say so before doing anything: otherwise the next --gui-only finds no analyzer,
    # reports it MISSING, and never mentions the 600 MB sitting next to it.
    for leftover in sorted(glob.glob(os.path.join(dist, "_analyzer_keep_*"))):
        print(f"[build] NOTE: {leftover} is left over from an interrupted build. "
              f"Move it back to {analyzer_dir(dist)} or delete it.")

    if a.analyzer_only and not os.path.isdir(gui):
        raise SystemExit(f"[build] --analyzer-only needs an existing GUI build at {gui}")

    # Every reason to refuse is checked HERE, before a single file is removed. --gui-only
    # does not build the analyzer, so it does not need ffmpeg.
    if not a.gui_only:
        check_inputs(a.no_ffmpeg)

    if not a.analyzer_only:
        with preserve_analyzer(dist, a.gui_only):
            build_gui(dist, work)         # wipes <dist>\Attune
    if not a.gui_only:
        # lands in <dist>\Attune\analyzer
        build_analyzer(dist, work, allow_no_ffmpeg=a.no_ffmpeg)

    exe = os.path.join(gui, "Attune.exe")
    ana = os.path.join(analyzer_dir(dist), "AttuneAnalyzer.exe")
    print(f"\n[build] GUI      : {exe}  ({'ok' if os.path.exists(exe) else 'MISSING'})")
    print(f"[build] analyzer : {ana}  "
          f"({'ok' if os.path.exists(ana) else 'MISSING'})")


if __name__ == "__main__":
    main()
