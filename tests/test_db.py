"""Locks in db.py bug fixes:
  * connect() creates the full schema and stamps meta(schema_version,
    feature_dim) on a fresh DB;
  * connect() fails closed (SystemExit) on a schema_version mismatch instead
    of silently mixing incompatible feature dimensions;
  * features.src_mtime exists and is used by up_to_date_paths() to detect a
    changed source file (tracks.mtime advancing past features.src_mtime)
    so a re-imported/changed file is correctly re-analyzed instead of the
    old "stale vector kept forever" bug.
"""
from __future__ import annotations
import os
import time

import numpy as np
import pytest

import db as dbm
from features import FEATURE_DIM


def test_connect_creates_schema_and_stamps_meta(tmp_path):
    dbp = str(tmp_path / "fresh.db")
    conn = dbm.connect(dbp)
    try:
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"meta", "tracks", "features"} <= tables

        sv = conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        assert sv is not None
        assert sv[0] == str(dbm.SCHEMA_VERSION)

        fd = conn.execute("SELECT value FROM meta WHERE key='feature_dim'").fetchone()
        assert fd is not None
        assert fd[0] == str(FEATURE_DIM)
    finally:
        conn.close()


def test_features_table_has_src_mtime_column(tmp_path):
    dbp = str(tmp_path / "fresh.db")
    conn = dbm.connect(dbp)
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
        assert "src_mtime" in cols
    finally:
        conn.close()


def test_reconnect_with_matching_schema_succeeds(tmp_path):
    dbp = str(tmp_path / "fresh.db")
    dbm.connect(dbp).close()
    # a second connect() against the same, untouched DB must not raise
    conn2 = dbm.connect(dbp)
    conn2.close()


def test_reconnect_with_mismatched_schema_version_raises_systemexit(tmp_path):
    dbp = str(tmp_path / "fresh.db")
    conn = dbm.connect(dbp)
    conn.execute("UPDATE meta SET value=? WHERE key='schema_version'",
                 (str(dbm.SCHEMA_VERSION + 1),))
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        dbm.connect(dbp)


def test_reconnect_with_mismatched_feature_dim_raises_systemexit(tmp_path):
    dbp = str(tmp_path / "fresh.db")
    conn = dbm.connect(dbp)
    conn.execute("UPDATE meta SET value=? WHERE key='feature_dim'",
                 (str(FEATURE_DIM + 1),))
    conn.commit()
    conn.close()

    with pytest.raises(SystemExit):
        dbm.connect(dbp)


def _seed_one_track(conn, path, mtime, src_mtime, analyzed_at):
    dbm.upsert_track(conn, {
        "path": path, "artist": "A", "album": "Al", "title": "T", "genre": "",
        "year": 2000, "seconds": 200, "bytes": 12345, "mtime": mtime,
    })
    dbm.save_features(conn, path, np.zeros(FEATURE_DIM, dtype=np.float32),
                       120.0, analyzed_at, src_mtime=src_mtime)
    conn.commit()


def test_up_to_date_paths_detects_changed_mtime(tmp_path):
    dbp = str(tmp_path / "fresh.db")
    conn = dbm.connect(dbp)
    try:
        path = "/lib/song.mp3"
        now = int(time.time())
        _seed_one_track(conn, path, mtime=1000, src_mtime=1000, analyzed_at=now)

        assert path in dbm.up_to_date_paths(conn)

        # simulate a re-imported / changed source file: tracks.mtime advances
        # past features.src_mtime -> the row must drop out of up_to_date_paths
        conn.execute("UPDATE tracks SET mtime=? WHERE path=?", (2000, path))
        conn.commit()

        assert path not in dbm.up_to_date_paths(conn)
    finally:
        conn.close()


def test_up_to_date_paths_grandfathers_null_src_mtime(tmp_path):
    """Rows predating mtime-tracking (src_mtime IS NULL) must still count as
    up-to-date rather than being force-reanalyzed."""
    dbp = str(tmp_path / "fresh.db")
    conn = dbm.connect(dbp)
    try:
        path = "/lib/old_song.mp3"
        dbm.upsert_track(conn, {
            "path": path, "artist": "A", "album": "Al", "title": "T", "genre": "",
            "year": 2000, "seconds": 200, "bytes": 12345, "mtime": 5000,
        })
        dbm.save_features(conn, path, np.zeros(FEATURE_DIM, dtype=np.float32),
                           120.0, int(time.time()), src_mtime=None)
        conn.commit()

        assert path in dbm.up_to_date_paths(conn)
    finally:
        conn.close()
