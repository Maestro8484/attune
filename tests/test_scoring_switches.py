"""The scoring switches (2026-10-01): fusion, the fingerprint space, the feel ingredient.

Everything here defends one promise and three properties:

  the promise   with every switch off, _score() is the V2 arithmetic it has always been,
                to the last bit (an oracle copy of that arithmetic sits in this file);
  fusion        under "rank" a weight is a share of say: on a library whose ingredients
                have wildly different raw spreads, influence falls in weight order;
  centering     the centered fingerprints no longer share one direction, and the stored
                ones are not touched;
  feel          the scores are a fixed function of the stored fingerprints and a small
                file of directions, computed with no model loaded.
"""
from __future__ import annotations
import json
import os
import sys
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "eval"))

import db as dbm                     # noqa: E402
from features import FEATURE_DIM     # noqa: E402
import hybrid                        # noqa: E402
import influence                     # noqa: E402
import variants                      # noqa: E402
from test_collections_fit import build_db as build_small_db   # noqa: E402

N = 600
GENRES = ["rock", "jazz", "folk", "rap", "classical", "blues"]


def _unit(v):
    return v / np.linalg.norm(v)


def build_spread_db(dirpath):
    """600 tracks whose ingredients vary independently and on very different raw scales,
    like the real library: fingerprints bunched round one direction (cosines within a
    hair of each other), tempo and year spread wide."""
    dbp = os.path.join(dirpath, "spread.db")
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(20261001)
    now = int(time.time())
    shared = _unit(rng.normal(size=512))
    for i in range(N):
        path = os.path.join(dirpath, f"t_{i:03d}.mp3")
        v = _unit(shared + 0.2 * rng.normal(size=512) / np.sqrt(512))
        conn.execute(
            "INSERT INTO tracks(path, artist, album, title, genre, year, seconds, bytes, mtime) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (path, f"Artist {i}", "Album", f"Title {i}", GENRES[int(rng.integers(len(GENRES)))],
             int(rng.integers(1960, 2021)), 200, 1, now))
        conn.execute(
            "INSERT INTO features(path, vec, dim, error, src_mtime, tempo) VALUES(?,?,?,?,?,?)",
            (path, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(), FEATURE_DIM,
             None, now, float(rng.uniform(60, 180))))
        conn.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)",
                     (path, v.astype(np.float32).tobytes(), 512))
    conn.commit()
    conn.close()
    return dbp


@pytest.fixture(scope="module")
def small(tmp_path_factory):
    return hybrid.HybridEngine(build_small_db(str(tmp_path_factory.mktemp("sw-small"))))


@pytest.fixture(scope="module")
def spread(tmp_path_factory):
    return hybrid.HybridEngine(build_spread_db(str(tmp_path_factory.mktemp("sw-spread"))))


def _v2_score(eng, si):
    """_score() exactly as it shipped before the switches existed, kept as the oracle."""
    w = eng.w
    s = w["clap"] * (eng.X @ eng.X[si])
    if eng.use_lib and w.get("lib"):
        s = s + w["lib"] * (eng.L @ eng.L[si])
    st = eng.genre_tags[si]
    if w.get("genre"):
        jac = np.array([len(st & g) / len(st | g) if st and g else 0.0 for g in eng.genre_tags])
        s = s + w["genre"] * jac
    if w.get("bpm"):
        sb = eng.bpm[si]
        dbpm = np.where((eng.bpm > 0) & (sb > 0), np.abs(eng.bpm - sb) / 40.0, 0.5)
        s = s - w["bpm"] * dbpm
    if w.get("key") and eng.key[si]:
        kc = np.array([hybrid._key_compat(eng.key[si], kj) for kj in eng.key])
        s = s + w["key"] * kc
    if w.get("era"):
        sy = eng.year[si]
        dera = np.where((eng.year > 0) & (sy > 0), np.abs(eng.year - sy) / 25.0, 0.5)
        s = s - w["era"] * dera
    return s


