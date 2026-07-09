"""Build Attune.exe from app_desktop.py with PyInstaller.

    python attune/desktop/build.py             # one-dir  -> attune/dist/Attune/Attune.exe
    python attune/desktop/build.py --onefile   # single    -> attune/dist/Attune.exe

Paths are resolved from this file, so it works from any working directory.
Needs: pip install pywebview pyinstaller
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ATT = os.path.dirname(HERE)                       # .../attune


def _data(src_rel, dest):
    return ["--add-data", f"{os.path.join(ATT, src_rel)};{dest}"]


def main():
    args = [sys.executable, "-m", "PyInstaller", "--name", "Attune", "--windowed",
            "--noconfirm", "--clean",
            "--distpath", os.path.join(ATT, "dist"),
            "--workpath", os.path.join(ATT, "build"),
            "--specpath", HERE,
            "--collect-all", "webview"]
    if "--onefile" in sys.argv:
        args.append("--onefile")
    # app.py loads hybrid.py / export.py by file path, so ship them as data at the
    # same relative layout the launcher expects inside the bundle.
    args += _data("web/app.py", "attune/web")
    args += _data("src/hybrid.py", "attune/src")
    args += _data("src/export.py", "attune/src")
    args.append(os.path.join(HERE, "app_desktop.py"))
    print("running:", " ".join(args))
    raise SystemExit(subprocess.call(args))


if __name__ == "__main__":
    main()
