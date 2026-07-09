# Attune desktop (one-click app)

`app_desktop.py` wraps the Attune web UI in a native window (pywebview + the
Windows WebView2 runtime), so it runs like an installed program — no browser
tab, no terminal.

## Run from source
```
python attune/desktop/app_desktop.py
```

## Build a standalone .exe
```
pip install pywebview pyinstaller
python attune/desktop/build.py             # -> attune/dist/Attune/Attune.exe
python attune/desktop/build.py --onefile   # single-file exe instead of a folder
```
Then double-click `Attune.exe` (or a Desktop shortcut pointed at it).

The build is lightweight (~60 MB): the runtime only needs `flask` + `numpy`.
The heavy analyze/embed dependencies (torch, librosa) are **not** bundled —
those build the database offline; the app just reads it.

## Finding your library
On launch it looks for the music DB in this order:
1. the `ATTUNE_DB` environment variable,
2. `mixer.db` or `data/mixer.db` sitting next to the program,
3. a `mixer-ng/data/mixer.db` found by walking up from the program — so it
   works automatically when the exe lives inside the repo.

To run it on a machine without the repo, copy your `mixer.db` next to `Attune.exe`.

## Requirements
Windows 10/11 with the Microsoft Edge WebView2 runtime (present by default on
current Windows). No Python is needed to run the built exe.
