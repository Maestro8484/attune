"""The five feel scores as optional song-list columns (2026-10-01).

Locks in:
  * a row carries the scores as 0 to 100 when the engine has them, and None when it has
    not (the shipped state: no feel file, so no column is offered at all);
  * scores that do not line up with the pool are dropped rather than shown against the
    wrong songs;
  * a feel column sorts highest first, like BPM, and an unknown name falls back;
  * the page adds the columns only from /api/lib/stats and never turns them on by
    default, so nobody's saved layout changes.

The database is built from scratch in tmp_path. Nothing here reads the production library.
"""
from __future__ import annotations
import importlib.util
import os
import re
import sys
import time

import numpy as np
import pytest

import db as dbm

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
STUDIO_PY = os.path.join(ROOT, "web", "studio.py")
STUDIO_JS = os.path.join(ROOT, "web", "static", "studio.js")
NAMES = ["danceability", "valence", "arousal", "instrumentalness", "acousticness"]
LABELS = ["Danceable", "Happy", "Intense", "Instrumental", "Acoustic"]


@pytest.fixture(scope="module")
def studio():
    web_dir = os.path.dirname(STUDIO_PY)
    if web_dir not in sys.path:
        sys.path.insert(0, web_dir)
    spec = importlib.util.spec_from_file_location("attune_studio_feel_test", STUDIO_PY)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def library(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("feelcols"))
    dbp = os.path.join(d, "lib.db")
    conn = dbm.connect(dbp)
    now = int(time.time())
    paths = [os.path.join(d, f"t{i}.mp3") for i in range(3)]
    for i, p in enumerate(paths):
        conn.execute("INSERT INTO tracks(path, artist, album, title, genre, year, seconds, bytes, mtime) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (p, f"A{i}", "Al", f"T{i}", "Rock", 2000, 200, 1, now))
    conn.commit()
    conn.close()
    meta = {p: {"artist": f"A{i}", "album": "Al", "title": f"T{i}", "year": 2000}
            for i, p in enumerate(paths)}
    feel = np.array([[0.1, 0.5, 0.9, 0.0, 1.0],
                     [0.8, 0.2, 0.3, 0.4, 0.5],
                     [0.45, 0.6, 0.6, 0.7, 0.0]], np.float32)
    return dbp, paths, meta, feel


def test_rows_carry_the_scores_as_0_to_100(studio, library):
    dbp, paths, meta, feel = library
    lib = studio.LibraryIndex(dbp, paths, meta, feel, NAMES, LABELS)
    assert lib.row(0, with_trackno=False)["feel"] == [10, 50, 90, 0, 100]
    assert lib.row(2, with_trackno=False)["feel"] == [45, 60, 60, 70, 0]
    assert lib.feel_labels == LABELS


def test_no_feel_file_no_scores(studio, library):
    dbp, paths, meta, _feel = library
    lib = studio.LibraryIndex(dbp, paths, meta)
    assert lib.row(0, with_trackno=False)["feel"] is None
    assert lib.feel_names == [] and lib.feel_sort("feel:danceability") is None


def test_scores_that_do_not_fit_the_pool_are_dropped(studio, library):
    dbp, paths, meta, feel = library
    lib = studio.LibraryIndex(dbp, paths, meta, feel[:2], NAMES, LABELS)
    assert lib.feel is None and lib.feel_names == []


def test_a_feel_column_sorts_highest_first(studio, library):
    dbp, paths, meta, feel = library
    lib = studio.LibraryIndex(dbp, paths, meta, feel, NAMES, LABELS)
    key = lib.feel_sort("feel:danceability")
    assert sorted(range(3), key=lambda i: key(lib, i)) == [1, 2, 0]
    assert lib.feel_sort("feel:nonsense") is None and lib.feel_sort("bpm") is None


def test_the_page_offers_them_only_from_stats_and_never_by_default():
    js = open(STUDIO_JS, encoding="utf-8").read()
    default = re.search(r"store\.get\('cols',\s*\[([^\]]*)\]", js).group(1)
    assert "feel" not in default
    # at boot and again after a library reload, which may add, drop or reorder the scores
    assert len(re.findall(r"addFeelCols\(S\.stats\.feel\)", js)) >= 2
    static_cols = js[js.index("const COLS = ["):js.index("];", js.index("const COLS = ["))]
    assert "feel" not in static_cols
