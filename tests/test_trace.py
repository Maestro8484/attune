"""The trace (2026-10-05): what the engine did to make a list, from its own records.

The walk already decided every song's place, took some and passed over others; until
this it threw that away and the window got one sentence. Now hybrid._walk's report
carries each taken song's place and score, every song it reached and did not take with
the reason, and what would have come next; web/app.py's _trace() turns that and the
route's own thinning into the `trace` every mix route returns. Held here, on the
synthetic library from test_collections_fit.py (three tight groups and a loner, unique
artists):

  1. the walk's picks are unchanged by asking for the report, and the report names
     every taken song's place and score;
  2. a song passed over for artist spacing is in the report with that reason, and
     the trace lists it under left_out with its fit;
  3. /api/mix returns a trace whose songs cover every track delivered, whose stages
     run in the order the code runs them with exactly one lit, and whose left_out
     names the reason for every song;
  4. the thinning a route does after the walk (a removed song, a duplicate, the
     near-twin re-pick) is in the trace with its reason;
  5. a Blend's trace carries each song's closeness to each seed, a steered list's its
     closeness to the songs voted on, and /api/explain answers the same for one song;
  6. the MusicIP route answers trace=None (read in web/app.py; it needs a live MusicIP
     to exercise, so it is not run here).
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


@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    d = tmp_path_factory.mktemp("trace-eng")
    return hybrid.HybridEngine(build_db(str(d)))


# ------------------------------------------------------------------ the walk's report

def test_report_does_not_change_the_walk_and_names_every_place(eng):
    plain = eng.mix(eng.paths[0], size=12)
    rep = {}
    with_report = eng.mix(eng.paths[0], size=12, report=rep)
    assert with_report == plain
    idx = [eng.idx[p] for p in with_report]
    assert set(rep["rank"]) == set(idx) and set(rep["score"]) == set(idx)
    # places are 1-based positions in the ranking, strictly increasing down the list
    places = [rep["rank"][j] for j in idx]
    assert places == sorted(places) and places[0] >= 1
    # the ceiling is the seed's own score, so a fit of score / ceiling is at most 1
    s = eng._score(0)
    assert rep["ceiling"] == pytest.approx(float(s[0]))
    for j in idx:
        assert rep["score"][j] == pytest.approx(float(s[j]))
        assert rep["score"][j] / rep["ceiling"] <= 1.0 + 1e-9
    assert len(rep["next"]) >= 1 and all(len(t) == 3 for t in rep["next"])


def test_artist_spacing_drops_are_in_the_report(eng):
    # make two group-A songs share an artist: the walk takes one and passes the other
    e = hybrid.HybridEngine.__new__(hybrid.HybridEngine)
    e.__dict__.update(eng.__dict__)
    e.artist = list(eng.artist)
    order = np.argsort(-eng._score(0))
    first, second = int(order[1]), int(order[2])
    e.artist[second] = e.artist[first]
    rep = {}
    out = e.mix(e.paths[0], size=5, report=rep)
    idx = [e.idx[p] for p in out]
    assert first in idx and second not in idx
    dropped = {j: why for j, _place, _score, why in rep["dropped"]}
    assert dropped.get(second) == "artist"


def test_radio_coins_name_which_coin_said_no(eng):
    rep = {}
    rng = np.random.default_rng(7)
    eng.radio_next(eng.paths[0], n=6, variety=3.0, rng=rng, report=rep)
    whys = {why for _j, _p, _s, why in rep["dropped"]}
    assert whys and whys <= {"variety", "energy", "artist", "recording"}
    assert rep["coin_skipped"] == sum(1 for d in rep["dropped"] if d[3] in ("variety", "energy"))


# ------------------------------------------------------------------ the routes

def _ids(j):
    return [t["i"] for t in j["tracks"]]


def test_mix_trace_covers_the_list_and_lights_one_stage(client):
    j = client.get("/api/mix?i=0&size=20&max=1&min_fit=0.5").get_json()
    t = j["trace"]
    assert t["kind"] == "mix" and t["pool"] == 40 and t["line"] == 0.5
    ids = _ids(j)
    assert set(map(str, ids)) == set(t["songs"])          # json keys are strings
    for i in ids:
        s = t["songs"][str(i)]
        assert s["rank"] >= 1 and 0 < s["fit"] <= 1.0 and s["score"] is not None
    stage_ids = [s["id"] for s in t["stages"]]
    assert stage_ids[:4] == ["seeds", "pool", "walk", "line"] and stage_ids[-1] == "result"
    assert sum(1 for s in t["stages"] if s["on"]) == 1
    assert t["decider"] in stage_ids
    assert t["recipe"]["terms"] and t["recipe"]["terms"][0]["id"] == "clap"
    # a song left out carries a reason the window has words for
    known = {"artist", "recording", "variety", "energy", "line", "count",
             "removed", "blocked", "duplicate", "near-twin"}
    assert t["left_out"] and all(d["why"] in known for d in t["left_out"])
    assert not ({d["i"] for d in t["left_out"]} & set(ids))
    # transitions run over the list as shown: seed first, then the picks
    assert t["order"][0] == 0 and t["order"][1:] == ids
    assert len(t["transitions"]) == len(ids)


def test_quota_mix_still_has_a_trace_with_fit(client):
    j = client.get("/api/mix?i=0&size=20").get_json()
    t = j["trace"]
    assert j["stop"]["reason"] == "quota" and t["line"] is None
    assert all(t["songs"][str(i)]["fit"] is not None for i in _ids(j))
    assert any(s["id"] == "line" and s["label"] == "count is a quota" for s in t["stages"])


def test_route_thinning_is_in_the_trace(client):
    base = client.get("/api/mix?i=0&size=20").get_json()
    gone = _ids(base)[0]
    j = client.get(f"/api/mix?i=0&size=20&ban={gone}&variety=1").get_json()
    t = j["trace"]
    assert gone not in _ids(j)
    whys = {d["i"]: d["why"] for d in t["left_out"]}
    assert whys.get(gone) == "removed"
    assert any(s["id"] == "after" for s in t["stages"])
    assert any(d["why"] == "near-twin" for d in t["left_out"])


def test_blend_and_steer_traces_carry_closeness(client):
    j = client.get("/api/mix/blend?i=0&i=16&size=20").get_json()
    t = j["trace"]
    assert t["kind"] == "blend" and len(t["seeds"]) == 2 and t["recipe"]["sound_only"]
    assert "cohesion" in t
    for i in _ids(j):
        assert len(t["songs"][str(i)]["seeds"]) == 2
    liked, disliked = _ids(j)[0], _ids(j)[-1]
    r = client.post("/api/refine", json={"i": 0, "size": 20, "liked": [liked],
                                         "disliked": [disliked]}).get_json()
    t2 = r["trace"]
    assert t2["kind"] == "steer"
    assert t2["votes"]["liked"][0]["i"] == liked and t2["votes"]["disliked"][0]["i"] == disliked
    for i in _ids(r):
        v = t2["songs"][str(i)]["votes"]
        assert len(v["liked"]) == 1 and len(v["disliked"]) == 1
    e = client.get(f"/api/explain?seed=0&cand={_ids(j)[0]}&seeds=0,16&liked={liked}").get_json()
    assert len(e["to_seeds"]) == 2 and len(e["to_liked"]) == 1 and "to_centre" in e
    assert e["recipe"]["terms"]


def test_adventure_trace_is_per_stop(client):
    j = client.get("/api/mix/adventure?a=0&b=16&size=8&max=1&min_fit=0.3").get_json()
    t = j["trace"]
    assert t["kind"] == "adventure" and [s["i"] for s in t["seeds"]] == [0, 16]
    assert t["order"][0] == 0 and t["order"][-1] == 16



def test_radio_trace_names_the_coins(client):
    j = client.get("/api/radio/next?seed=0&n=8&variety=3&rng=7").get_json()
    t = j["trace"]
    assert t["kind"] == "radio" and t["stages"][0]["id"] == "seeds"
    whys = {d["why"] for d in t["left_out"]}
    assert whys <= {"variety", "energy", "artist", "recording", "count", "line"}
    if j["stop"].get("passed_over"):
        assert whys & {"variety", "energy"}
