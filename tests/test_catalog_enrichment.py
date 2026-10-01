"""Catalog enrichment (2026-10-01): recording ids, original years, styles and artist similarity
imported from an outside tagger (src/enrich.py) and read by the V2 engine (hybrid._load_catalog).

Locks in:
  1. no tables, no change: a library that never imported anything scores and mixes exactly as before;
  2. "covers" is a theme, not a genre: it never counts in the genre overlap;
  3. STYLE joins the genre tags, so a shared style earns partial credit;
  4. the original release year replaces the file's year in the era term;
  5. one recording once: two files of the same recording never both appear, nor the seed's twin;
  6. the artist weight is off by default, and explain() still adds up to _score() when it is on.

Synthetic library: twelve tracks, CLAP vectors close enough that every track is a candidate.
"""
from __future__ import annotations
import csv
import os
import sqlite3
import sys
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import db as dbm                     # noqa: E402
from features import FEATURE_DIM     # noqa: E402
import hybrid                        # noqa: E402
import enrich                        # noqa: E402

N = 12
GENRES = ["Punk; Covers", "Punk", "Punk", "Pop; Covers", "Pop", "Pop",
          "Rock", "Rock", "Rock", "Electronic", "Electronic", "Electronic"]


def build_db(dirpath):
    dbp = os.path.join(dirpath, "lib.db")
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(7)
    centre = rng.normal(size=512)
    now = int(time.time())
    paths = []
    for i in range(N):
        p = os.path.join(dirpath, f"t{i:02d}.mp3")
        paths.append(p)
        v = centre + 0.3 * rng.normal(size=512)
        v = v / np.linalg.norm(v)
        conn.execute("INSERT INTO tracks(path, artist, album, title, genre, year, seconds, bytes, mtime) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (p, f"Artist {i}", "Album", f"Title {i}", GENRES[i], 2005, 200, 1, now))
        conn.execute("INSERT INTO features(path, vec, dim, error, src_mtime, tempo) VALUES(?,?,?,?,?,?)",
                     (p, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(), FEATURE_DIM, None, now, 120.0))
        conn.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)", (p, v.astype(np.float32).tobytes(), 512))
    conn.commit()
    conn.close()
    return dbp, paths


def write_csvs(dirpath, paths):
    ids = os.path.join(dirpath, "ids.csv")
    with open(ids, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "recording_mbid", "artist_mbids", "original_year", "style"])
        for i, p in enumerate(paths):
            rec = "rec-same" if i in (7, 8) else f"rec-{i}"      # tracks 7 and 8: one recording, two files
            year = 1970 if i == 6 else ""                        # track 6: a 1970 song on a 2005 compilation
            style = "Big Beat" if i in (9, 10) else ""
            w.writerow([p, rec, f"art-{i}", year, style])
    sim = os.path.join(dirpath, "sim.csv")
    with open(sim, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["artist_mbid", "similar_mbid", "source", "score"])
        w.writerow(["art-0", "art-11", "lastfm", 0.9])
        w.writerow(["art-11", "art-0", "listenbrainz", 0.4])
    return ids, sim


@pytest.fixture(scope="module")
def lib(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("enrich"))
    dbp, paths = build_db(d)
    plain = hybrid.HybridEngine(dbp)                   # before any import
    ids, sim = write_csvs(d, paths)
    conn = sqlite3.connect(dbp)
    counts = enrich.import_csvs(conn, ids, sim)
    conn.close()
    return {"db": dbp, "paths": paths, "plain": plain, "counts": counts,
            "on": hybrid.HybridEngine(dbp), "off": hybrid.HybridEngine(dbp, catalog=False)}


def test_import_counts(lib):
    assert lib["counts"] == {"catalog_ids": N, "artist_similarity": 2}


def test_no_tables_no_change(lib):
    plain, off = lib["plain"], lib["off"]
    for si in range(N):
        assert np.array_equal(plain._score(si), off._score(si))
        assert plain.mix(plain.paths[si], size=N) == off.mix(off.paths[si], size=N)


def test_covers_is_not_a_genre(lib):
    eng = lib["off"]
    assert eng.genre_tags[0] == {"punk"} == eng.genre_tags[1]
    assert eng.explain(0, 1)["genre"] == pytest.approx(eng.w["genre"])   # full credit, not half
    assert eng.explain(0, 3)["genre"] == 0.0                              # Punk cover vs Pop cover


def test_style_joins_the_genre_tags(lib):
    on, off = lib["on"], lib["off"]
    assert on.genre_tags[9] == {"electronic", "big beat"}
    assert off.genre_tags[9] == {"electronic"}


def test_original_year_replaces_the_file_year(lib):
    assert lib["on"].year[6] == 1970 and lib["off"].year[6] == 2005
    assert lib["on"].year[0] == 2005                                     # no original year: file year stands


def test_one_recording_once(lib):
    on, off = lib["on"], lib["off"]
    p = lib["paths"]
    mixed = on.mix(p[0], size=N)
    assert not (p[7] in mixed and p[8] in mixed)
    assert p[7] in off.mix(p[0], size=N) and p[8] in off.mix(p[0], size=N)
    assert p[8] not in on.mix(p[7], size=N)                              # the seed's twin is out too


def test_artist_weight_is_off_by_default_and_explain_adds_up(lib):
    on = lib["on"]
    assert "artist" not in on.w
    assert "artist" not in on.explain(0, 11)
    eng = hybrid.HybridEngine(lib["db"], weights={"artist": 0.3})
    comp = eng.explain(0, 11)
    assert comp["artist"] == pytest.approx(0.3 * 0.9)                   # best score of the two sources
    assert comp["total"] == pytest.approx(float(eng._score(0)[11]))
    assert eng.explain(11, 0)["artist"] == pytest.approx(0.3 * 0.9)     # read both ways
    assert eng.explain(0, 5)["artist"] == 0.0


def test_before_version_is_the_engine_without_the_catalog(lib):
    before = lib["on"].without_catalog()
    plain = lib["plain"]                        # loaded before any import: the old engine, but covers out
    assert before.genre_tags[0] == {"punk", "covers"}     # covers counted, as before
    assert all(before.year == plain.year)
    for si in (1, 4, 9):                        # seeds whose tags carry no covers
        assert before.mix(before.paths[si], size=N) is not None
    assert lib["on"].genre_tags[0] == {"punk"}            # the source engine is unchanged


def test_artist_weight_survives_the_scoring_switches(lib):
    eng = hybrid.HybridEngine(lib["db"], weights={"artist": 0.3}, fusion="rank")
    assert eng._switched()
    assert "artist" in [name for name, _w, _g in eng._terms(0)]
    comp = eng.explain(0, 11)
    assert comp["total"] == pytest.approx(float(eng._score(0)[11]))
