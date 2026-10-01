"""Steering a Blend or an Adventure keeps it what it is (2026-10-01).

Before this, every steered or filtered list was re-ranked around its first seed alone:
a Blend lost the sound of its other seeds, an Adventure lost its walk. These hold the
fix in place, on the synthetic library from test_collections_fit.py: three tight groups
(A = 0-14, B = 15-29, C = 30-38) whose fingerprints are unrelated to each other.

  1. an Adventure called with no steering is exactly the walk it always was;
  2. a removed song never stands on a steered Adventure, a liked one always does, and the
     two ends stay first and last;
  3. liking a song bends the whole walk toward it;
  4. through the server, a steered Blend draws from all its seeds, a steered Adventure
     comes back as a walk, and a plain steered mix is unchanged.
"""
from __future__ import annotations
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import hybrid                                                   # noqa: E402
from test_collections_fit import build_db, client               # noqa: E402,F401

A = set(range(0, 15))
B = set(range(15, 30))
C = set(range(30, 39))


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    d = tmp_path_factory.mktemp("steer-eng")
    return hybrid.HybridEngine(build_db(str(d)))


def _ix(eng, path):
    return [eng.idx[p] for p in path]


def test_adventure_without_steering_is_the_old_walk(eng):
    plain = eng.adventure(0, 15, size=10)
    empty = eng.adventure(0, 15, size=10, liked=[], disliked=[], keep=[], exclude=[])
    assert plain == empty


def test_removed_songs_stay_off_and_liked_songs_stay_on(eng):
    plain = _ix(eng, eng.adventure(0, 15, size=10))
    gone = plain[2]
    liked = 33                                   # a group C song, nowhere near A or B
    assert liked not in plain
    path = _ix(eng, eng.adventure(0, 15, size=10, keep=[liked], liked=[liked],
                                  exclude=[gone]))
    assert path[0] == 0 and path[-1] == 15
    assert gone not in path
    assert liked in path and path.index(liked) not in (0, len(path) - 1)
    assert len(path) == len(set(path))          # nothing twice


def test_liking_a_song_bends_the_walk_toward_it(eng):
    plain = _ix(eng, eng.adventure(0, 15, size=12))
    bent = _ix(eng, eng.adventure(0, 15, size=12, liked=[33]))
    assert sum(i in C for i in bent) > sum(i in C for i in plain)


def _ids(j):
    return [t["i"] for t in j["tracks"]]


def test_steered_blend_draws_from_every_seed(client):
    one = client.post("/api/refine", json={"i": 0, "size": 20, "liked": [1]}).get_json()
    both = client.post("/api/refine", json={"i": 0, "size": 20, "liked": [1],
                                            "seeds": [0, 15]}).get_json()
    assert one["kind"] == "mix" and both["kind"] == "blend"
    assert not any(i in B for i in _ids(one)[:10])      # around seed A alone: A first
    assert any(i in B for i in _ids(both))              # the second seed's sound is back
    assert 0 not in _ids(both) and 15 not in _ids(both)  # seeds are the window's to place


def test_steered_adventure_comes_back_as_a_walk(client):
    plain = client.post("/api/refine", json={
        "i": 0, "adventure": {"a": 0, "b": 15, "size": 8}}).get_json()
    walk = _ids(plain)
    assert plain["kind"] == "adventure" and walk[0] == 0 and walk[-1] == 15
    gone = walk[3]
    j = client.post("/api/refine", json={
        "i": 0, "ban": [gone], "liked": [33],
        "adventure": {"a": 0, "b": 15, "size": 8}}).get_json()
    w2 = _ids(j)
    assert w2[0] == 0 and w2[-1] == 15 and gone not in w2 and 33 in w2
    # the liked song rides on top of the stops; the "N of M" line counts stops only
    assert j["stop"]["returned"] == len(w2) - 2 - 1 <= j["stop"]["requested"]


def test_adventure_refine_refuses_bad_ends(client):
    r = client.post("/api/refine", json={"i": 0, "adventure": {"a": 0, "b": 99999}})
    assert r.status_code == 400
