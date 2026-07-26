"""Assemble a ready-to-test Attune bundle from the PyInstaller output.

    python attune/desktop/build.py       # first: produce attune/dist/Attune/
    python attune/desktop/package.py     # then: fold in the library DB + a README

Produces attune/dist/Attune/ containing:
    Attune.exe            the app
    _internal/            its runtime (PyInstaller)
    analyzer/             AttuneAnalyzer.exe + its runtime — analyzing new audio
    mixer.db              a COPY of the analyzed library, so the exe is self-locating
    README.txt            what it needs and what it can't do

This copy of the folder is fully self-contained for an ALREADY-ANALYZED library: double-
click Attune.exe and it runs. It still streams the actual audio files live from their
stored paths (the music itself is far too large to bundle), and it uses a live MusicIP
Mixer only if one happens to be running -- otherwise the built-in V2 engine.

NOTE: mixer.db is personal data (your library's paths + metadata). This bundle is for
YOUR testing. Do not redistribute it; a public installer ships WITHOUT mixer.db and asks
the user to analyze their own library.
"""
import os
import shutil
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ATT = os.path.dirname(HERE)
ROOT = os.path.dirname(ATT)

DIST = os.path.join(ATT, "dist", "Attune")
DB_SRC = os.path.join(ROOT, "mixer-ng", "data", "mixer.db")

README = """Attune Studio — test bundle
===========================

Double-click  Attune.exe  to run. A window appears in about 3 seconds; the library
finishes loading a couple of seconds later.

WHAT THIS BUNDLE INCLUDES
  - Attune.exe + its Python runtime (_internal\\)
  - analyzer\\AttuneAnalyzer.exe : analyzes new music (librosa + the CLAP encoder +
    ffmpeg/ffprobe, all bundled). Kept as a separate program so the app you launch
    every day stays small; Attune runs it for you when you scan.
  - mixer.db : your analyzed library (21k tracks, CLAP + librosa vectors, metadata)

WHAT IT STILL NEEDS ON THIS PC (not bundled -- too large / third-party)
  - The music files themselves, at their stored paths (L:\\ / the NAS share).
    Mixing and browsing work without them; PLAYBACK and album ART need the files present.
  - Optional: MusicIP Mixer running with its API on port 10002. If it's up, Attune uses it
    as the mix engine; if not, Attune uses its own built-in V2 engine automatically.
  - Optional: a Plex server + the .env config, for "Create Plex playlist".

PLAYLIST FOLDER
  Put a folder named  Playlists  next to Attune.exe, OR set the environment variable
  ATTUNE_PLAYLIST_DIR to your playlist folder, to browse and save .m3u8 playlists.

WHAT IT CANNOT DO
  - Launch MusicIP Mixer for you. MusicIP is separate, closed software.

ANALYZING NEW MUSIC
  Point Preferences -> Library at a folder and press Scan. Attune hands the work to
  analyzer\\AttuneAnalyzer.exe, which carries everything it needs -- no system Python
  and no system ffmpeg required. Newly analyzed tracks join the mixable pool after
  the next restart (the engine loads its pool once, at startup).
"""


def main():
    if not os.path.isdir(DIST):
        raise SystemExit(f"[package] {DIST} not found -- run build.py first.")
    if not os.path.exists(DB_SRC):
        raise SystemExit(f"[package] library DB not found: {DB_SRC}")

    dst_db = os.path.join(DIST, "mixer.db")
    print(f"[package] copying library DB ({os.path.getsize(DB_SRC)/1e6:.0f} MB) -> {dst_db}")
    shutil.copy2(DB_SRC, dst_db)

    with open(os.path.join(DIST, "README.txt"), "w", encoding="utf-8") as f:
        f.write(README)

    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _dn, fns in os.walk(DIST) for f in fns)
    print(f"[package] done. bundle at {DIST}  ({total/1e6:.0f} MB total)")
    print("[package] test it:  double-click Attune.exe  (or run it from a terminal to see logs)")


if __name__ == "__main__":
    main()
