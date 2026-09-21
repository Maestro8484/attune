"""Build a PRIVATE TEST BUNDLE of Attune. NOT a release, and never redistributable.

    python desktop/build.py                                        # first: dist/Attune/
    python desktop/package.py --i-know-this-contains-my-library    # then: fold in YOUR library

This folds a COPY OF YOUR PERSONAL LIBRARY DATABASE into the built app folder so the
exe is self-locating for your own testing. That database carries every file path, album
and track name in your collection. It must never go into an installer, a zip, a release,
or anywhere off this machine.

Because that hazard is one careless command away, this script refuses to run unless
--i-know-this-contains-my-library is passed, and desktop/build_installer.py and
tools/make_release.py refuse to package any folder containing a *.db file. Running the
release scripts in the obvious order after this one fails loudly instead of shipping
your library.

To make something you CAN give away, use tools/make_release.py against a build folder
that has never been through this script.

Produces, in the chosen build folder:
    Attune.exe            the app
    _internal/            its runtime (PyInstaller)
    analyzer/             AttuneAnalyzer.exe + its runtime - analyzing new audio
    mixer.db              a COPY of your analyzed library   <-- the private part
    README.txt            what it needs and what it cannot do

It still streams the actual audio files live from their stored paths (the music itself
is far too large to bundle), and it uses a live MusicIP Mixer only if one happens to be
running -- otherwise the built-in V2 engine.
"""
import argparse
import os
import shutil

HERE = os.path.dirname(os.path.abspath(__file__))
ATT = os.path.dirname(HERE)
ROOT = os.path.dirname(ATT)

DIST = os.path.join(ATT, "dist", "Attune")
DB_SRC = os.path.join(ROOT, "mixer-ng", "data", "mixer.db")

README = """Attune Studio - PRIVATE TEST BUNDLE (contains your library database)
===================================================================

This folder is NOT a release. It holds a copy of the packager's own library database
(mixer.db), which lists every music file path, album and track name on their machine.
Do not share, upload, zip or install it anywhere.

Double-click  Attune.exe  to run. A window appears in about 3 seconds; the library
finishes loading a couple of seconds later.

WHAT THIS BUNDLE INCLUDES
  - Attune.exe + its Python runtime (_internal\\)
  - analyzer\\AttuneAnalyzer.exe : analyzes new music (librosa + the CLAP encoder +
    ffmpeg/ffprobe, all bundled). Kept as a separate program so the app you launch
    every day stays small; Attune runs it for you when you scan.
  - mixer.db : the packager's analyzed library (CLAP + librosa vectors, metadata)

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

REFUSAL = """[package] REFUSING to run without --i-know-this-contains-my-library.

This script copies your personal library database into the app folder. That file lists
every music file path, album and track name on this machine. A folder it has touched can
never be released: build_installer.py and make_release.py will refuse to package it.

For a bundle you can give away:
    python tools/make_release.py --dist <build folder> --out <staging folder>

To make this private test bundle anyway, re-run with --i-know-this-contains-my-library.
"""


def main():
    ap = argparse.ArgumentParser(
        description="Fold YOUR PERSONAL library database into a built Attune folder, "
                    "for local testing only. The result must never be redistributed.")
    ap.add_argument("--i-know-this-contains-my-library", dest="confirmed",
                    action="store_true",
                    help="Required. Confirms you understand the output holds a copy of "
                         "your library database and is not a release artifact.")
    ap.add_argument("--dist", default=DIST,
                    help="Built app folder to fold the database into "
                         "(default: this repo's dist/Attune)")
    ap.add_argument("--db", default=DB_SRC, help="Library database to copy in")
    args = ap.parse_args()

    if not args.confirmed:
        raise SystemExit(REFUSAL)

    dist, db_src = args.dist, args.db
    if not os.path.isdir(dist):
        raise SystemExit(f"[package] {dist} not found -- run build.py first.")
    if not os.path.exists(db_src):
        raise SystemExit(f"[package] library DB not found: {db_src}")

    dst_db = os.path.join(dist, "mixer.db")
    print("[package] PRIVATE TEST BUNDLE -- this folder becomes non-redistributable.")
    print(f"[package] copying library DB ({os.path.getsize(db_src)/1e6:.0f} MB) -> {dst_db}")
    shutil.copy2(db_src, dst_db)

    with open(os.path.join(dist, "README.txt"), "w", encoding="utf-8") as f:
        f.write(README)

    total = sum(os.path.getsize(os.path.join(dp, f))
                for dp, _dn, fns in os.walk(dist) for f in fns)
    print(f"[package] done. PRIVATE test bundle at {dist}  ({total/1e6:.0f} MB total)")
    print("[package] test it:  double-click Attune.exe")
    print("[package] do NOT zip, install, upload or share this folder.")


if __name__ == "__main__":
    main()
