#!/usr/bin/env python3
r"""loudness_scan.py - measure a whole library's loudness by hand, with a backup first.

The measurement itself is src/loudness.py: integrated loudness (LUFS), loudness range and true
peak per song from ffmpeg's ebur128 filter (EBU R128), stored in the add-on `loudness` table,
nothing written into any music file. The scan runs the same thing as its fourth stage on new and
changed songs (web/scanjob.py); this tool is for a library that has never been measured, or a
re-measure, run deliberately.

What it adds to the stage: before the first row is written, the library is copied beside itself
by SQLite's own backup (src/embed_onnx.py's reason: a plain file copy misses what sits in the WAL
side file), and the copy is checked: integrity, the same song count, and its size on disk equal
to its own page count times page size. One copy a day is enough; a second run the same day reuses
it. Also a dry run, a retry of songs that came back with an error, and a limit.

    python tools/loudness_scan.py --db <library> --dry-run          # measure 20, write nothing
    python tools/loudness_scan.py --db <library> --workers 6        # the whole library

Resumable: stop it any time; the next run carries on from the rows already written.
Exit codes: 0 done, 1 refused (the reason is printed), 2 the backup did not verify.
"""
from __future__ import annotations

import argparse
import os
import random
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import features  # noqa: E402
import loudness  # noqa: E402


def backup_library(db_path):
    """Today's checked copy of the library beside it, made once a day. Returns its path, or
    None when the copy does not check out."""
    stem, ext = os.path.splitext(db_path)
    dest = f"{stem}.backup-{time.strftime('%Y%m%d')}-before-loudness{ext}"
    if not os.path.exists(dest):
        src = sqlite3.connect(db_path, timeout=30)
        dst = sqlite3.connect(dest)
        try:
            src.backup(dst)
            dst.commit()
        finally:
            dst.close()
            src.close()
    src = sqlite3.connect(db_path, timeout=30)
    dst = sqlite3.connect(dest)
    try:
        ok = dst.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        ok = ok and (src.execute("SELECT count(*) FROM tracks").fetchone()
                     == dst.execute("SELECT count(*) FROM tracks").fetchone())
        pages = dst.execute("PRAGMA page_count").fetchone()[0] * dst.execute("PRAGMA page_size").fetchone()[0]
    finally:
        dst.close()
        src.close()
    size = os.path.getsize(dest)
    print(f"loudness: backup {dest}: {size:,} bytes on disk, {pages:,} by its own page count, "
          f"{'checked' if ok and size == pages else 'DID NOT CHECK OUT'}", flush=True)
    return dest if ok and size == pages else None


def main(argv=None):
    ap = argparse.ArgumentParser(description="Measure every song's loudness (EBU R128) into the library's loudness table.")
    ap.add_argument("--db", required=True, help="the library database")
    ap.add_argument("--workers", type=int, default=4, help="ffmpeg runs at once (default 4)")
    ap.add_argument("--dry-run", action="store_true", help="measure 20 random songs, write nothing")
    ap.add_argument("--retry-errors", action="store_true", help="ask again for songs that came back with an error")
    ap.add_argument("--limit", type=int, default=0, help="stop after this many songs (0 = all)")
    a = ap.parse_args(argv)

    if not os.path.isfile(a.db):
        print(f"loudness: no library at {a.db}")
        return 1
    if features.ffmpeg_state() != "ok":
        print(f"loudness: REFUSED - no working ffmpeg ({features.ffmpeg_state()}); {features._decoder_hint()}")
        return 1
    work = loudness.todo(a.db, a.retry_errors)
    if a.dry_run:
        work = random.sample(work, min(20, len(work)))
    elif a.limit:
        work = work[:a.limit]
    print(f"loudness: {len(work):,} song(s) to measure, {a.workers} at once, ffmpeg {shutil.which('ffmpeg')}", flush=True)
    if not work:
        return 0
    if not a.dry_run and backup_library(a.db) is None:
        print("loudness: the backup did not check out; nothing was written")
        return 2
    done, failed, el = loudness.run(a.db, work, a.workers, write=not a.dry_run,
                                    say=lambda s: print(s, flush=True))
    print(f"loudness: {'measured' if a.dry_run else 'wrote'} {done:,} song(s), {failed} error(s), "
          f"{el:.0f} s, {el / max(done, 1):.2f} s a song wall clock{' (dry run, nothing written)' if a.dry_run else ''}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
