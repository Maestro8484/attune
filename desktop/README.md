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
python attune/desktop/package.py           # fold your mixer.db + a README into the bundle
```
Then double-click `Attune.exe` (or a Desktop shortcut pointed at it). `build.py --onefile`
makes a single exe instead of a folder (slower to start; skip for testing).

The build is self-contained and moderate-sized: the runtime needs only `flask`, `numpy`,
`mutagen`, `requests` and the webview shell. The heavy analyze/embed dependencies (torch,
librosa, CLAP) are **not** bundled — those build the database offline; the app only reads it.

## Engine: auto-detected, never crashes
On launch the app probes `http://localhost:10002/api/version`. If a MusicIP Mixer is
running there, it uses it as the mix engine; otherwise it falls back to the built-in **V2**
engine (which reads the CLAP/librosa vectors already in `mixer.db` — no MusicIP, no network,
no torch). Either way it starts and mixes. MusicIP itself is closed third-party software and
is **not** bundled or launched by this app.

## Finding your library
On launch it looks for the music DB in this order:
1. the `ATTUNE_DB` environment variable,
2. `mixer.db` or `data/mixer.db` sitting next to the program (this is what `package.py` sets up),
3. a `mixer-ng/data/mixer.db` found by walking up from the program — so it works automatically
   when the exe lives inside the repo.

`package.py` copies your `mixer.db` next to `Attune.exe`, so the packaged folder just works.

## Playlist folder
Put a folder named `Playlists` next to `Attune.exe`, or set `ATTUNE_PLAYLIST_DIR`, to browse
and save `.m3u8` playlists in Studio.

## What the bundle still needs on the target PC (not bundled)
- The **music files** at their stored paths — playback and album art stream live from disk.
- Optional MusicIP Mixer (better engine) and optional Plex config (`.env`) for Plex export.

## Requirements
Windows 10/11 with the Microsoft Edge WebView2 runtime (present by default on
current Windows). No Python is needed to run the built exe.
