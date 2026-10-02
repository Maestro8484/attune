"""The fingerprint stage carries the model's trained weights (2026-10-02), and says so.

Locks in:
  1. both fingerprint stages name the trained weights, the same name the engine's sound profile
     looks for;
  2. a library made with the earlier model file (it holds fingerprints and does not say what made
     them) is refused, with the reason and the way across named, and nothing is written to it;
  3. a new library (no fingerprint yet, nothing said) goes ahead, and is marked beside its first
     fingerprint, so the engine gives it the sound profile;
  4. a library that names some other weights is refused;
  5. the model file says which weights it carries, and the bundled file must be the one this build
     expects: the earlier file left in place stops the stage instead of writing its fingerprints
     under the new name;
  6. the download tool and the model's data sheet pin the same file;
  7. the way across (tools/refingerprint.py) changes nothing without --yes, and with it leaves a
     byte-for-byte backup, clears the old fingerprints, marks the library and fingerprints again;
  8. the older learned engine does not load on a library with trained-weight fingerprints;
  9. text search asks the library which text half it needs.

No audio and no model: the decoder, the mel front end and the session are stand-ins. What the real
file does is measured in the development workspace (audit-sonic/trained_model_parity.txt).
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import db as dbm                     # noqa: E402
from features import FEATURE_DIM     # noqa: E402
import embed                         # noqa: E402
import embed_onnx                    # noqa: E402
import hybrid                        # noqa: E402

TRAINED = hybrid.TRAINED_CLAP_WEIGHTS
N = 12


def build_db(dirpath, fingerprints, says=None):
    os.makedirs(dirpath, exist_ok=True)
    dbp = os.path.join(dirpath, "lib.db")
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(11)
    now = int(time.time())
    for i in range(N):
        p = os.path.join(dirpath, f"t{i:02d}.mp3")
        conn.execute("INSERT INTO tracks(path, artist, album, title, genre, year, seconds, bytes, mtime) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (p, f"Artist {i}", "Album", f"Title {i}", "Punk", 1990 + i, 200, 1, now))
        conn.execute("INSERT INTO features(path, vec, dim, error, src_mtime, tempo) VALUES(?,?,?,?,?,?)",
                     (p, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(), FEATURE_DIM, None, now, 100.0 + i))
        if fingerprints:
            v = rng.normal(size=512)
            conn.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)",
                         (p, (v / np.linalg.norm(v)).astype(np.float32).tobytes(), 512))
    if says:
        conn.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('clap_weights', ?)", (says,))
    conn.commit()
    conn.close()
    return dbp


def says(dbp):
    conn = sqlite3.connect(dbp)
    row = conn.execute("SELECT value FROM meta WHERE key='clap_weights'").fetchone()
    n = conn.execute("SELECT COUNT(*) FROM clap WHERE vec IS NOT NULL").fetchone()[0]
    conn.close()
    return (row[0] if row else None), n


class FakeMeta:
    def __init__(self, weights):
        self.custom_metadata_map = {"clap_weights": weights} if weights else {}


class FakeSession:
    """Stands in for the onnxruntime session: unit rows, and the file's own metadata."""
    def __init__(self, weights):
        self._weights = weights

    def get_modelmeta(self):
        return FakeMeta(self._weights)

    def run(self, _names, feed):
        n = feed["mel"].shape[0]
        E = np.ones((n, 512), np.float32)
        return [E / np.linalg.norm(E, axis=1, keepdims=True)]


@pytest.fixture()
def stand_ins(monkeypatch):
    """embed_onnx.embed() with no audio and no model file. Returns a setter for the weights the
    stand-in model file says it carries."""
    box = {"weights": TRAINED}
    monkeypatch.setattr(embed_onnx, "_import_stack", lambda: (None, None))
    monkeypatch.setattr(embed_onnx, "_check_norm_json", lambda: None)
    monkeypatch.setattr(embed_onnx, "_check_model_present", lambda *_a: None)
    monkeypatch.setattr(embed_onnx, "make_session", lambda *_a: FakeSession(box["weights"]))
    monkeypatch.setattr(embed_onnx, "_decode", lambda _lib, _p, _m: ([np.zeros(4, np.float32)] * 3, None))
    monkeypatch.setattr(embed_onnx, "track_mels", lambda *_a: np.zeros((3, 1, 2, 2), np.float32))
    return lambda w: box.__setitem__("weights", w)


@pytest.mark.parametrize("mod", [embed_onnx, embed])
def test_both_stages_name_the_trained_weights(mod):
    assert mod.CLAP_WEIGHTS == TRAINED


@pytest.mark.parametrize("mod", [embed_onnx, embed])
def test_an_earlier_library_is_refused_with_the_reason(mod, tmp_path):
    dbp = build_db(str(tmp_path), fingerprints=True)
    conn = sqlite3.connect(dbp)
    with pytest.raises(SystemExit) as stop:
        mod._refuse_to_mix(conn, 3)
    said = str(stop.value)
    assert "3 new song(s) were NOT fingerprinted" in said
    assert "the original release" in said and TRAINED in said and "refingerprint" in said


@pytest.mark.parametrize("mod", [embed_onnx, embed])
def test_a_new_library_goes_ahead_and_is_marked(mod, tmp_path):
    dbp = build_db(str(tmp_path), fingerprints=False)
    conn = sqlite3.connect(dbp)
    mod._refuse_to_mix(conn, 3)
    mod._mark_library(conn)
    mod._mark_library(conn)                               # once is enough; twice changes nothing
    conn.commit()
    conn.close()
    assert says(dbp) == (TRAINED, 0)


