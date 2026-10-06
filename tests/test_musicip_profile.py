"""The MusicIP profile (2026-10-06, TODO.md row 62): the fingerprint through a head taught by
MusicIP Mixer's own lists, selectable by name, never chosen for a library.

Locks in:
  1. a library of the earlier fingerprint kind refuses the profile, with the reason (the learned
     engine's own rule: a head taught on one kind of fingerprint must not score the other);
  2. a trained-weight library still gets the sound profile unless the MusicIP profile is asked
     for by name, however many head files sit in the models folder;
  3. asked for, the profile scores by the head alone: one ingredient, so the ranking is the
     head's cosine ranking, and the app is told the profile's name;
  4. the profile needs its head file and names it when it is missing;
  5. the fit line the app's switch draws for the profile is its own;
  6. the app's --profile flag reaches the engine: the stats and the mix say so;
  7. the head file that ships, when it is there, is the shape the engine reads and says what
     made it and what it was taught by.

Synthetic library and head from test_sound_profile (thirty tracks, unique artists).
"""
from __future__ import annotations
import importlib.util
import os
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import hybrid                                              # noqa: E402
from test_sound_profile import build_db, write_head, N     # noqa: E402


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


def test_a_library_of_the_earlier_kind_refuses_the_profile(plain, head):
    with pytest.raises(ValueError) as err:
        hybrid.HybridEngine(plain, profile="musicip", head_file=head)
    assert "trained weights" in str(err.value) and "earlier kind" in str(err.value)


def test_a_trained_library_never_gets_the_profile_unasked(trained, head, monkeypatch):
    monkeypatch.setattr(hybrid, "MUSICIP_HEAD_FILE", head)
    e = hybrid.HybridEngine(trained, head_file=head)
    assert e.profile == "sound"
    assert hybrid.HybridEngine(trained, head_file=head, profile="v2").profile == "v2"


def test_asked_for_the_profile_scores_by_the_head_alone(trained, head):
    e = hybrid.HybridEngine(trained, profile="musicip", head_file=head)
    assert e.profile == "musicip"
    assert (e.fusion, e.clap_space, e.family_credit) == ("z", "head", 0.0)
    assert e.w["clap"] == 1.0
    assert all(e.w[k] == 0.0 for k in ("lib", "genre", "bpm", "era", "key", "earfeel", "artist"))
    M = e._clap_matrix("head")
    for si in (0, 7, N - 1):
        by_head = np.argsort(-(M @ M[si]))
        by_score = np.argsort(-e._score(si))
        assert by_head.tolist() == by_score.tolist()


def test_the_profile_needs_its_head_file(trained, tmp_path):
    missing = str(tmp_path / "no_such_head.npz")
    with pytest.raises(ValueError) as err:
        hybrid.HybridEngine(trained, profile="musicip", head_file=missing)
    assert "no_such_head.npz" in str(err.value)


def test_the_fit_line_is_the_profiles_own(trained, head):
    e = hybrid.HybridEngine(trained, profile="musicip", head_file=head)
    assert (e.fit_line, e.clap_line) == (hybrid.FIT_LINE_MUSICIP, hybrid.CLAP_LINE_TRAINED)
    assert 0.0 < hybrid.FIT_LINE_MUSICIP < 1.0


