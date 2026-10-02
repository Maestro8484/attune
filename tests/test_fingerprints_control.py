"""The window's control for bringing a library made by an earlier version across (TODO row 49).

Preferences, Library shows a "Sound fingerprints" row only when there is something to do. The
server side is three things, and these checks hold each:

  1. fingerprint_state() tells the three kinds of library apart (earlier, current, empty) and
     counts what the job would do;
  2. the two routes: state is readable, renew starts ONE stage, the analyzer's fingerprint
     stage with --start-over, refuses when a scan is running, and refuses when there is nothing
     to do;
  3. embed_onnx.start_over(), which is what that stage runs first: for an earlier library it
     copies, clears and marks; for a library already of this build's kind it touches nothing,
     so a crossing that was stopped is simply carried on;
  4. a scan that reads new songs into an earlier library ends with a sentence saying where the
     control is, not "embed failed (rc=1)".

No audio and no model: the stage itself is replaced where a test would otherwise start it.
"""
from __future__ import annotations
import importlib.util
import os
import sqlite3
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

import embed_onnx                                                     # noqa: E402
from test_settings_routes import client                              # noqa: E402,F401  (fixture)
from test_trained_model import N, TRAINED, build_db, says, stand_ins  # noqa: E402,F401  (fixture)

LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


