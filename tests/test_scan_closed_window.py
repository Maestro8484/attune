"""Four things a real rescan got wrong on 2026-11-09 (ISSUES.md rows 103 to 106).

Joe synced the file dates between his local disk and his NAS, edited some tags, and
pressed Scan. What happened, read from the scan log and the library afterwards:

  103  He closed the window. The analyzer kept running on every core, saving nothing,
       until he ended it from Task Manager.
  104  20,083 of 21,310 songs were queued for analysis because their file DATE had
       changed. Their audio had not.
  105  A second scan, pointed at the same music through the NAS drive letter, added
       21,362 songs: the library doubled.
  106  The log held 515 lines saying "error" while every song decoded.

Each check below fails on the code as it was that day.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time

import numpy as np
import pytest

import db as dbm
import scan
from features import FEATURE_DIM

HERE = os.path.dirname(os.path.abspath(__file__))
SCANJOB = os.path.join(os.path.dirname(HERE), "web", "scanjob.py")


def _load_scanjob():
    spec = importlib.util.spec_from_file_location("scanjob_under_test", SCANJOB)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _alive(pid):
    out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                         capture_output=True, text=True).stdout
    return str(pid) in out


# ---------------------------------------------------------------- 103, the window's side
@pytest.mark.skipif(os.name != "nt", reason="the job object is a Windows mechanism")
def test_a_stage_dies_with_the_process_that_started_it(tmp_path):
    """Stand in for the window with a small parent process that starts a child the way
    the scan job does and ties it. End the parent abruptly, as Task Manager would, and
    the child has to be gone within seconds without anybody telling it."""
    pidfile = tmp_path / "child.pid"
    parent_src = tmp_path / "parent.py"
    parent_src.write_text(
        "import importlib.util, subprocess, sys, time\n"
        f"spec = importlib.util.spec_from_file_location('sj', {SCANJOB!r})\n"
        "sj = importlib.util.module_from_spec(spec); spec.loader.exec_module(sj)\n"
        "p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "assert sj._tie_to_this_process(p), 'could not tie the child'\n"
        f"open({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(120)\n", encoding="utf-8")
    parent = subprocess.Popen([sys.executable, str(parent_src)])
    try:
        deadline = time.time() + 30
        while not pidfile.exists() and time.time() < deadline:
            assert parent.poll() is None, "the stand-in window exited early"
            time.sleep(0.1)
        time.sleep(0.2)
        child = int(pidfile.read_text())
        assert _alive(child)
        parent.kill()
        parent.wait(10)
        deadline = time.time() + 10
        while _alive(child) and time.time() < deadline:
            time.sleep(0.2)
        assert not _alive(child), "the stage outlived the process that started it"
    finally:
        if parent.poll() is None:
            parent.kill()
        if pidfile.exists():
            subprocess.run(["taskkill", "/F", "/PID", pidfile.read_text()],
                           capture_output=True)


# -------------------------------------------------------------- 103, the analyzer's side
def _library(tmp_path, n):
    conn = dbm.connect(str(tmp_path / "t.db"))
    for i in range(n):
        dbm.upsert_track(conn, {"path": f"X:\\none\\song{i:04d}.mp3", "mtime": 1, "bytes": 1,
                                "title": f"song{i}"})
    conn.commit()
    conn.close()
    return str(tmp_path / "t.db")


def test_the_queue_is_dropped_when_nobody_reads_the_output(tmp_path, monkeypatch):
    """300 songs queued, and the progress line fails the way it does once the window is
    gone. Before: every one of the 300 was still analyzed. Now: the few in flight."""
    db_path = _library(tmp_path, 300)
    started = []

    def slow(path, read_path=None):
        started.append(path)
        time.sleep(0.02)
        return (path, np.zeros(FEATURE_DIM, np.float32), 120.0, None, 0.0, False, None, 1.0)

    def dead_pipe(*a, **k):
        if a and " ok=" in str(a[0]):
            raise OSError(22, "Invalid argument")     # what print() raises on a closed pipe

    monkeypatch.setattr(scan, "_analyze_one", slow)
    monkeypatch.setattr(scan.feat, "decoder_available", lambda: True)
    monkeypatch.setattr(scan, "print", dead_pipe, raising=False)
    with pytest.raises(OSError):
        scan.analyze(db_path, workers=4)
    time.sleep(0.5)
    seen = len(started)
    assert seen < 60, f"{seen} of 300 queued songs were still analyzed after the output died"
    time.sleep(0.5)
    assert len(started) == seen, "songs were still being analyzed a second later"
    # and what was done before the stop was kept
    conn = dbm.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM features WHERE vec IS NOT NULL").fetchone()[0] >= 25


# ------------------------------------------------------------------------------- 104
def _song(path, tag, audio, tail=b"TAG" + b"\0" * 125):
    with open(path, "wb") as f:
        f.write(tag + audio + tail)


def test_the_signature_follows_the_audio_not_the_tags(tmp_path):
    audio = os.urandom(600_000)
    p = str(tmp_path / "a.mp3")
    _song(p, b"ID3" + b"\0" * 2000, audio)
    before = scan.audio_signature(p)
    _song(p, b"ID3" + b"genre and a longer comment " * 400, audio)   # tags rewritten, longer
    assert scan.audio_signature(p) == before
    os.utime(p, (1, 1))                                              # only the date
    assert scan.audio_signature(p) == before
    _song(p, b"ID3" + b"\0" * 2000, os.urandom(600_000))             # other audio
    assert scan.audio_signature(p) != before
    assert scan.audio_signature(str(tmp_path / "missing.mp3")) is None


def _analyzed(conn, path, src_mtime, now_mtime, sig):
    dbm.upsert_track(conn, {"path": path, "mtime": now_mtime, "bytes": 1, "title": "t"})
    dbm.save_features(conn, path, np.zeros(FEATURE_DIM, np.float32), 120.0, 1,
                      src_mtime=src_mtime, audio_sig=sig)


def test_a_new_date_on_the_same_audio_is_not_analyzed_again(tmp_path):
    same, changed, old = (str(tmp_path / n) for n in ("same.mp3", "changed.mp3", "old.mp3"))
    for p in (same, changed, old):
        _song(p, b"ID3" + b"\0" * 100, os.urandom(400_000))
    conn = dbm.connect(str(tmp_path / "t.db"))
    _analyzed(conn, same, 100, 200, scan.audio_signature(same))
    _analyzed(conn, changed, 100, 200, scan.audio_signature(changed))
    _analyzed(conn, old, 100, 100, None)        # analyzed before signatures existed
    conn.execute("CREATE TABLE loudness(path TEXT PRIMARY KEY, src_mtime REAL)")
    conn.execute("INSERT INTO loudness VALUES(?, 100)", (same,))
    conn.commit()
    _song(changed, b"ID3" + b"\0" * 100, os.urandom(400_000))        # really new audio

    settled = scan._settle_unchanged_audio(conn)

    assert settled == [same]
    done = dbm.up_to_date_paths(conn)
    assert same in done and changed not in done
    # the loudness row moved with it, so the loudness stage does not measure it again
    assert conn.execute("SELECT src_mtime FROM loudness WHERE path=?", (same,)).fetchone()[0] == 200
    # and the song from before signatures existed has one now
    assert conn.execute("SELECT audio_sig FROM features WHERE path=?",
                        (old,)).fetchone()[0] == scan.audio_signature(old)


# ------------------------------------------------------------------------------- 105
def _music_folder(root, albums=6, songs=5):
    for a in range(albums):
        d = root / "Albums" / f"Artist {a}" / f"Album {a}"
        d.mkdir(parents=True)
        for s in range(songs):
            (d / f"{s:02d} - Song.mp3").write_bytes(b"\0" * 64)


def test_the_same_music_under_a_second_folder_is_refused(tmp_path, capsys):
    local, nas, other = tmp_path / "L" / "Music", tmp_path / "M" / "Music", tmp_path / "N"
    _music_folder(local)
    _music_folder(nas)
    db_path = str(tmp_path / "t.db")
    scan.import_folder(db_path, str(local))
    conn = dbm.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 30

    with pytest.raises(SystemExit) as e:
        scan.import_folder(db_path, str(nas))
    assert e.value.code == 3
    said = capsys.readouterr().out
    assert "NOT imported:" in said and str(local) in said
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 30

    # scanning the first folder again is an ordinary rescan
    scan.import_folder(db_path, str(local))
    # different music in a second folder is an ordinary second folder
    for i in range(25):
        d = other / f"Someone {i}" / f"Record {i}"
        d.mkdir(parents=True)
        (d / "01 - Tune.mp3").write_bytes(b"\0" * 64)
    scan.import_folder(db_path, str(other))
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 55
    # and the flag is the way to list a second copy on purpose
    scan.import_folder(db_path, str(nas), allow_second_copy=True)
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 85


def test_the_window_shows_the_refusal_as_written():
    sj = _load_scanjob()
    assert sj._TWIN_MARK in ("NOT imported: the songs in N:\\Music are already in "
                             "your library from D:\\Music.")


# ------------------------------------------------------------------------------- 106
CHATTER = [
    r"[C:\vcpkg\buildtrees\mpg123\src\-66150af195.clean\src\libmpg123\id3.c:process_comment():570] error: Invalid frame size of 3 (too small for anything).",
    r"[C:\vcpkg\buildtrees\mpg123\src\-66150af195.clean\src\libmpg123\layer3.c:INT123_do_layer3():1948] error: dequantization failed!",
    "Note: Illegal Audio-MPEG-Header 0x00000000 at offset 5803363.",
    "Note: Trying to resync...",
    "Note: Skipped 1024 bytes in input.",
    "Note: Hit end of (available) data during resync.",
    "Warning: Xing stream size off by more than 1%, fuzzy seeking may be even more fuzzy than by design!",
    r"C:\Programs\Attune\analyzer\_internal\attune\src\features.py:377: UserWarning: PySoundFile failed. Trying audioread instead.",
    "  y, _ = librosa.load(path, sr=sr, mono=True)",
    r"librosa\core\audio.py:184: FutureWarning: librosa.core.audio.__audioread_load",
    "\tDeprecated as of librosa version 0.10.0.",
    "\tIt will be removed in librosa version 1.0.",
    "  y, sr_native = __audioread_load(path, offset, duration, dtype)",
]
KEPT = [
    "  800/40945 ok=800 err=0 1.39/s eta=479.8m last=3.9s",
    "21310 candidates, 1227 already done, 20083 to analyze, workers=4",
    "retrying 2 previously failed tracks: recorded as too short before anything asked a second decoder",
    "DONE ok=10 err=1 in 0.2m",
    "NOT imported: the songs in M:\\Music are already in your library from L:\\Music.",
    "note: could not clear CLAP rows (locked); run the embed stage",
    "Traceback (most recent call last):",
    "OSError: [Errno 22] Invalid argument",
]


def test_decoder_chatter_is_left_out_and_real_lines_are_kept():
    sj = _load_scanjob()
    for line in CHATTER:
        assert sj._CHATTER_RE.search(line), f"still logged as if it were news: {line}"
    for line in KEPT:
        assert not sj._CHATTER_RE.search(line), f"a real line would be hidden: {line}"
