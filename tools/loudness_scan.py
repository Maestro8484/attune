#!/usr/bin/env python3
r"""loudness_scan.py - measure every song's loudness the broadcast way and store it beside the library.

Three numbers per song, the EBU R128 measure (ITU-R BS.1770) that ReplayGain 2.0 and every
streaming service normalize by:

  lufs       integrated loudness, LUFS: how loud the whole song sounds, gated so silence
             and quiet passages do not drag it down
  lra        loudness range, LU: how far the loud parts sit from the quiet parts
  true_peak  the highest sample peak after 4x oversampling, dBTP (ffmpeg prints it as dBFS)

They go in an add-on table, `loudness`, keyed by path, made with CREATE TABLE IF NOT EXISTS the
way web/libverify.py makes `filestate` and web/audioinfo.py makes `audioinfo`: SCHEMA_VERSION
is untouched, a library without the table loads exactly as before, and a features rebuild never
wipes it. NOTHING IS WRITTEN INTO ANY MUSIC FILE. That is the difference from rsgain and
loudgain, which measure the same thing and then write ReplayGain tags into the files.

PRIOR ART, stated per CLAUDE.md section 7: the measurement is ffmpeg's own ebur128 filter, the
same one ffmpeg-normalize and loudcheck read. ffmpeg is found the one way the analyzer finds it
(src/features.py ffmpeg_state, shutil.which), and an MP3 that ffmpeg misreads as a RIFF file is
asked a second time as MP3, the same second ask src/features.py _ffmpeg_decode makes.
pyloudnorm (MIT) would do it in pure Python but needs the whole song decoded into memory first;
ffmpeg decodes and measures in one pass at about 300x real time.

A song counts as measured when its row's src_mtime equals the library's own mtime for it
(tracks.mtime), so a rescan that changes a file makes its row stale and the next run measures it
again. Rows that came back with an error are not asked again unless --retry-errors is given.

Before the first row is written in a run, the library is copied beside itself by SQLite's own
backup (src/embed_onnx.py's reason: a plain file copy misses what sits in the WAL side file), and
the copy is checked: integrity, the same song count, and its size on disk equal to its own page
count times page size. One copy a day is enough; a second run the same day reuses it.

    python tools/loudness_scan.py --db <library> --dry-run          # measure 20, write nothing
    python tools/loudness_scan.py --db <library> --workers 6        # the whole library

Resumable: stop it any time; the next run carries on from the rows already written.
Exit codes: 0 done, 1 refused (the reason is printed), 2 the backup did not verify.
"""
from __future__ import annotations

import argparse
import os
import random
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))
import features  # noqa: E402  (the one ffmpeg lookup)

BATCH = 200
SCHEMA = """CREATE TABLE IF NOT EXISTS loudness (
    path        TEXT PRIMARY KEY,
    lufs        REAL,
    lra         REAL,
    true_peak   REAL,
    src_mtime   REAL,
    measured_at REAL,
    error       TEXT
)"""

_NUM = r"(-?inf|nan|-?\d+(?:\.\d+)?)"
_I_RE = re.compile(r"Integrated loudness:\s*\n\s*I:\s*" + _NUM + r"\s*LUFS")
_LRA_RE = re.compile(r"Loudness range:\s*\n\s*LRA:\s*" + _NUM + r"\s*LU")
_TP_RE = re.compile(r"True peak:\s*\n\s*Peak:\s*" + _NUM + r"\s*dBFS")


