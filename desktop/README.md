# Attune desktop

`app_desktop.py` wraps the Attune web UI in a native window, using pywebview and the Windows
WebView2 runtime. No browser tab, no terminal. This is what the installer ships.

Most people want [the installer](../INSTALL.md), not this folder.

## Run from source

```
python desktop/app_desktop.py
```

It starts the same Flask app that `web/app.py` serves and points a window at it. If you'd
rather work in a browser, run `python web/app.py --port 8778` instead and open
`http://127.0.0.1:8778/`.

## Build the app

```
pip install -r desktop/requirements-build.txt
python desktop/build.py                 # -> dist/Attune/Attune.exe
```

`build.py --gui-only` rebuilds only `Attune.exe` and keeps the analyzer that's already in
the output folder, which is much faster while you're working on the UI. `--analyzer-only`
does the reverse.

**A fresh clone cannot build until you do one thing.** The build needs
`desktop/ffbin/ffmpeg.exe`, and `desktop/ffbin/` is git-ignored because the binary is about
148 MB. So a clone or a git worktree never has it, and `build.py` **refuses to build**
rather than quietly producing an app that can't decode part of a real library. Download an
ffmpeg build for Windows, put `ffmpeg.exe` in `desktop/ffbin/`, and build again. `--no-ffmpeg`
builds without it deliberately, at the cost of roughly one mp3 in fourteen from a real
library failing to analyse.

Never run a build while `Attune.exe` is running. PyInstaller's clean step half-deletes the
live install and then dies on a locked file.

`build.py`'s own docstring is the authoritative description of the layout and is kept current
with the code. See also [../docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md).

## Two programs in one folder

```
dist/Attune/
  Attune.exe              the app you launch: flask, numpy, mutagen, onnxruntime, webview
  _internal/
  analyzer/
    AttuneAnalyzer.exe    librosa, numba, scipy, onnxruntime, the CLAP encoder, ffmpeg
    _internal/
```

Analysing audio needs roughly half a gigabyte of machinery that playing and mixing never
touch, because the numbers a mix is built from are already in the database. Keeping it in a
separate program means the app you launch every day stays small and cold-starts quickly.
`web/scanjob.py` runs the analyzer for you when you scan. It's still one install.

## The engine it uses

On launch the app probes `http://localhost:10002/api/version`. If a MusicIP Mixer is running
there, it uses that as the mix engine; otherwise it falls back to Attune's own **V2** engine,
which reads the vectors already in the library. No MusicIP, no network, no PyTorch. Either
way it starts and mixes.

MusicIP itself is closed third-party software and is neither bundled nor launched by this app.

## Finding your library

In this order:

1. The database path saved in your settings, which is what Preferences writes under Advanced.
2. The `ATTUNE_DB` environment variable.
3. `mixer.db` or `data/mixer.db` sitting next to the program.
4. A last-resort walk up the folder tree, so a build run from inside a development checkout
   finds the checkout's own library without being told anything.

Whichever it lands on is written back into settings, so it only has to be found once. If
there's no library at all, the first-run wizard opens and asks for a music folder.

## Playlist folder

`ATTUNE_PLAYLIST_DIR`, the folder saved in Preferences, or a folder named `Playlists` next
to `Attune.exe`. That's where Studio browses and saves `.m3u8` files.

## What the built app still needs on the target PC

- **The music files**, at the paths stored in the library. Mixing and browsing work without
  them; playback and album art read the file off disk.
- Optionally, a MusicIP Mixer on port 10002 for the alternative engine.
- Optionally, a Plex server. Its address and key are fields in Preferences, under Plex.
  Nothing has to be edited by hand.

## Requirements

Windows 10 or 11 with the Microsoft Edge WebView2 runtime, which is present by default on
current Windows. No Python is needed to run the built app.

## Not a release tool

`package.py` folds a real library database into the app folder to make a private test bundle.
That database names every music file, album and track on the machine, so the bundle it builds
must never be shared, and the script refuses to run without
`--i-know-this-contains-my-library`. To make a release, use `tools/make_release.py`. See
[../docs/RELEASING.md](../docs/RELEASING.md).
