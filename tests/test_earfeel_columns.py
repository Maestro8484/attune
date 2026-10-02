"""The five Earfeel scores as optional song-list columns (2026-10-01).

Locks in:
  * a row carries the scores as 0 to 100 when the engine has them, and None when it has
    not (the shipped state: no Earfeel file, so no column is offered at all);
  * scores that do not line up with the pool are dropped rather than shown against the
    wrong songs;
  * an Earfeel column sorts highest first, like BPM, and an unknown name falls back;
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
NAMES = ["pulse", "glow", "heat", "voice", "grain"]
LABELS = ["Pulse", "Glow", "Heat", "Voice", "Grain"]


@pytest.fixture(scope="module")
def studio():
    web_dir = os.path.dirname(STUDIO_PY)
    if web_dir not in sys.path:
        sys.path.insert(0, web_dir)
    spec = importlib.util.spec_from_file_location("attune_studio_earfeel_test", STUDIO_PY)
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
    earfeel = np.array([[0.1, 0.5, 0.9, 0.0, 1.0],
                     [0.8, 0.2, 0.3, 0.4, 0.5],
                     [0.45, 0.6, 0.6, 0.7, 0.0]], np.float32)
    return dbp, paths, meta, earfeel


def test_rows_carry_the_scores_as_0_to_100(studio, library):
    dbp, paths, meta, earfeel = library
    lib = studio.LibraryIndex(dbp, paths, meta, earfeel, NAMES, LABELS)
    assert lib.row(0, with_trackno=False)["earfeel"] == [10, 50, 90, 0, 100]
    assert lib.row(2, with_trackno=False)["earfeel"] == [45, 60, 60, 70, 0]
    assert lib.earfeel_labels == LABELS


def test_no_earfeel_file_no_scores(studio, library):
    dbp, paths, meta, _earfeel = library
    lib = studio.LibraryIndex(dbp, paths, meta)
    assert lib.row(0, with_trackno=False)["earfeel"] is None
    assert lib.earfeel_names == [] and lib.earfeel_sort("earfeel:pulse") is None


def test_scores_that_do_not_fit_the_pool_are_dropped(studio, library):
    dbp, paths, meta, earfeel = library
    lib = studio.LibraryIndex(dbp, paths, meta, earfeel[:2], NAMES, LABELS)
    assert lib.earfeel is None and lib.earfeel_names == []


def test_an_earfeel_column_sorts_highest_first(studio, library):
    dbp, paths, meta, earfeel = library
    lib = studio.LibraryIndex(dbp, paths, meta, earfeel, NAMES, LABELS)
    key = lib.earfeel_sort("earfeel:pulse")
    assert sorted(range(3), key=lambda i: key(lib, i)) == [1, 2, 0]
    assert lib.earfeel_sort("earfeel:nonsense") is None and lib.earfeel_sort("bpm") is None


def test_the_page_offers_them_only_from_stats_and_never_by_default():
    js = open(STUDIO_JS, encoding="utf-8").read()
    default = re.search(r"store\.get\('cols',\s*\[([^\]]*)\]", js).group(1)
    assert "earfeel" not in default
    # at boot and again after a library reload, which may add, drop or reorder the scores
    assert len(re.findall(r"addEarfeelCols\(S\.stats\.earfeel\)", js)) >= 2
    static_cols = js[js.index("const COLS = ["):js.index("];", js.index("const COLS = ["))]
    assert "earfeel" not in static_cols