def _feel_file(dirpath, space="raw", dims=(0, 1)):
    """A feel file whose directions are plain axes, so the expected score is known."""
    scores = []
    for k, d in enumerate(dims):
        w = np.zeros(512); w[d] = 1.0
        scores.append({"name": f"axis{k}", "label": f"Axis {k}", "w": w.tolist()})
    p = os.path.join(dirpath, "feel.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump({"model": "test", "space": space, "scores": scores}, fh)
    return p


# ------------------------------------------------------------------ the promise

def test_switches_are_off_by_default(small):
    assert small.fusion == "raw" and small.clap_space == "raw"
    assert small.w["feel"] == 0.0
    assert not small._switched()


@pytest.mark.parametrize("fixture", ["small", "spread"])
def test_switches_off_is_the_v2_score_to_the_bit(fixture, request):
    eng = request.getfixturevalue(fixture)
    for si in (0, 7, len(eng.paths) - 1):
        a, b = eng._score(si), _v2_score(eng, si)
        assert a.dtype == b.dtype and a.tobytes() == b.tobytes()


def test_feel_scores_present_but_unweighted_change_nothing(small, tmp_path):
    e = variants.variant_engine(small, "v2")
    e._load_feel(_feel_file(str(tmp_path)))
    assert e.feel is not None and not e._switched()
    assert e._score(3).tobytes() == _v2_score(small, 3).tobytes()


def test_a_version_does_not_change_the_engine_it_was_made_from(small):
    before = (dict(small.w), small.fusion, small.clap_space, small.X.tobytes())
    e = variants.variant_engine(small, "v2-fused")
    e._score(0)
    assert (dict(small.w), small.fusion, small.clap_space, small.X.tobytes()) == before
    assert e.fusion == "rank" and e.clap_space == "centered"


def test_unknown_switch_values_are_refused(tmp_path):
    dbp = build_small_db(str(tmp_path))
    with pytest.raises(ValueError):
        hybrid.HybridEngine(dbp, fusion="sideways")
    with pytest.raises(ValueError):
        hybrid.HybridEngine(dbp, clap_space="bent")


def test_switches_can_come_from_the_engine_file(tmp_path):
    dbp = build_small_db(str(tmp_path))
    with open(os.path.join(str(tmp_path), "engine_v2.json"), "w") as fh:
        json.dump({"scoring": {"fusion": "rank", "clap_space": "centered"}}, fh)
    e = hybrid.HybridEngine(dbp)
    assert (e.fusion, e.clap_space) == ("rank", "centered")
    with open(os.path.join(str(tmp_path), "engine_v2.json"), "w") as fh:
        json.dump({"scoring": {"fusion": "nonsense"}}, fh)
    assert hybrid.HybridEngine(dbp).fusion == "raw"      # unknown value ignored, not trusted


# ------------------------------------------------------------------ fusion

def test_percentile_handles_ties_and_extremes():
    p = hybrid._pct(np.array([5.0, 1.0, 3.0, 3.0]))
    assert p[1] == 0.0 and p[0] == 1.0 and p[2] == p[3] == 0.5
    assert (hybrid._pct(np.zeros(9)) == 0.5).all()


def test_rank_fusion_puts_influence_in_weight_order(spread):
    """Raw spreads here differ by orders of magnitude (fingerprint cosines within a hair
    of each other, tempo across 120 bpm). Raw addition lets the wide ingredients decide
    and the fingerprint barely counts whatever its weight; ranked, say follows weight."""
    weights = {"clap": 1.0, "lib": 0.5, "bpm": 0.25, "era": 0.1, "genre": 0.0}
    seeds = list(range(0, N, 50))
    med = lambda r: {k: float(np.median(v)) for k, v in r.items()}      # noqa: E731

    raw = med(influence.influence(variants.variant_engine(spread, "v2", weights=weights),
                                  seeds, k=50))
    ranked = med(influence.influence(
        variants.variant_engine(spread, "v2", weights=weights, fusion="rank"), seeds, k=50))

    assert ranked["clap"] > ranked["lib"] > ranked["bpm"] > ranked["era"]
    assert raw["clap"] < raw["bpm"]                 # the fault being fixed
    assert ranked["clap"] > raw["clap"]


def test_z_fusion_ignores_a_flat_ingredient(small):
    e = variants.variant_engine(small, "v2", fusion="z")
    s = e._score(0)                                # tempo and year are constant in this library
    assert np.isfinite(s).all()


def test_explain_adds_up_to_the_switched_score(spread, tmp_path):
    for kw in ({"fusion": "rank"}, {"fusion": "z", "clap_space": "centered"},
               {"clap_space": "abtt2"}):
        e = variants.variant_engine(spread, "v2", **kw)
        s = e._score(5)
        for ci in (0, 99, 417):
            assert e.explain(5, ci)["total"] == s[ci]
    e = variants.variant_engine(spread, "v2", fusion="rank", weights={"feel": 0.4})
    e._load_feel(_feel_file(str(tmp_path)))
    comp = e.explain(5, 99)
    assert comp["total"] == e._score(5)[99] and comp["feel"] != 0.0


def test_fit_line_still_reads_the_seed_as_a_perfect_fit(spread):
    e = variants.variant_engine(spread, "v2-fused")
    rep = {}
    out = e.mix(e.paths[0], size=20, min_fit=0.5, report=rep)
    assert out and all(0.5 <= f <= 1.0 + 1e-9 for f in rep["fit"].values())


# ------------------------------------------------------------------ centering

def test_centered_fingerprints_lose_the_shared_direction(spread):
    x_before = spread.X.copy()
    raw_len = np.linalg.norm(spread.X.astype(np.float64).mean(0))
    M = spread._clap_matrix("centered")
    assert raw_len > 0.9
    assert np.linalg.norm(M.astype(np.float64).mean(0)) < 0.1
    assert np.allclose(np.linalg.norm(M, axis=1), 1.0, atol=1e-5)
    assert spread._clap_matrix("raw") is spread.X
    assert (spread.X == x_before).all()            # the stored vectors are untouched
    spread._spaces.clear()


def test_all_but_the_top_removes_the_strongest_direction(spread):
    Xc = spread.X.astype(np.float64); Xc = Xc - Xc.mean(0)
    top = np.linalg.eigh(Xc.T @ Xc)[1][:, -1]
    M = spread._clap_matrix("abtt1").astype(np.float64)
    assert np.abs(M @ top).max() < 1e-4
    spread._spaces.clear()


# ------------------------------------------------------------------ feel

def test_feel_scores_are_fixed_and_need_no_model(spread, tmp_path):
    f = _feel_file(str(tmp_path), space="raw", dims=(3, 10))
    a = variants.variant_engine(spread, "v2"); a._load_feel(f)
    b = variants.variant_engine(spread, "v2"); b._load_feel(f)
    assert a.feel_names == ["axis0", "axis1"] and a.feel.shape == (N, 2)
    assert (a.feel == b.feel).all()
    expect = hybrid._pct(spread.X[:, 3].astype(np.float64)).astype(np.float32)
    assert np.allclose(a.feel[:, 0], expect)
    assert a.feel.min() == 0.0 and a.feel.max() == 1.0
    assert "torch" not in sys.modules and "transformers" not in sys.modules


def test_feel_from_text_pairs_matches_its_direction(spread, tmp_path):
    rng = np.random.default_rng(5)
    pos, neg = rng.normal(size=(2, 512)), rng.normal(size=(3, 512))
    p = os.path.join(str(tmp_path), "pairs.json")
    with open(p, "w") as fh:
        json.dump({"space": "centered", "scores": [
            {"name": "x", "pos": pos.tolist(), "neg": neg.tolist()}]}, fh)
    e = variants.variant_engine(spread, "v2"); e._load_feel(p)
    M = spread._clap_matrix("centered").astype(np.float64)
    assert np.allclose(e.feel[:, 0], hybrid._pct(M @ (pos.mean(0) - neg.mean(0))), atol=1e-6)
    spread._spaces.clear()


def test_missing_feel_file_is_not_an_error(small, tmp_path):
    e = variants.variant_engine(small, "v2", weights={"feel": 0.4})
    e.feel = None
    e._load_feel(os.path.join(str(tmp_path), "nope.json"))
    assert e.feel is None and not e._switched()
    assert e._score(0).tobytes() == _v2_score(small, 0).tobytes()


def test_feel_closeness_prefers_songs_that_feel_alike(spread, tmp_path):
    e = variants.variant_engine(spread, "v2", weights={
        "clap": 0.0, "lib": 0.0, "genre": 0.0, "bpm": 0.0, "era": 0.0, "feel": 1.0})
    e._load_feel(_feel_file(str(tmp_path)))
    s = e._score(0)
    gap = np.abs(e.feel - e.feel[0]).mean(axis=1)
    assert np.argmax(s) == 0
    assert np.corrcoef(s, -gap)[0, 1] > 0.999
