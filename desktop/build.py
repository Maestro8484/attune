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
DATA = [
    ("web/app.py",             "attune/web"),
    ("web/studio.py",          "attune/web"),
    ("web/static/studio.html", "attune/web/static"),
    ("web/static/studio.css",  "attune/web/static"),
    ("web/static/studio.js",   "attune/web/static"),
    ("src/hybrid.py",          "attune/src"),
    ("src/engine.py",          "attune/src"),
    ("src/musicip_engine.py",  "attune/src"),
    ("src/export.py",          "attune/src"),
]

# imported dynamically / not statically visible from app_desktop.py's import list
HIDDEN = ["mutagen", "requests", "numpy"]


def main():
    for src, _dest in DATA:
        full = os.path.join(ATT, src)
        if not os.path.exists(full):
            raise SystemExit(f"[build] missing source file: {full}")

    args = [sys.executable, "-m", "PyInstaller", "--name", "Attune", "--windowed",
            "--noconfirm", "--clean",
            "--distpath", os.path.join(ATT, "dist"),
            "--workpath", os.path.join(ATT, "build"),
            "--specpath", HERE,
            "--collect-all", "webview"]
    for m in HIDDEN:
        args += ["--collect-submodules", m]
    if "--onefile" in sys.argv:
        args.append("--onefile")
    for src, dest in DATA:
        args += ["--add-data", f"{os.path.join(ATT, src)}{os.pathsep}{dest}"]
    args.append(os.path.join(HERE, "app_desktop.py"))

    print("running:", " ".join(args))
    raise SystemExit(subprocess.call(args))


if __name__ == "__main__":
    main()
