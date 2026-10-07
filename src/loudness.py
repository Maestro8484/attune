#!/usr/bin/env python3
r"""loudness.py - each song's loudness, measured the broadcast way, as a stage of the scan.

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
(features.ffmpeg_state, shutil.which), and an MP3 that ffmpeg misreads as a RIFF file is asked a
second time as MP3, the same second ask features._ffmpeg_decode makes. pyloudnorm (MIT) would do
it in pure Python but needs the whole song decoded into memory first; ffmpeg decodes and measures
in one pass at about 300x real time.

A song counts as measured when its row's src_mtime equals the library's own mtime for it
(tracks.mtime), so a rescan that changes a file makes its row stale and the next run measures it
again. Rows that came back with an error are not asked again unless retry_errors is given.

The scan runs this as its fourth stage, after the fingerprints (web/scanjob.py), on the songs
without a fresh row: the new and the changed. Stopping it loses nothing; rows are committed in
batches and the next scan carries on. tools/loudness_scan.py is the same measurement run by hand
over a whole library, with a backup first.

    python src/loudness.py --db <library>
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
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


def run(db_path, work, workers=4, write=True, say=print):
    """Measure `work` ([(path, mtime)]) `workers` at once and, when `write`, store each batch
    as it completes. Returns (done, failed, seconds). `say` gets a progress line per batch;
    when not writing it gets one line per song instead (the dry run's report)."""
    exe = shutil.which("ffmpeg")
    if write and work:
        ensure_schema(db_path)
    t0 = time.time()
    done = failed = 0
    pending = []
    with ThreadPoolExecutor(max(1, workers)) as ex:
        for (p, mt), (lufs, lra, tp, err) in zip(work, ex.map(lambda w: measure(exe, w[0]), work)):
            done += 1
            failed += err is not None
            if not write:
                say(f"  {'-' if lufs is None else round(lufs, 1):>6} LUFS  {'-' if lra is None else round(lra, 1):>5} LU  "
                    f"{'-' if tp is None else round(tp, 1):>5} dBTP  {err or ''}")
                continue
            pending.append((p, lufs, lra, tp, mt, time.time(), err))
            if len(pending) >= BATCH:
                write_batch(db_path, pending)
                pending = []
                el = time.time() - t0
                say(f"loudness: {done:,} of {len(work):,}, {failed} error(s), {done / el:.2f} songs/s, "
                    f"about {(len(work) - done) / (done / el) / 60:.0f} min left")
    if pending:
        write_batch(db_path, pending)
    return done, failed, time.time() - t0


def main(argv=None):
    """The scan's loudness stage: measure the songs without a fresh row. Exits 0 when there
    is no working ffmpeg, saying so, because the songs are already mixable without it and a
    scan must not read as failed for a measurement it could not take."""
    ap = argparse.ArgumentParser(description="Measure the loudness (EBU R128) of library songs not yet measured.")
    ap.add_argument("--db", required=True, help="the library database")
    ap.add_argument("--workers", type=int, default=4, help="ffmpeg runs at once (default 4)")
    a = ap.parse_args(argv)
    if not os.path.isfile(a.db):
        print(f"loudness: no library at {a.db}", flush=True)
        return 1
    # A user's own ffmpeg in %APPDATA%\Attune\bin is found on every route, as the analyze
    # stage finds it (the ML-python route never passes through desktop/worker_entry.py).
    import scan
    scan.add_user_bin_to_path()
    state = features.ffmpeg_state()
    if state != "ok":
        print(f"loudness: skipped, no working ffmpeg ({state}); {features._decoder_hint()}", flush=True)
        return 0
    work = todo(a.db)
    print(f"loudness: {len(work):,} song(s) to measure", flush=True)
    if not work:
        return 0
    done, failed, el = run(a.db, work, a.workers, say=lambda s: print(s, flush=True))
    print(f"loudness: measured {done:,} song(s), {failed} error(s), {el:.0f} s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