def ensure_schema(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute(SCHEMA)
        con.commit()
    finally:
        con.close()


def _num(m):
    if not m:
        return None
    v = float(m.group(1))
    return v if v == v and abs(v) != float("inf") else None


def parse_summary(text):
    """ffmpeg's ebur128 summary block -> (lufs, lra, true_peak), or None when there is no
    summary at all (ffmpeg could not open or decode the file). The LAST summary is read: the
    filter prints one at the end of the run, after any per-frame lines. A silent song has a
    summary whose integrated loudness is not a finite number; that one comes back as None in
    its slot, with the other two as printed."""
    at = text.rfind("Summary:")
    if at < 0:
        return None
    tail = text[at:]
    i = _I_RE.search(tail)
    if not i:
        return None
    return _num(i), _num(_LRA_RE.search(tail)), _num(_TP_RE.search(tail))


def _run(exe, path, as_format=()):
    cmd = [exe, "-nostdin", "-nostats", "-hide_banner", *as_format, "-i", path,
           "-vn", "-sn", "-dn",                       # cover art is a video stream; skip it
           "-af", "ebur128=peak=true:framelog=verbose",   # per-frame lines below info: summary only
           "-f", "null", "-"]
    try:
        r = subprocess.run(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                           stderr=subprocess.PIPE, timeout=features.FFMPEG_TIMEOUT_S,
                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except subprocess.TimeoutExpired:
        return None, f"ffmpeg took over {features.FFMPEG_TIMEOUT_S} s"
    except Exception as e:
        return None, f"ffmpeg did not run: {type(e).__name__}"
    return r.stderr.decode("utf-8", "replace"), None


def measure(exe, path):
    """One song -> (lufs, lra, true_peak, error). error is None on success."""
    if not os.path.exists(path):
        return None, None, None, features.TRANSIENT_ERRORS[0]
    text, err = _run(exe, path)
    got = parse_summary(text) if text is not None else None
    if got is None and err is None and path.lower().endswith(".mp3"):
        text, err = _run(exe, path, ("-f", "mp3"))
        got = parse_summary(text) if text is not None else None
    if got is None:
        if err is None:
            last = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
            err = "ffmpeg could not measure it" + (f": {last[-1][:160]}" if last else "")
        return None, None, None, err
    lufs, lra, tp = got
    return lufs, lra, tp, (None if lufs is not None else "silent: no integrated loudness")


def todo(db_path, retry_errors=False):
    """[(path, mtime)] of library songs without a fresh measurement, in library order."""
    con = sqlite3.connect(db_path, timeout=30)
    try:
        have = {}
        if con.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='loudness'").fetchone():
            have = {p: (m, e) for p, m, e in con.execute("SELECT path, src_mtime, error FROM loudness")}
        out = []
        for p, mt in con.execute("SELECT path, mtime FROM tracks"):
            row = have.get(p)
            if row is None or row[0] != mt or (retry_errors and row[1] is not None):
                out.append((p, mt))
        return out
    finally:
        con.close()


def write_batch(db_path, rows):
    """rows: [(path, lufs, lra, true_peak, src_mtime, measured_at, error)]. One transaction,
    three attempts (web/audioinfo.py's retry, for the same transient WAL lock)."""
    for attempt in (1, 2, 3):
        con = sqlite3.connect(db_path, timeout=30)
        try:
            con.executemany(
                """INSERT INTO loudness(path, lufs, lra, true_peak, src_mtime, measured_at, error)
                   VALUES(?,?,?,?,?,?,?)
                   ON CONFLICT(path) DO UPDATE SET lufs=excluded.lufs, lra=excluded.lra,
                     true_peak=excluded.true_peak, src_mtime=excluded.src_mtime,
                     measured_at=excluded.measured_at, error=excluded.error""", rows)
            con.commit()
            return
        except sqlite3.OperationalError:
            if attempt == 3:
                raise
            time.sleep(0.5 * attempt)
        finally:
            con.close()


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
    exe = shutil.which("ffmpeg")
    work = todo(a.db, a.retry_errors)
    if a.dry_run:
        work = random.sample(work, min(20, len(work)))
    elif a.limit:
        work = work[:a.limit]
    print(f"loudness: {len(work):,} song(s) to measure, {a.workers} at once, ffmpeg {exe}", flush=True)
    if not work:
        return 0
    if not a.dry_run:
        if backup_library(a.db) is None:
            print("loudness: the backup did not check out; nothing was written")
            return 2
        ensure_schema(a.db)

    t0 = time.time()
    done = failed = 0
    pending = []
    with ThreadPoolExecutor(max(1, a.workers)) as ex:
        for (p, mt), (lufs, lra, tp, err) in zip(work, ex.map(lambda w: measure(exe, w[0]), work)):
            done += 1
            failed += err is not None
            if a.dry_run:
                print(f"  {lufs if lufs is None else round(lufs, 1):>6} LUFS  {lra if lra is None else round(lra, 1):>5} LU  "
                      f"{tp if tp is None else round(tp, 1):>5} dBTP  {err or ''}", flush=True)
                continue
            pending.append((p, lufs, lra, tp, mt, time.time(), err))
            if len(pending) >= BATCH:
                write_batch(a.db, pending)
                pending = []
                el = time.time() - t0
                print(f"loudness: {done:,} of {len(work):,}, {failed} error(s), {done / el:.2f} songs/s, "
                      f"about {(len(work) - done) / (done / el) / 60:.0f} min left", flush=True)
    if pending:
        write_batch(a.db, pending)
    el = time.time() - t0
    print(f"loudness: {'measured' if a.dry_run else 'wrote'} {done:,} song(s), {failed} error(s), "
          f"{el:.0f} s, {el / max(done, 1):.2f} s a song wall clock{' (dry run, nothing written)' if a.dry_run else ''}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
