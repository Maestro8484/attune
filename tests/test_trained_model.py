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
    conn = sqlite3.connect(dbp)
    old = dict(conn.execute("SELECT path, vec FROM clap"))
    conn.close()

    assert refingerprint.main(["--db", dbp]) == 0         # the plan: nothing changes
    assert says(dbp) == (None, N) and "nothing was changed" in capsys.readouterr().out

    assert refingerprint.main(["--db", dbp, "--yes", "--workers", "2"]) == 0
    backups = [f for f in os.listdir(tmp_path) if "before-refingerprint" in f]
    assert len(backups) == 1
    conn = sqlite3.connect(os.path.join(tmp_path, backups[0]))
    assert dict(conn.execute("SELECT path, vec FROM clap")) == old      # the backup holds the old ones
    assert conn.execute("SELECT value FROM meta WHERE key='clap_weights'").fetchone() is None
    conn.close()
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
    import engine                                         # refused before onnxruntime is asked for
    dbp = build_db(str(tmp_path), fingerprints=True, says=TRAINED)
    with pytest.raises(SystemExit) as stop:
        engine.LearnedEngine(dbp)
    assert "cannot be used on this library" in str(stop.value)


def test_text_search_asks_the_library_which_text_half(tmp_path, monkeypatch):
    import textsearch
    plain = build_db(str(tmp_path / "a"), fingerprints=True)
    trained = build_db(str(tmp_path / "b"), fingerprints=True, says=TRAINED)
    assert textsearch.library_weights(plain) is None
    assert textsearch.library_weights(trained) == TRAINED
    asked = []
    monkeypatch.setattr(textsearch, "load_model",
                        lambda dev=None, weights=None: asked.append(weights) or ("torch", "model", "proc", "dev"))
    monkeypatch.setattr(textsearch, "embed_text", lambda *_a: np.ones(512, np.float32) / np.sqrt(512))
    for dbp in (plain, trained):
        results, _meta, _session = textsearch.search(dbp, "anything", n=3)
        assert len(results) == 3
    assert asked == [None, TRAINED]                       # search() loads the half the library needs


def test_a_backup_that_does_not_hold_the_library_stops_the_way_across(stand_ins, tmp_path, monkeypatch, capsys):
    import refingerprint
    dbp = build_db(str(tmp_path), fingerprints=True)
    monkeypatch.setattr(refingerprint, "backup", lambda _db: None)
    assert refingerprint.main(["--db", dbp, "--yes"]) == 2
    assert says(dbp) == (None, N) and "Nothing was changed" in capsys.readouterr().out


def test_the_backup_holds_what_another_open_connection_has_not_flushed(tmp_path):
    """A cold reader showed a file copy of a library that something else has open can miss its
    newest fingerprints. The backup reads through SQLite, so it holds them."""
    import refingerprint
    dbp = build_db(str(tmp_path), fingerprints=False)
    writer = sqlite3.connect(dbp)
    writer.execute("PRAGMA journal_mode=WAL")
    reader = sqlite3.connect(dbp)
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM tracks").fetchone()          # holds the side file open
    paths = [r[0] for r in writer.execute("SELECT path FROM tracks")]
    for p in paths:
        writer.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)", (p, np.ones(512, np.float32).tobytes(), 512))
    writer.commit()
    dest = refingerprint.backup(dbp)
    reader.close()
    writer.close()
    assert dest is not None
    conn = sqlite3.connect(dest)
    assert conn.execute("SELECT COUNT(*) FROM clap WHERE vec IS NOT NULL").fetchone()[0] == N
    conn.close()


def test_a_model_file_passed_by_path_is_held_to_the_library_too(stand_ins, tmp_path):
    """--onnx with the earlier, unstamped file: a trained-weight library refuses it, and the
    library is left as it was."""
    stand_ins(None)
    dbp = build_db(str(tmp_path), fingerprints=True, says=TRAINED)
    conn = sqlite3.connect(dbp)
    conn.execute("DELETE FROM clap WHERE path LIKE ?", ("%t00.mp3",))
    conn.commit()
    conn.close()
    with pytest.raises(SystemExit) as stop:
        embed_onnx.embed(dbp, workers=1, batch=4, onnx_path="an/earlier/file.onnx")
    assert "NOT fingerprinted" in str(stop.value) and says(dbp) == (TRAINED, N - 1)


@pytest.mark.parametrize("mod", [embed_onnx, embed])
def test_a_library_that_changes_kind_under_a_run_stops_it(mod, tmp_path):
    dbp = build_db(str(tmp_path), fingerprints=False)
    conn = sqlite3.connect(dbp)
    mod._refuse_to_mix(conn, 3)                           # a new library: this run may start
    other = sqlite3.connect(dbp)                          # a second run of another kind marks it first
    other.execute("INSERT INTO meta(key, value) VALUES('clap_weights', 'some_other_checkpoint')")
    other.commit()
    other.close()
    with pytest.raises(SystemExit) as stop:
        mod._mark_library(conn)
    assert "some_other_checkpoint" in str(stop.value)
    conn.close()
    assert says(dbp) == ("some_other_checkpoint", 0)


class _FakeTensor:
    def __init__(self, a):
        self.a = a

    def detach(self):
        return self

    def cpu(self):
        return self

    def numpy(self):
        return self.a


class _NoGrad:
    def __enter__(self):
        return self

    def __exit__(self, *_a):
        return False


class _FakeTorch:
    class cuda:
        @staticmethod
        def is_available():
            return False

    @staticmethod
    def no_grad():
        return _NoGrad()

    @staticmethod
    def is_tensor(x):
        return isinstance(x, _FakeTensor)


class _FakeClap:
    def to(self, _dev):
        return self

    def eval(self):
        return self

    def get_audio_features(self, **inp):
        return _FakeTensor(np.ones((inp["n"], 512), np.float32) / np.sqrt(512))


class _FakeValue:
    def __init__(self, v):
        self.v = v

    def to(self, _dev):
        return self.v


def test_the_torch_stage_marks_a_new_library_too(tmp_path, monkeypatch):
    """embed.py end to end with no torch and no model: a new library is fingerprinted and marked."""
    monkeypatch.setattr(embed, "_import_stack", lambda: (_FakeTorch, None, None, None))
    monkeypatch.setattr(embed, "load_model",
                        lambda *_a: (_FakeClap(), lambda audio, sampling_rate, return_tensors: {"n": _FakeValue(len(audio))}))
    monkeypatch.setattr(embed, "_decode", lambda _lib, _p, _m: ([np.zeros(4, np.float32)] * 3, None))
    dbp = build_db(str(tmp_path), fingerprints=False)
    embed.embed(dbp, workers=2, batch=4)
    assert says(dbp) == (TRAINED, N)