def _scanjob():
    spec = importlib.util.spec_from_file_location("attune_scanjob_test", os.path.join(ROOT, "web", "scanjob.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _drop(dbp, n):
    conn = sqlite3.connect(dbp)
    conn.execute("DELETE FROM clap WHERE path IN (SELECT path FROM clap ORDER BY path LIMIT ?)", (n,))
    conn.commit()
    conn.close()


def test_the_three_kinds_of_library_are_told_apart(tmp_path):
    sj = _scanjob()
    earlier = sj.fingerprint_state(build_db(str(tmp_path / "a"), fingerprints=True), TRAINED)
    assert (earlier["kind"], earlier["songs"], earlier["fingerprints"], earlier["todo"]) == ("earlier", N, N, N)
    assert earlier["est_seconds"] == int(N * sj.FINGERPRINT_SECONDS_A_SONG)

    cur = build_db(str(tmp_path / "b"), fingerprints=True, says=TRAINED)
    assert sj.fingerprint_state(cur, TRAINED)["kind"] == "current"
    assert sj.fingerprint_state(cur, TRAINED)["todo"] == 0
    _drop(cur, 3)                                         # a crossing that was stopped
    part = sj.fingerprint_state(cur, TRAINED)
    assert (part["kind"], part["waiting"], part["todo"]) == ("current", 3, 3)

    empty = sj.fingerprint_state(build_db(str(tmp_path / "c"), fingerprints=False), TRAINED)
    assert (empty["kind"], empty["fingerprints"], empty["todo"]) == ("empty", 0, N)

    other = sj.fingerprint_state(build_db(str(tmp_path / "d"), fingerprints=False, says="another"), TRAINED)
    assert other["kind"] == "earlier"                     # says a kind this build does not make
    assert sj.fingerprint_state(str(tmp_path / "nothing.db"), TRAINED)["kind"] == "empty"


def test_the_state_route_answers(client):  # noqa: F811
    j = client.get("/api/fingerprints/state").get_json()
    assert j["ok"] and j["kind"] in ("earlier", "current", "empty")
    assert j["running"] is False and j["todo"] >= 0 and j["seconds_a_song"] > 0


def test_renew_starts_the_fingerprint_stage_and_nothing_else(client, monkeypatch):  # noqa: F811
    job = client.application.extensions["attune_scan_job"]
    ran = []
    monkeypatch.setattr(job, "_exec", lambda name, argv: ran.append((name, list(argv))) or 0)
    state = client.get("/api/fingerprints/state").get_json()
    r = client.post("/api/fingerprints/renew", json={}, environ_base=LOCAL)
    if not state["todo"]:
        assert r.status_code == 400 and "nothing to do" in r.get_json()["error"]
        return
    assert r.status_code == 200 and r.get_json()["todo"] == state["todo"]
    for _ in range(100):
        if not job.running:
            break
        time.sleep(0.05)
    assert not job.running and job.kind == "fingerprints"
    assert len(ran) == 1 and ran[0][0] == "fingerprints"
    argv = ran[0][1]
    assert argv[-1] == "--start-over" and any("embed_onnx" in a for a in argv) and job.db_path in argv
    assert client.get("/api/scan/status").get_json()["kind"] == "fingerprints"


def test_renew_waits_its_turn_behind_a_scan(client):  # noqa: F811
    job = client.application.extensions["attune_scan_job"]
    if not client.get("/api/fingerprints/state").get_json()["todo"]:
        pytest.skip("the fixture library has nothing to fingerprint")
    job.running = True
    try:
        r = client.post("/api/fingerprints/renew", json={}, environ_base=LOCAL)
    finally:
        job.running = False
    assert r.status_code == 409 and "scan is running" in r.get_json()["error"]


def test_renew_is_only_for_this_machine(client):  # noqa: F811
    r = client.post("/api/fingerprints/renew", json={}, environ_base={"REMOTE_ADDR": "192.0.2.7"})
    assert r.status_code == 403


def test_start_over_copies_clears_and_marks_an_earlier_library(stand_ins, tmp_path, capsys):  # noqa: F811
    dbp = build_db(str(tmp_path), fingerprints=True)
    conn = sqlite3.connect(dbp)
    old = dict(conn.execute("SELECT path, vec FROM clap"))
    conn.close()
    embed_onnx.start_over(dbp)
    said = capsys.readouterr().out
    assert "copy of the library made first" in said and f"cleared {N}" in said
    assert says(dbp) == (TRAINED, 0)
    copies = [f for f in os.listdir(tmp_path) if "before-refingerprint" in f]
    assert len(copies) == 1
    conn = sqlite3.connect(os.path.join(tmp_path, copies[0]))
    assert dict(conn.execute("SELECT path, vec FROM clap")) == old
    conn.close()
    embed_onnx.embed(dbp, workers=2, batch=4)             # the run that follows does every song
    assert says(dbp) == (TRAINED, N)


def test_start_over_leaves_a_library_of_this_kind_alone(stand_ins, tmp_path, capsys):  # noqa: F811
    dbp = build_db(str(tmp_path), fingerprints=True, says=TRAINED)
    _drop(dbp, 4)
    embed_onnx.start_over(dbp)
    assert "carrying on with 4 song(s)" in capsys.readouterr().out
    assert says(dbp) == (TRAINED, N - 4)
    assert not [f for f in os.listdir(tmp_path) if "before-refingerprint" in f]
    fresh = build_db(str(tmp_path / "fresh"), fingerprints=False)
    embed_onnx.start_over(fresh)                          # a new library: nothing to clear either
    assert says(fresh) == (None, 0)


def test_start_over_clears_nothing_when_the_model_file_is_the_earlier_one(stand_ins, tmp_path):  # noqa: F811
    stand_ins(None)
    dbp = build_db(str(tmp_path), fingerprints=True)
    with pytest.raises(SystemExit):
        embed_onnx.start_over(dbp)                        # the bundled path, holding the earlier file
    with pytest.raises(SystemExit) as stop:
        embed_onnx.start_over(dbp, "an/earlier/file.onnx")
    assert "nothing" in str(stop.value) and says(dbp) == (None, N)


def test_a_scan_into_an_earlier_library_says_where_the_control_is(tmp_path):
    sj = _scanjob()
    job = sj.ScanJob(build_db(str(tmp_path), fingerprints=True))

    def fake_exec(name, argv):
        if name.startswith("embed"):
            job.lines.append("3 new song(s) were NOT fingerprinted. This library's fingerprints were made with ...")
            return 1
        return 0
    job._exec = fake_exec
    job.running = True
    job._run([], "", ())
    assert "Sound fingerprints" in job.error and "rc=" not in job.error
    assert job.running is False
