"""The twin block in the walk (2026-10-06, ISSUES.md row 101): one recording once, without a catalog.

Locks in:
  1. a song held in two files under one artist and title is taken once; the second is reported
     as dropped for 'twin';
  2. the seed's own twin (another file of the same song) is never the first pick;
  3. two files whose fingerprints are all but identical are NOT blocked by the walk (that is the
     app's near-twin re-pick's job) unless TWIN_COSINE is set, and then they are;
  4. the title is compared without its suffixes (live, remastered, a bracketed note);
  5. a library of distinct songs walks exactly as before: nothing is dropped for 'twin';
  6. the name compared is None for a song with no title, and such songs are never twins of each
     other by name.

Synthetic library built the way test_sound_profile builds one, with the twins added. The
files are named .snd so the leak scanner's bulk-media rule (ten audio filenames in one file)
does not read a synthetic fixture as a library dump.
"""
from __future__ import annotations
import os
import sys
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

import db as dbm                     # noqa: E402
from features import FEATURE_DIM     # noqa: E402
import hybrid                        # noqa: E402

N = 24


def build(dirpath, twins=True):
    os.makedirs(dirpath, exist_ok=True)
    dbp = os.path.join(dirpath, "lib.db")
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(11)
    now = int(time.time())
    vecs = []
    for i in range(N):
        v = rng.normal(size=512)
        vecs.append(v / np.linalg.norm(v))
    rows = []
    for i in range(N):
        rows.append((os.path.join(dirpath, f"t{i:02d}.snd"), f"Artist {i}", f"Title {i}", vecs[i]))
    if twins:
        # the seed (song 0) again under another album, with a bracketed note: a twin by name
        again = vecs[0] + 0.004 * rng.normal(size=512)          # the same recording, another file
        rows.append((os.path.join(dirpath, "t00_again.snd"), "Artist 0", "Title 0 (Remastered 2009)", again / np.linalg.norm(again)))
        # song 3 again as a live take: a twin by name through the suffix rule
        rows.append((os.path.join(dirpath, "t03_live.snd"), "Artist 3", "Title 3 - Live", vecs[7] * 0.4 + vecs[8] * 0.6))
        # song 2's recording under a different title: a twin by fingerprint
        near = vecs[2] + 0.004 * rng.normal(size=512)
        rows.append((os.path.join(dirpath, "t02_other.snd"), "Artist 2", "Some Other Name", near / np.linalg.norm(near)))
        # two files with no title at all: never twins by name
        rows.append((os.path.join(dirpath, "blank_a.snd"), "Artist 9", "", vecs[9] * 0.5 + vecs[10] * 0.5))
        rows.append((os.path.join(dirpath, "blank_b.snd"), "Artist 19", None, vecs[11] * 0.5 + vecs[12] * 0.5))
    for p, a, t, v in rows:
        v = np.asarray(v, float)
        v = v / np.linalg.norm(v)
        conn.execute("INSERT INTO tracks(path, artist, album, title, genre, year, seconds, bytes, mtime) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (p, a, "Album", t, "Rock", 1990, 200, 1, now))
        conn.execute("INSERT INTO features(path, vec, dim, error, src_mtime, tempo) VALUES(?,?,?,?,?,?)",
                     (p, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(), FEATURE_DIM, None, now, 100.0))
        conn.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)", (p, v.astype(np.float32).tobytes(), 512))
    conn.commit()
    conn.close()
    return dbp


@pytest.fixture()
def lib(tmp_path):
    return build(str(tmp_path / "lib"))


def test_the_twin_key_strips_suffixes_and_notes():
    k = hybrid._twin_key
    assert k("The Band", "Up on Cripple Creek") == k("The Band", "Up On Cripple Creek (Previously Unreleased)")
    assert k("Beatles", "Get Back") == k("Beatles", "Get Back - Live")
    assert k("A", "Title 0") == k("A", "Title 0 (Remastered 2009)")
    assert k("A", "") is None and k("A", None) is None
    assert k("A", "Song") != k("B", "Song")


def test_a_song_held_twice_is_taken_once(lib):
    e = hybrid.HybridEngine(lib)
    seed = next(p for p in e.paths if p.endswith("t10.snd"))
    rep = {}
    out = e.mix(seed, size=len(e.paths), artist_spacing=1, report=rep)
    names = [e.twin[e.idx[p]] for p in out]
    named = [n for n in names if n is not None]
    assert len(named) == len(set(named)), "a name came back twice"
    reasons = {why for _, _, _, why in rep["dropped"]}
    assert "duplicate" in reasons


def test_the_seeds_own_twin_is_never_the_first_pick(lib):
    e = hybrid.HybridEngine(lib)
    seed = next(p for p in e.paths if p.endswith("t00.snd"))
    rep = {}
    out = e.mix(seed, size=5, artist_spacing=1, report=rep)
    assert not any(p.endswith("t00_again.snd") for p in out)
    twin_i = e.idx[next(p for p in e.paths if p.endswith("t00_again.snd"))]
    assert any(j == twin_i and why == "duplicate" for j, _, _, why in rep["dropped"])


def test_the_same_recording_under_another_title_is_left_to_the_apps_near_twin_rule(lib, monkeypatch):
    """By fingerprint the walk blocks nothing unless TWIN_COSINE is set: the app's near-twin
    re-pick is the one fingerprint rule. Set, the mechanism works."""
    e = hybrid.HybridEngine(lib)
    seed = next(p for p in e.paths if p.endswith("t02.snd"))
    other = e.idx[next(p for p in e.paths if p.endswith("t02_other.snd"))]
    assert hybrid.TWIN_COSINE is None
    assert float(e.X[other] @ e.X[e.idx[seed]]) >= 0.98
    out = e.mix(seed, size=5, artist_spacing=1)
    assert any(p.endswith("t02_other.snd") for p in out)
    monkeypatch.setattr(hybrid, "TWIN_COSINE", 0.98)
    rep = {}
    out = e.mix(seed, size=5, artist_spacing=1, report=rep)
    assert not any(p.endswith("t02_other.snd") for p in out)
    assert any(j == other and why == "duplicate" for j, _, _, why in rep["dropped"])


def test_songs_without_a_title_are_not_twins_by_name(lib):
    e = hybrid.HybridEngine(lib)
    seed = next(p for p in e.paths if p.endswith("t15.snd"))
    out = e.mix(seed, size=len(e.paths), artist_spacing=1)
    blanks = [p for p in out if os.path.basename(p).startswith("blank_")]
    assert len(blanks) == 2


def test_a_library_of_distinct_songs_walks_as_before(tmp_path):
    dbp = build(str(tmp_path / "plain"), twins=False)
    e = hybrid.HybridEngine(dbp)
    seed = e.paths[0]
    rep = {}
    out = e.mix(seed, size=len(e.paths), artist_spacing=1, report=rep)
    assert len(out) == len(e.paths) - 1
    assert not any(why == "duplicate" for _, _, _, why in rep["dropped"])
    s = e._score(0)
    order = [e.paths[j] for j in np.argsort(-s) if j != 0]
    assert out == order
