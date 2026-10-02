#!/usr/bin/env python3
"""refingerprint.py - move a library made with the earlier model file over to this build's.

Every Attune build before 2026-10-02 fingerprinted songs with the Hugging Face release of the
fingerprint model, whose weights were never trained. This build carries the model's trained
weights (src/embed_onnx.py, CLAP_WEIGHTS). The two kinds of fingerprint are unrelated numbers,
so the fingerprint stage refuses to add new songs to a library of the earlier kind. This tool is
the way across: one deliberate action that

  1. copies the library beside itself and checks the copy byte for byte,
  2. clears every sound fingerprint in the library and records which weights the new ones come
     from (both in one transaction), and
  3. runs the fingerprint stage over every song.

Nothing else is touched: no tags, no ratings, no playlists, no sound descriptors, no music file.
It takes about two seconds a song on an ordinary processor, so hours for a large library; it can
be stopped, and running the ordinary fingerprint stage afterwards (src/embed_onnx.py --db ...)
picks up where it stopped. Until it finishes, songs without a fingerprint stay out of mixes.
Close Attune first. To undo: close Attune and copy the backup over the library.

    python tools/refingerprint.py --db <library>          # say what would happen, change nothing
    python tools/refingerprint.py --db <library> --yes    # do it

Exit codes: 0 done or nothing to do, 1 refused (the reason is printed), 2 the backup did not verify.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import embed_onnx  # noqa: E402


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def state(db_path):
    """(songs, fingerprints, what the library says made them), read-only."""
    conn = sqlite3.connect(Path(os.path.abspath(db_path)).as_uri() + "?mode=ro", uri=True, timeout=30)
    try:
        songs = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        try:
            prints = conn.execute("SELECT COUNT(*) FROM clap WHERE vec IS NOT NULL").fetchone()[0]
        except sqlite3.OperationalError:
            prints = 0
        theirs, _has = embed_onnx._library_weights(conn)
    finally:
        conn.close()
    return songs, prints, theirs


def backup(db_path):
    """A byte-for-byte copy beside the library, verified. Returns its path, or None."""
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()
    stem, ext = os.path.splitext(db_path)
    dest = f"{stem}.backup-{time.strftime('%Y%m%d-%H%M%S')}-before-refingerprint{ext}"
    shutil.copyfile(db_path, dest)
    if os.path.getsize(dest) != os.path.getsize(db_path) or _sha256(dest) != _sha256(db_path):
        return None
    return dest


def clear_and_mark(db_path, weights=embed_onnx.CLAP_WEIGHTS):
    """Clear every fingerprint row and record the new kind, in one transaction."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute("BEGIN IMMEDIATE")
        n = conn.execute("DELETE FROM clap").rowcount
        conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('clap_weights', ?)", (weights,))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return n


def main(argv=None):
    ap = argparse.ArgumentParser(description="Re-fingerprint a library made with the earlier model file.")
    ap.add_argument("--db", required=True, help="the library database")
    ap.add_argument("--yes", action="store_true", help="do it; without this nothing is changed")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--onnx", default=embed_onnx.ONNX_PATH, help="model file (default: the bundled one)")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.db):
        print(f"refingerprint: no library at {a.db}")
        return 1
    songs, prints, theirs = state(a.db)
    print(f"refingerprint: {a.db}: {songs:,} songs, {prints:,} fingerprints, made with "
          f"{theirs or 'the original release (the library does not say otherwise)'}")

    # the model first: nothing is cleared unless the file that will make the new ones is right
    _librosa, onnxruntime = embed_onnx._import_stack()
    sess = embed_onnx.make_session(onnxruntime, a.onnx)
    weights = embed_onnx._check_model_weights(sess, a.onnx)
    del sess
    if weights is None:
        print("refingerprint: REFUSED - that model file carries the original release's untrained "
              "weights. Nothing was changed.")
        return 1
    if theirs == weights:
        print(f"refingerprint: this library already holds {weights} fingerprints. Nothing to do; "
              f"the ordinary fingerprint stage adds new songs to it.")
        return 0

    hours = songs * 2.0 / 3600
    print(f"refingerprint: would clear {prints:,} fingerprints, mark the library as {weights}, and "
          f"fingerprint {songs:,} songs again (roughly {hours:.1f} hours at two seconds a song).")
    if not a.yes:
        print("refingerprint: nothing was changed (no --yes).")
        return 0

    dest = backup(a.db)
    if dest is None:
        print("refingerprint: FAILED - the backup is not byte for byte the library. Nothing was changed.")
        return 2
    print(f"refingerprint: backup verified: {dest} ({os.path.getsize(dest):,} bytes)")
    n = clear_and_mark(a.db, weights)
    print(f"refingerprint: cleared {n:,} fingerprint rows; the library now says {weights}")
    t0 = time.time()
    embed_onnx.embed(a.db, workers=a.workers, batch=a.batch, onnx_path=a.onnx)
    songs, prints, theirs = state(a.db)
    took = time.time() - t0
    print(f"refingerprint: done in {took / 60:.1f} minutes ({took / max(songs, 1):.2f} s a song): "
          f"{prints:,} of {songs:,} songs carry a {theirs} fingerprint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
