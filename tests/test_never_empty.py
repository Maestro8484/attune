"""A mix is never empty, and a vote never throws the list away (2026-10-02, TODO.md row 51,
ISSUES.md row 95).

Joe's case: a Blend of three songs, one Less Like This on a fourth, and the list was gone.
Two things did it. The dislike was subtracted at the same weight as a like (0.75), so one
vote moved the query to where no song came within the sound-alike line; and the line ended
the walk with nothing taken. These hold the two fixes in place, on the synthetic library
from test_collections_fit.py: three tight groups (A = 0-14, B = 15-29, C = 30-38) whose
fingerprints are unrelated, and a loner (39) unrelated to everything.

  1. a dislike pushes at the textbook gamma (0.15), a like pulls at beta (0.75), and one
     dislike on a Blend keeps most of the Blend's songs;
  2. the floor: a walk with a line still takes at least `floor` songs, past the line if it
     must, and says which ones, with 'fit' as the reason because the line was met;
  3. without a floor the walk is exactly what it was: the loner still gets nothing;
  4. through the server: Blend, steering, mix and radio with the line on all come back
     with songs for the loner, marked past the line; floor=0 brings the old answer back;
     an Adventure still drops a stop with no song near the path (it takes no floor).
"""
from __future__ import annotations
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))

import hybrid                                                   # noqa: E402
from test_collections_fit import build_db, client, LONER        # noqa: E402,F401

A = list(range(0, 15))


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    d = tmp_path_factory.mktemp("never-empty-eng")
    return hybrid.HybridEngine(build_db(str(d)))


def _ids(j):
    return [t["i"] for t in j["tracks"]]


# ------------------------------------------------------------------ the vote weights

def test_a_dislike_pushes_at_gamma_and_a_like_pulls_at_beta(eng):
    assert hybrid.ROCCHIO_GAMMA < hybrid.ROCCHIO_BETA
    q0 = eng.X[0].astype(np.float64)
    liked = eng.refine(0, liked_idx=[1])
    disliked = eng.refine(0, disliked_idx=[1])
    # a like moves the query more than a dislike of the same song does
    assert float(liked @ q0) < float(disliked @ q0)
    # the arithmetic, by hand
    want = eng._unit(q0 - hybrid.ROCCHIO_GAMMA * eng.X[1].astype(np.float64))
    assert np.allclose(disliked, want)


def test_one_dislike_keeps_most_of_a_blend(eng):
    seeds = [0, 1, 2]
    blend = eng.mix_multi(seeds, size=10)[0]
    centre = eng.X[seeds].astype(np.float64).mean(axis=0)
    dislike = eng.idx[blend[-1]]                     # a song that WAS on the list
    q = eng.refine(centre, disliked_idx=[dislike])
    after = eng.mix_from_vector(q, size=10, exclude=seeds + [dislike])
    kept = set(blend) & set(after)
    assert len(kept) >= 7, (len(kept), blend, after)
    # (The contrast with the old weight, 8 of 100 kept against 71, is a measurement on the
    # reference library: audit-sonic/harness/never_empty.py. This library's groups are too
    # tight to show it: every song in a group stays nearest whatever the vote.)


# ------------------------------------------------------------------ the floor, engine level

def test_the_floor_keeps_songs_past_the_line_and_says_which(eng):
    rep = {}
    picks = eng.mix(eng.paths[LONER], size=50, min_fit=0.7, report=rep, floor=10)
    assert len(picks) == 10
    assert rep["stopped"] == "fit"                   # the line was met; the floor carried on
    assert rep["floor"] == 10
    past = rep["past_line"]
    assert len(past) == 10 and all(f < 0.7 for _, f in past)
    assert [eng.idx[p] for p in picks] == [j for j, _ in past]
    # best first, past the line too
    fits = [f for _, f in past]
    assert fits == sorted(fits, reverse=True)


def test_the_floor_only_tops_up_what_fits(eng):
    rep = {}
    picks = eng.mix(eng.paths[0], size=50, min_fit=0.7, report=rep, floor=25)
    fit_n = len(picks) - len(rep["past_line"])
    assert 0 < fit_n < 25 and len(picks) == 25
    # the ones that fit come first, then the ones past the line
    fits = [rep["fit"][eng.idx[p]] for p in picks]
    assert all(f >= 0.7 for f in fits[:fit_n]) and all(f < 0.7 for f in fits[fit_n:])


def test_without_a_floor_the_walk_is_what_it_was(eng):
    rep = {}
    assert eng.mix(eng.paths[LONER], size=50, min_fit=0.7, report=rep) == []
    assert rep["stopped"] == "fit" and rep["past_line"] == [] and rep["floor"] is None


def test_a_full_list_needs_no_floor(eng):
    rep = {}
    picks = eng.mix(eng.paths[0], size=5, min_fit=0.7, report=rep, floor=3)
    assert len(picks) == 5 and rep["stopped"] == "size" and rep["past_line"] == []


# ------------------------------------------------------------------ through the server

def test_the_loner_gets_a_mix_marked_past_the_line(client):
    j = client.get(f"/api/mix?i={LONER}&size=50&max=1&min_fit=0.7").get_json()
    assert len(j["tracks"]) == 25                    # half of 50, the default floor
    st = j["stop"]
    assert st["reason"] == "floor" and st["floor"] == 25
    assert {p["i"] for p in st["past_line"]} == set(_ids(j))
    assert "0 of 50 fit" in st["sentence"] and "25 more past it" in st["sentence"]


