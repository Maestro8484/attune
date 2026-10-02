"""The sound profile (2026-10-01): the configuration a library gets when its fingerprints were made
with the fingerprint model's trained weights, and the trained head it scores through.

Locks in:
  1. a library that does not say what made its fingerprints scores as V2, to the bit, whatever
     files sit in the models folder;
  2. a library that says "trained weights" gets the sound profile: its weights, its scale, the
     fingerprint through the head;
  3. without a head file such a library stays on V2 instead of failing;
  4. the profile can be forced either way, and an unknown one is refused;
  5. the head is plain arithmetic: numpy alone gives the same rows as the formula written out
     here, each of length 1;
  6. the sound score is the weighted sum of its ingredients on the z scale, and explain() adds
     up to it;
  7. a saved weights file is for the profile it names: an old V2 file leaves the sound profile
     alone;
  8. the Earfeel file that ships is read only for a library with trained-weight fingerprints;
     a file passed by path is always read;
  9. the two data files that ship can be read and are the shape the engine expects;
 10. the listening-test versions are built from V2: a base loaded any other way is refused, and
     the named version "sound-lab" scores as the profile does;
 11. the fit line the app's switch draws sits where the library's profile puts it;
 12. neither fingerprint stage writes its kind of fingerprint into a library made with the other kind.

Synthetic library: thirty tracks, unique artists.
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
import variants                      # noqa: E402

N = 30
GENRES = ["Punk", "Grunge", "Pop", "House", "Folk"]


def build_db(dirpath, trained):
    dirpath = os.path.join(dirpath, "trained" if trained else "plain")
    os.makedirs(dirpath, exist_ok=True)
    dbp = os.path.join(dirpath, "lib.db")
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(7)
    now = int(time.time())
    for i in range(N):
        p = os.path.join(dirpath, f"t{i:02d}.mp3")
        v = rng.normal(size=512)
        v = v / np.linalg.norm(v)
        conn.execute("INSERT INTO tracks(path, artist, album, title, genre, year, seconds, bytes, mtime) "
                     "VALUES(?,?,?,?,?,?,?,?,?)",
                     (p, f"Artist {i}", "Album", f"Title {i}", GENRES[i % len(GENRES)], 1970 + 2 * i, 200, 1, now))
        conn.execute("INSERT INTO features(path, vec, dim, error, src_mtime, tempo) VALUES(?,?,?,?,?,?)",
                     (p, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(), FEATURE_DIM, None, now,
                      90.0 + 3 * i))
        conn.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)", (p, v.astype(np.float32).tobytes(), 512))
    if trained:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('clap_weights', ?)",
                     (hybrid.TRAINED_CLAP_WEIGHTS,))
    conn.commit()
    conn.close()
    return dbp


def write_head(dirpath, hidden=16, out=8):
    rng = np.random.default_rng(5)
    p = os.path.join(dirpath, "head.npz")
    np.savez(p, mean=(0.01 * rng.normal(size=512)).astype(np.float32),
             w1=rng.normal(size=(hidden, 512)).astype(np.float16), b1=rng.normal(size=hidden).astype(np.float16),
             w2=rng.normal(size=(out, hidden)).astype(np.float16), b2=rng.normal(size=out).astype(np.float16))
    return p


@pytest.fixture()
def plain(tmp_path):
    return build_db(str(tmp_path), trained=False)


@pytest.fixture()
def trained(tmp_path):
    return build_db(str(tmp_path), trained=True)


@pytest.fixture()
def head(tmp_path):
    d = tmp_path / "head"
    d.mkdir()
    return write_head(str(d))


def test_a_library_that_does_not_say_scores_as_v2(plain, head):
    e = hybrid.HybridEngine(plain, head_file=head)
    assert e.profile == "v2" and not e.trained_fingerprints
    assert e.w == hybrid.DEFAULT_WEIGHTS and e.fusion == "raw" and e.clap_space == "raw"
    assert not e._switched()
    forced = hybrid.HybridEngine(plain, profile="v2")
    for si in (0, 7, 29):
        assert np.array_equal(e._score(si), forced._score(si))


def test_a_trained_library_gets_the_sound_profile(trained, head):
    e = hybrid.HybridEngine(trained, head_file=head)
    assert e.profile == "sound" and e.trained_fingerprints
    for k, v in hybrid.SOUND_PROFILE["weights"].items():
        assert e.w[k] == v
    assert e.fusion == "z" and e.clap_space == "head" and e.family_credit == 0.0
    assert e._switched()
    picks = e.mix(e.paths[0], size=10)
    assert len(picks) == 10 and e.paths[0] not in picks


def test_a_trained_library_without_a_head_file_stays_on_v2(trained, tmp_path):
    e = hybrid.HybridEngine(trained, head_file=str(tmp_path / "nothing.npz"))
    assert e.profile == "v2" and e.w["clap"] == 1.0 and e.fusion == "raw"
    assert e.mix(e.paths[0], size=5) is not None


def test_the_profile_can_be_forced(plain, trained, head):
    assert hybrid.HybridEngine(trained, head_file=head, profile="v2").profile == "v2"
    e = hybrid.HybridEngine(plain, head_file=head, profile="sound")
    assert e.profile == "sound" and e.clap_space == "head"
    with pytest.raises(ValueError):
        hybrid.HybridEngine(plain, profile="loud")
    with pytest.raises(ValueError):                      # the head space asked for, no head to run
        hybrid.HybridEngine(plain, profile="sound", head_file=os.path.join(os.path.dirname(plain), "no.npz"))._score(0)


def test_the_head_is_plain_arithmetic(trained, head):
    e = hybrid.HybridEngine(trained, head_file=head)
    M = e._clap_matrix("head")
    with np.load(head) as f:
        h = {k: f[k].astype(np.float64) for k in f.files}
    rows = []
    for x in e.X.astype(np.float64):
        xc = x - h["mean"]
        xc = xc / np.linalg.norm(xc)
        a = h["w1"] @ xc + h["b1"]
        a = 0.5 * a * (1.0 + np.tanh(np.sqrt(2.0 / np.pi) * (a + 0.044715 * a ** 3)))
        y = h["w2"] @ a + h["b2"]
        rows.append(y / np.linalg.norm(y))
    assert M.shape == (N, 8)
    assert np.allclose(M, np.array(rows), atol=1e-4)
    assert np.allclose(np.linalg.norm(M, axis=1), 1.0, atol=1e-5)
    assert e.X.shape == (N, 512)                         # the stored fingerprints are not touched


def test_the_sound_score_is_the_z_sum_of_its_ingredients(trained, head):
    e = hybrid.HybridEngine(trained, head_file=head)
    si = 4
    M = e._clap_matrix("head")
    terms = {name: (w, g) for name, w, g in e._terms(si)}
    assert np.allclose(terms["clap"][1], M @ M[si], atol=1e-6)
    assert "lib" not in terms and "bpm" not in terms          # no weight, no say
    total = np.zeros(N)
    for name, (w, g) in terms.items():
        sd = g.std()
        total += w * ((g - g.mean()) / sd if sd > 1e-12 else 0.0)
    assert np.allclose(e._score(si), total)
    for ci in (0, 9, 21):
        assert e.explain(si, ci)["total"] == pytest.approx(e._score(si)[ci])


def test_a_saved_weights_file_is_for_the_profile_it_names(trained, head):
    cfg = os.path.join(os.path.dirname(trained), "engine_v2.json")
    json.dump({"best_weights": {"clap": 0.33, "bpm": 0.9}, "scoring": {"fusion": "rank"}}, open(cfg, "w"))
    e = hybrid.HybridEngine(trained, head_file=head)        # an old file: written for V2
    assert e.profile == "sound" and e.w["clap"] == 1.0 and e.w["bpm"] == 0.0 and e.fusion == "z"
    v2 = hybrid.HybridEngine(trained, head_file=head, profile="v2")
    assert v2.w["clap"] == 0.33 and v2.fusion == "rank"
    json.dump({"profile": "sound", "best_weights": {"era": 0.5}}, open(cfg, "w"))
    e = hybrid.HybridEngine(trained, head_file=head)
    assert e.w["era"] == 0.5 and e.w["clap"] == 1.0
    assert hybrid.HybridEngine(trained, head_file=head, profile="v2").w["era"] == hybrid.DEFAULT_WEIGHTS["era"]


def test_the_shipped_earfeel_file_is_only_for_trained_fingerprints(plain, tmp_path, head):
    assert os.path.exists(hybrid.EARFEEL_FILE)
    assert hybrid.HybridEngine(plain).earfeel is None
    d = tmp_path / "t"
    d.mkdir()
    e = hybrid.HybridEngine(build_db(str(d), trained=True), head_file=head)
    assert e.earfeel is not None and e.earfeel.shape == (N, 5)
    assert e.earfeel_labels == ["Pulse", "Glow", "Heat", "Voice", "Grain"]
    by_path = hybrid.HybridEngine(plain, earfeel_file=hybrid.EARFEEL_FILE)   # a file passed by path is always read
    assert by_path.earfeel is not None


def test_the_shipped_data_files_are_the_shape_the_engine_expects():
    h = hybrid._load_head()
    assert h is not None
    assert h["mean"].shape == (512,) and h["w1"].shape[1] == 512 and h["w2"].shape == (256, h["w1"].shape[0])
    X = np.random.default_rng(0).normal(size=(3, 512)).astype(np.float32)
    out = hybrid._apply_head(h, X / np.linalg.norm(X, axis=1, keepdims=True))
    assert out.shape == (3, 256) and np.isfinite(out).all()
    spec = json.load(open(hybrid.EARFEEL_FILE, encoding="utf-8"))
    assert hybrid.TRAINED_CLAP_WEIGHTS in spec["weights"] and len(spec["scores"]) == 5


def test_versions_are_built_from_v2_and_sound_lab_is_the_profile(trained, head):
    auto = hybrid.HybridEngine(trained, head_file=head)
    with pytest.raises(ValueError):
        variants.variant_engine(auto, "v2")
    base = hybrid.HybridEngine(trained, head_file=head, profile="v2")
    named = variants.variant_engine(base, "sound-lab")
    assert named.fusion == "z" and named.clap_space == "head"
    assert {k: named.w[k] for k in hybrid.SOUND_PROFILE["weights"]} == hybrid.SOUND_PROFILE["weights"]
    for si in (0, 13):
        assert np.allclose(named._score(si), auto._score(si))
    assert base.w == hybrid.DEFAULT_WEIGHTS and base.fusion == "raw"       # the base is not changed


def test_the_fit_line_follows_the_library(plain, trained, head):
    e = hybrid.HybridEngine(plain)
    assert (e.fit_line, e.clap_line) == (hybrid.FIT_LINE_DEFAULT, hybrid.CLAP_LINE_DEFAULT)
    e = hybrid.HybridEngine(trained, head_file=head)
    assert (e.fit_line, e.clap_line) == (hybrid.FIT_LINE_SOUND, hybrid.CLAP_LINE_TRAINED)
    e = hybrid.HybridEngine(trained, head_file=head, profile="v2")
    assert (e.fit_line, e.clap_line) == (hybrid.FIT_LINE_V2_TRAINED, hybrid.CLAP_LINE_TRAINED)


def test_the_app_draws_the_fit_line_where_the_engine_says(tmp_path, monkeypatch):
    """The fit switch with no number of its own uses the line of the library's profile."""
    import importlib.util
    app_py = os.path.join(ROOT, "web", "app.py")
    cfg_home = tmp_path / "config"
    cfg_home.mkdir()
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(cfg_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("ATTUNE_ENV", str(empty_env))
    monkeypatch.syspath_prepend(os.path.dirname(app_py))
    lines = {}
    for kind in ("plain", "trained"):
        d = tmp_path / kind
        d.mkdir()
        dbp = build_db(str(d), trained=(kind == "trained"))
        try:
            spec = importlib.util.spec_from_file_location(f"attune_app_profile_{kind}", app_py)
            appmod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(appmod)
            client = appmod.create_app(dbp, engine_name="v2", playlist_dir=str(tmp_path)).test_client()
        except Exception as e:          # pragma: no cover - environment-dependent
            pytest.skip(f"the app could not be stood up: {type(e).__name__}: {e}")
        lines[kind] = (client.get("/api/mix?i=0&size=20&max=1").get_json()["stop"]["line"],
                       client.get("/api/mix/blend?i=0&i=1&size=20&max=1").get_json()["stop"]["line"])
    assert lines["plain"] == (hybrid.FIT_LINE_DEFAULT, hybrid.CLAP_LINE_DEFAULT)
    assert lines["trained"] == (hybrid.FIT_LINE_SOUND, hybrid.CLAP_LINE_TRAINED)


@pytest.mark.parametrize("module", ["embed_onnx", "embed"])
def test_new_songs_are_never_fingerprinted_into_a_library_of_the_other_kind(module, plain, trained):
    """Both fingerprint stages carry the release's weights. Into a library that says "trained
    weights" they write nothing and say why; a library that does not say is fingerprinted as ever."""
    import importlib
    import sqlite3
    mod = importlib.import_module(module)
    assert mod.CLAP_WEIGHTS is None
    conn = sqlite3.connect(plain)
    mod._refuse_to_mix(conn, 5)                           # same kind: goes ahead
    conn.close()
    conn = sqlite3.connect(trained)
    mod._refuse_to_mix(conn, 0)                           # nothing to fingerprint: nothing to refuse
    with pytest.raises(SystemExit) as stop:
        mod._refuse_to_mix(conn, 5)
    assert "NOT fingerprinted" in str(stop.value) and hybrid.TRAINED_CLAP_WEIGHTS in str(stop.value)