def test_the_apps_profile_flag_reaches_the_engine(tmp_path, monkeypatch):
    """--profile musicip: the stats name the profile and its line, the recipe names the head,
    and a mix with the switch on draws the profile's line."""
    app_py = os.path.join(ROOT, "web", "app.py")
    cfg_home = tmp_path / "config"
    cfg_home.mkdir()
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("APPDATA", str(cfg_home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(cfg_home))
    monkeypatch.setenv("ATTUNE_ENV", str(empty_env))
    monkeypatch.syspath_prepend(os.path.dirname(app_py))
    d = tmp_path / "lib"
    d.mkdir()
    dbp = build_db(str(d), trained=True)
    hd = tmp_path / "head"
    hd.mkdir()
    head = write_head(str(hd))
    try:
        spec = importlib.util.spec_from_file_location("attune_app_musicip_profile", app_py)
        appmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(appmod)
        hy = appmod._load_hybrid()
        hy.MUSICIP_HEAD_FILE = head                      # the app's own copy of the module
        monkeypatch.setattr(appmod, "_load_hybrid", lambda: hy)
        client = appmod.create_app(dbp, engine_name="v2", playlist_dir=str(tmp_path),
                                   profile="musicip").test_client()
    except Exception as e:          # pragma: no cover - environment-dependent
        pytest.skip(f"the app could not be stood up: {type(e).__name__}: {e}")
    st = client.get("/api/lib/stats").get_json()
    assert st["profile"] == "musicip"
    assert st["fit_line"] == hybrid.FIT_LINE_MUSICIP
    assert st["weights"]["clap"] == 1.0 and st["weights"]["era"] == 0.0 and st["weights"]["genre"] == 0.0
    m = client.get("/api/mix?i=0&size=20&max=1").get_json()
    assert m["stop"]["line"] == hybrid.FIT_LINE_MUSICIP
    labels = [t["label"] for t in (m.get("trace") or {}).get("recipe", {}).get("terms", [])]
    assert any("MusicIP-taught" in lab for lab in labels), labels


def write_lib_head(dirpath, hidden=16, out=8):
    """A head that takes the 79 descriptors beside the fingerprint, with the z-score it was
    trained with written into the file."""
    from features import FEATURE_DIM
    rng = np.random.default_rng(9)
    p = os.path.join(dirpath, "lib_head.npz")
    np.savez(p, mean=(0.01 * rng.normal(size=512)).astype(np.float32),
             w1=rng.normal(size=(hidden, 512 + FEATURE_DIM)).astype(np.float16),
             b1=rng.normal(size=hidden).astype(np.float16),
             w2=rng.normal(size=(out, hidden)).astype(np.float16), b2=rng.normal(size=out).astype(np.float16),
             lib_mu=rng.normal(size=FEATURE_DIM).astype(np.float32),
             lib_sd=(1.0 + rng.random(size=FEATURE_DIM)).astype(np.float32), lib_clip=np.float32(6.0))
    return p


def test_a_head_that_takes_the_descriptors_is_read_and_run(trained, tmp_path):
    """The MusicIP-taught head's form: the descriptors, z-scored with the head's own stats and
    clipped, sit beside the centered fingerprint. Numpy alone, the formula written out here."""
    from features import FEATURE_DIM
    path = write_lib_head(str(tmp_path))
    h = hybrid._load_head(path)
    assert h is not None and h["w1"].shape[1] == 512 + FEATURE_DIM
    e = hybrid.HybridEngine(trained, profile="musicip", head_file=path)
    M = e._clap_matrix("head")
    assert M.shape == (N, 8)
    assert np.allclose(np.linalg.norm(M, axis=1), 1.0, atol=1e-5)
    Xc = e.X.astype(np.float32) - h["mean"]
    Xc /= np.linalg.norm(Xc, axis=1, keepdims=True)
    LZ = np.clip((e._lib_raw - h["lib_mu"]) / h["lib_sd"], -6.0, 6.0)
    a = np.hstack([Xc, LZ]) @ h["w1"].T + h["b1"]
    a = 0.5 * a * (1.0 + np.tanh(0.7978845608028654 * (a + 0.044715 * a ** 3)))
    y = a @ h["w2"].T + h["b2"]
    y /= np.linalg.norm(y, axis=1, keepdims=True)
    assert np.allclose(M, y, atol=1e-4)
    # without the descriptors the head cannot run, and says so
    with pytest.raises(ValueError):
        hybrid._apply_head(h, e.X)
    # a file whose widths do not add up is not a head
    bad = str(tmp_path / "bad.npz")
    with np.load(path) as f:
        d = dict(f)
    d["lib_mu"] = d["lib_mu"][:-1]
    d["lib_sd"] = d["lib_sd"][:-1]
    np.savez(bad, **d)
    assert hybrid._load_head(bad) is None


def test_the_shipped_head_file_is_the_shape_the_engine_reads():
    if not os.path.exists(hybrid.MUSICIP_HEAD_FILE):
        pytest.skip("the MusicIP-taught head has not been written to the models folder")
    h = hybrid._load_head(hybrid.MUSICIP_HEAD_FILE)
    assert h is not None and h["mean"].shape == (512,)
    with np.load(hybrid.MUSICIP_HEAD_FILE, allow_pickle=False) as f:
        assert str(f["made_for"]) == hybrid.TRAINED_CLAP_WEIGHTS
        assert "MusicIP" in str(f["taught_by"])
        inputs = str(f["inputs"])
    assert inputs in ("clap", "clap+lib")
    rng = np.random.default_rng(3)
    X = rng.normal(size=(5, 512)).astype(np.float32)
    X /= np.linalg.norm(X, axis=1, keepdims=True)
    L = rng.normal(size=(5, 79)).astype(np.float32) if inputs == "clap+lib" else None
    Y = hybrid._apply_head(h, X, L)
    assert Y.shape == (5, h["w2"].shape[0])
    assert np.allclose(np.linalg.norm(Y, axis=1), 1.0, atol=1e-5)