def test_floor_zero_is_the_old_answer(client):
    j = client.get(f"/api/mix?i={LONER}&size=50&max=1&min_fit=0.7&floor=0").get_json()
    assert j["tracks"] == [] and j["stop"]["reason"] == "none_fit"


def test_the_floor_is_a_share_of_the_count(client):
    j = client.get(f"/api/mix?i={LONER}&size=20&max=1&min_fit=0.7&floor=0.25").get_json()
    assert len(j["tracks"]) == 5


def test_a_blend_and_its_steering_are_never_empty(client):
    j = client.get(f"/api/mix/blend?i={LONER}&i=0&size=20&max=1&min_fit=0.999").get_json()
    assert len(j["tracks"]) >= 10 and j["stop"]["reason"] == "floor"
    r = client.post("/api/refine", json={"i": LONER, "seeds": [LONER, 0], "size": 20,
                                         "disliked": [1], "max": "1", "min_fit": "0.999"})
    assert r.status_code == 200, r.get_json()
    j2 = r.get_json()
    assert len(j2["tracks"]) >= 10 and j2["kind"] == "blend"
    assert 1 not in _ids(j2)


def test_radio_keeps_playing_past_the_line(client):
    j = client.get(f"/api/radio/next?seed={LONER}&n=20&variety=0&max=1&min_fit=0.7").get_json()
    assert len(j["tracks"]) == 10 and j["stop"]["reason"] == "floor"


# ------------------------------------------------------------------ the cold read of 2026-10-02

def test_bans_do_not_eat_the_floor(client):
    base = client.get(f"/api/mix?i={LONER}&size=50&max=1&min_fit=0.7").get_json()
    ban = "&".join(f"ban={i}" for i in _ids(base)[:5])
    j = client.get(f"/api/mix?i={LONER}&size=50&max=1&min_fit=0.7&{ban}").get_json()
    assert len(j["tracks"]) == 25
    # the sentence and the marks describe the list delivered, not the walk's take
    assert {p["i"] for p in j["stop"]["past_line"]} == set(_ids(j))
    assert "25 more past it" in j["stop"]["sentence"]


def test_dedup_does_not_eat_the_floor(client):
    j = client.get(f"/api/mix?i={LONER}&size=50&max=1&min_fit=0.7&dedup=title").get_json()
    assert len(j["tracks"]) == 25
    assert {p["i"] for p in j["stop"]["past_line"]} == set(_ids(j))


def test_steering_with_bans_keeps_the_floor(client):
    base = client.post("/api/refine", json={"i": LONER, "size": 50, "disliked": [],
                                            "max": "1", "min_fit": "0.7"}).get_json()
    bans = _ids(base)[:5]
    j = client.post("/api/refine", json={"i": LONER, "size": 50, "ban": bans,
                                         "max": "1", "min_fit": "0.7"}).get_json()
    assert len(j["tracks"]) == 25 and not set(bans) & set(_ids(j))
    assert {p["i"] for p in j["stop"]["past_line"]} == set(_ids(j))


def test_radio_floor_takes_passed_over_fitting_songs_first(client):
    """Seed 0 has 14 group-mates that fit at 0.7. With the variety coin passing most of
    them over, the floor (10 of 20) is met by taking those back, never by a song below
    the line; so no batch carries a past-line song, and none is short of the floor."""
    saw_thinned = False
    for r in range(30):
        j = client.get(f"/api/radio/next?seed=0&n=20&variety=9&rng={r}&max=1&min_fit=0.7").get_json()
        st = j["stop"]
        assert len(j["tracks"]) >= 10, (r, st)
        assert st["past_line"] == [], (r, st)
        if st["passed_over"]:
            saw_thinned = True
            assert "passed over" in st["sentence"]
    assert saw_thinned


def test_radio_floor_carries_past_the_line_only_after_the_passed_over(client):
    # the loner has nothing that fits, so its floor is all carried songs, and the
    # sentence says so
    j = client.get(f"/api/radio/next?seed={LONER}&n=20&variety=9&rng=3&max=1&min_fit=0.7").get_json()
    assert len(j["tracks"]) == 10 and j["stop"]["reason"] == "floor"
    assert len(j["stop"]["past_line"]) == 10


def test_a_bad_floor_on_radio_is_a_400(client):
    r = client.get(f"/api/radio/next?seed=0&n=20&max=1&min_fit=0.7&floor=nan")
    assert r.status_code == 400


def test_strongest_left_is_not_on_the_list_when_the_floor_carried(client):
    j = client.get(f"/api/mix?i={LONER}&size=50&max=1&min_fit=0.7").get_json()
    sl = j["stop"]["strongest_left"]
    assert sl is None or sl["i"] not in _ids(j)


def test_a_floor_of_the_whole_count_still_says_fit(eng):
    rep = {}
    picks = eng.mix(eng.paths[LONER], size=10, min_fit=0.7, report=rep, floor=10)
    assert len(picks) == 10 and rep["stopped"] == "fit"
    assert rep["strongest_left"] is not None and rep["strongest_left"][0] not in [eng.idx[p] for p in picks]


def test_an_adventure_still_drops_a_stop_with_nothing_near(client):
    # the loner's end of the path has no song near it; the floor is not applied per stop
    j = client.get(f"/api/mix/adventure?a=0&b={LONER}&size=12&max=1&min_fit=0.9").get_json()
    assert j["stop"]["reason"] in ("fit", "exhausted") and len(j["tracks"]) < 12