@pytest.mark.parametrize("mod", [embed_onnx, embed])
def test_a_library_of_some_other_kind_is_refused(mod, tmp_path):
    dbp = build_db(str(tmp_path), fingerprints=False, says="some_other_checkpoint")
    conn = sqlite3.connect(dbp)
    with pytest.raises(SystemExit) as stop:
        mod._refuse_to_mix(conn, 1)
    assert "some_other_checkpoint" in str(stop.value)


def test_the_stage_fingerprints_a_new_library_and_the_engine_gives_it_the_sound_profile(stand_ins, tmp_path):
    dbp = build_db(str(tmp_path), fingerprints=False)
    embed_onnx.embed(dbp, workers=2, batch=4)
    assert says(dbp) == (TRAINED, N)
    assert hybrid.HybridEngine(dbp).profile == "sound"


def test_the_stage_writes_nothing_into_an_earlier_library(stand_ins, tmp_path):
    dbp = build_db(str(tmp_path), fingerprints=True)
    conn = sqlite3.connect(dbp)
    conn.execute("DELETE FROM clap WHERE path LIKE '%t00.mp3'")       # one song still to fingerprint
    conn.commit()
    conn.close()
    with pytest.raises(SystemExit) as stop:
        embed_onnx.embed(dbp, workers=1, batch=4)
    assert "NOT fingerprinted" in str(stop.value)
    assert says(dbp) == (None, N - 1)


def test_the_bundled_file_must_be_the_one_this_build_expects(stand_ins, tmp_path):
    stand_ins(None)                                       # the earlier file, left in place
    dbp = build_db(str(tmp_path), fingerprints=False)
    with pytest.raises(SystemExit) as stop:
        embed_onnx.embed(dbp, workers=1, batch=4)
    assert "not the one this build expects" in str(stop.value) and "fetch_model" in str(stop.value)
    assert says(dbp) == (None, 0)


def test_a_model_file_says_which_weights_it_carries():
    assert embed_onnx.model_weights(FakeSession(TRAINED)) == TRAINED
    assert embed_onnx.model_weights(FakeSession(None)) is None
    assert embed_onnx.model_weights(object()) is None
    # a file passed by path is taken for what it says it is; the bundled path is held to the build
    assert embed_onnx._check_model_weights(FakeSession(None), "somewhere/else.onnx") is None
    assert embed_onnx._check_model_weights(FakeSession(TRAINED), embed_onnx.ONNX_PATH) == TRAINED


def test_the_download_tool_and_the_data_sheet_pin_the_same_file():
    import fetch_model
    with open(embed_onnx.NORM_PATH, encoding="utf-8") as fh:
        sheet = json.load(fh)
    dist = sheet["_distribution"]
    assert (dist["sha256"], dist["bytes"], dist["url"]) == (
        fetch_model.MODEL_SHA256, fetch_model.MODEL_BYTES, fetch_model.MODEL_URL)
    assert sheet["weights"] == embed_onnx.CLAP_WEIGHTS == embed.CLAP_WEIGHTS


def test_the_way_across(stand_ins, tmp_path, capsys):
    import refingerprint
    dbp = build_db(str(tmp_path), fingerprints=True)
    with open(dbp, "rb") as fh:
        before = fh.read()
    conn = sqlite3.connect(dbp)
    old = dict(conn.execute("SELECT path, vec FROM clap"))
    conn.close()

    assert refingerprint.main(["--db", dbp]) == 0         # the plan: nothing changes
    assert says(dbp) == (None, N) and "nothing was changed" in capsys.readouterr().out

    assert refingerprint.main(["--db", dbp, "--yes", "--workers", "2"]) == 0
    backups = [f for f in os.listdir(tmp_path) if "before-refingerprint" in f]
    assert len(backups) == 1
    with open(os.path.join(tmp_path, backups[0]), "rb") as fh:
        assert fh.read() == before
    assert says(dbp) == (TRAINED, N)
    conn = sqlite3.connect(dbp)
    new = dict(conn.execute("SELECT path, vec FROM clap"))
    conn.close()
    assert set(new) == set(old) and all(new[p] != old[p] for p in old)
    assert hybrid.HybridEngine(dbp).profile == "sound"

    assert refingerprint.main(["--db", dbp, "--yes"]) == 0  # already across: nothing to do
    assert "Nothing to do" in capsys.readouterr().out


def test_the_way_across_refuses_the_earlier_model_file(stand_ins, tmp_path):
    import refingerprint
    stand_ins(None)
    dbp = build_db(str(tmp_path), fingerprints=True)
    assert refingerprint.main(["--db", dbp, "--yes", "--onnx", "an/earlier/file.onnx"]) == 1
    assert says(dbp) == (None, N)


def test_the_learned_engine_does_not_load_on_trained_fingerprints(tmp_path):
    pytest.importorskip("onnxruntime")
    import engine
    dbp = build_db(str(tmp_path), fingerprints=True, says=TRAINED)
    with pytest.raises(SystemExit) as stop:
        engine.LearnedEngine(dbp)
    assert "cannot be used on this library" in str(stop.value)


def test_text_search_asks_the_library_which_text_half(tmp_path):
    import textsearch
    assert textsearch.library_weights(build_db(str(tmp_path / "a"), fingerprints=True)) is None
    assert textsearch.library_weights(build_db(str(tmp_path / "b"), fingerprints=True, says=TRAINED)) == TRAINED
