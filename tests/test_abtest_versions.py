"""The listening-test harness knows the scoring versions, and can take rankings that
were given somewhere other than its own prompt (2026-10-01).

The harness is what decides what ships (LAW 1), so the two things that would quietly
spoil a test are locked here: a version that is not the scoring it claims to be, and a
ranking recorded against the wrong engine.
"""
from __future__ import annotations
import json
import os
import sys
import types

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "eval"))

import abtest                        # noqa: E402
import variants                      # noqa: E402
from test_collections_fit import build_db   # noqa: E402


@pytest.fixture(scope="module")
def dbp(tmp_path_factory):
    return build_db(str(tmp_path_factory.mktemp("abv")))


def test_versions_are_the_scoring_they_claim(dbp):
    names = ["v2", "v2-fused", "v2-noclap"]
    eng, engines = abtest.build_engines(names, dbp, "http://localhost:1")
    assert list(engines) == names
    sc = {n: abtest._scoring_of(obj) for n, (obj, _pool) in engines.items()}
    assert sc["v2"]["fusion"] == "raw" and sc["v2"]["clap_space"] == "raw"
    assert sc["v2"]["weights"]["clap"] == 1.0
    assert sc["v2-fused"]["fusion"] == "rank" and sc["v2-fused"]["clap_space"] == "centered"
    assert sc["v2-fused"]["weights"]["clap"] == variants.FUSED_CLAP_WEIGHT
    assert "clap" not in sc["v2-noclap"]["weights"]
    # the plain version is the shared engine itself, and is what mix() returns
    assert engines["v2"][0].eng is eng
    got = [eng.paths[r.pool_i] for r in engines["v2"][0].similar(0, size=10)]
    assert got == eng.mix(eng.paths[0], size=10)
    # building the versions left the shared engine as shipped
    assert eng.fusion == "raw" and eng.w["clap"] == 1.0


def test_a_second_library_copy_is_matched_by_path(dbp, tmp_path):
    import shutil
    alt = os.path.join(str(tmp_path), "alt.db")
    shutil.copyfile(dbp, alt)
    eng, engines = abtest.build_engines(["v2", "v2@alt"], dbp, "http://localhost:1",
                                        db_alt={"alt": alt})
    assert engines["v2@alt"][0].eng is not eng
    import random
    seeds = abtest.pick_seeds(eng, engines, 1, 5, random.Random(1))
    _i, _p, mixes = seeds[0]
    assert mixes["v2"] == mixes["v2@alt"]          # same vectors, so the same list


def test_unknown_names_are_refused(dbp):
    with pytest.raises(SystemExit):
        abtest.build_engines(["v2-sideways"], dbp, "http://localhost:1")
    with pytest.raises(SystemExit):
        abtest.build_engines(["v2@nowhere"], dbp, "http://localhost:1")


def _key(tmp_path):
    key = {"engines": {"v2": {"pool_size": 40}, "v2-fused": {"pool_size": 40}},
           "tests": {"Test1": {"label": "one", "seed": "s1",
                               "letter_to_engine": {"A": "v2-fused", "B": "v2"}},
                     "Test2": {"label": "two", "seed": "s2",
                               "letter_to_engine": {"A": "v2", "B": "v2-fused"}}}}
    p = os.path.join(str(tmp_path), "abtest_key_x.json")
    with open(p, "w", encoding="utf-8") as fh:
        json.dump(key, fh)
    return p


def test_given_rankings_are_unsealed_against_the_key(tmp_path, monkeypatch):
    out = os.path.join(str(tmp_path), "results.jsonl")
    monkeypatch.setattr(abtest, "RESULTS_PATH", out)
    args = types.SimpleNamespace(key=_key(tmp_path), rank=["Test1=B>A", "Test2=a=b"])
    abtest.run_score(args)
    rec = json.loads(open(out, encoding="utf-8").read().strip())
    assert rec["tests"][0]["ranking_engines"] == [["v2"], ["v2-fused"]]
    assert rec["tests"][1]["ranking_engines"] == [["v2", "v2-fused"]]


@pytest.mark.parametrize("ranks", [["Test1=B>A"], ["Test1=B>A", "Test2=A>C"],
                                   ["Test1=B>A", "Test2=A>B", "Test9=A>B"]])
def test_incomplete_or_wrong_rankings_record_nothing(tmp_path, monkeypatch, ranks):
    out = os.path.join(str(tmp_path), "results.jsonl")
    monkeypatch.setattr(abtest, "RESULTS_PATH", out)
    with pytest.raises(SystemExit):
        abtest.run_score(types.SimpleNamespace(key=_key(tmp_path), rank=ranks))
    assert not os.path.exists(out)
