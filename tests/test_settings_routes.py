"""Locks in two contracts on the REAL routes web/app.py registers, by standing the
app up against a throwaway 12-track database and calling it through Flask's test
client:

  * POST /api/settings and POST /api/export/plex refuse a caller from another
    machine with 403. The guard is an inline `request.remote_addr` check
    copy-pasted at eight or more sites in web/app.py, and a copy-pasted guard is
    one a new route can silently forget: measured 2026-09-21, seventeen of the
    app's thirty-six POST routes had forgotten it. These two are the ones the
    Plex work added, and they are the two that write a credential and read the
    library, so they are the two locked in here. See ISSUES.md row 4 for the
    other fifteen and for why the fix is a decorator rather than a ninth copy.

  * Saving settings with a BLANK Plex token must not erase a stored one. A blank
    field in a form means "I did not type anything", never "delete it" -- and
    GET /api/settings deliberately never returns the real token, so the field is
    blank every single time the window opens. Without this, an ordinary save
    wiped a working key.

Safety, and this matters more than it looks:
  * APPDATA and XDG_CONFIG_HOME are redirected at a tmp_path, so the operator's
    own settings file is never opened.
  * ATTUNE_ENV is pointed at an EMPTY file. Redirecting APPDATA alone is not
    enough: export.find_env walks up from the working directory and adopts the
    workspace .env, which holds a real Plex key, and the one-time carryover then
    writes it into whatever config directory the process is using. Measured
    2026-09-21 -- the key landed in a scratch folder. ISSUES.md row 48.
  * The database is built from scratch in tmp_path. Nothing here reads the
    production library or any real audio file.
"""
from __future__ import annotations
import importlib.util
import json
import os
import sys
import time

import numpy as np
import pytest

import db as dbm
from features import FEATURE_DIM

HERE = os.path.dirname(os.path.abspath(__file__))
ATTUNE_ROOT = os.path.dirname(HERE)
APP_PY = os.path.join(ATTUNE_ROOT, "web", "app.py")

FOREIGN = {"REMOTE_ADDR": "10.0.0.5"}
LOCAL = {"REMOTE_ADDR": "127.0.0.1"}


def _build_scratch_db(dirpath):
    """A tiny but real library: twelve tracks with features and CLAP vectors, so the
    engine bundle builds and the routes have a pool to answer about."""
    dbp = os.path.join(dirpath, "scratch.db")
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(7)
    now = int(time.time())
    for i in range(12):
        path = os.path.join(dirpath, f"track_{i:02d}.mp3")
        with open(path, "wb") as fh:
            fh.write(b"not really audio")
        conn.execute(
            "INSERT INTO tracks(path, artist, album, title, genre, year, seconds, "
            "bytes, mtime) VALUES(?,?,?,?,?,?,?,?,?)",
            (path, f"Artist {i % 4}", f"Album {i % 3}", f"Title {i}", "Rock",
             2000 + i, 200 + i, 4096, now))
        conn.execute(
            "INSERT INTO features(path, vec, dim, error, src_mtime) VALUES(?,?,?,?,?)",
            (path, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(),
             FEATURE_DIM, None, now))
        conn.execute(
            "INSERT INTO clap(path, vec, dim) VALUES(?,?,?)",
            (path, rng.normal(size=512).astype(np.float32).tobytes(), 512))
    conn.commit()
    conn.close()
    return dbp


@pytest.fixture(scope="module")
def client(tmp_path_factory):
    if not os.path.exists(APP_PY):
        pytest.skip(f"web/app.py not found at {APP_PY}")

    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("attune-routes")
    cfg_home = tmp / "config"
    cfg_home.mkdir()
    empty_env = tmp / "empty.env"
    empty_env.write_text("", encoding="utf-8")

    mp.setenv("APPDATA", str(cfg_home))
    mp.setenv("XDG_CONFIG_HOME", str(cfg_home))
    mp.setenv("ATTUNE_ENV", str(empty_env))

    lib = tmp / "library"
    lib.mkdir()
    dbp = _build_scratch_db(str(lib))

    web_dir = os.path.dirname(APP_PY)
    added = web_dir not in sys.path
    if added:
        sys.path.insert(0, web_dir)
    try:
        spec = importlib.util.spec_from_file_location("attune_app_routes", APP_PY)
        appmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(appmod)
        flask_app = appmod.create_app(dbp, engine_name="v2", playlist_dir=str(tmp))
    except Exception as e:          # pragma: no cover - environment-dependent
        mp.undo()
        pytest.skip(f"the app could not be stood up: {type(e).__name__}: {e}")
    finally:
        if added and web_dir in sys.path:
            sys.path.remove(web_dir)

    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    c._attune_config_dir = str(cfg_home / "Attune")      # for the tests below
    yield c
    mp.undo()


def _settings_file(client):
    return os.path.join(client._attune_config_dir, "settings.json")


def _stored(client):
    with open(_settings_file(client), encoding="utf-8") as fh:
        return json.load(fh)


# --------------------------------------------------------------- loopback refusals

def test_settings_refuses_a_caller_from_another_machine(client):
    r = client.post("/api/settings", json={"playlist_name_template": "hijacked-{seed}"},
                    environ_base=FOREIGN)
    assert r.status_code == 403
    assert _stored(client).get("playlist_name_template") != "hijacked-{seed}", (
        "the refusal was reported but the write happened anyway")


def test_export_plex_refuses_a_caller_from_another_machine(client):
    r = client.post("/api/export/plex", json={"i": 0, "size": 5}, environ_base=FOREIGN)
    assert r.status_code == 403


def test_the_same_two_routes_answer_the_machine_they_run_on(client):
    """A control, so a guard that refuses EVERYBODY cannot pass the two tests above."""
    assert client.post("/api/settings", json={}, environ_base=LOCAL).status_code == 200
    # /api/export/plex from loopback gets past the guard and fails later on its own
    # terms (no Plex server is configured here), which is the point: not a 403.
    assert client.post("/api/export/plex", json={"i": 0, "size": 5},
                       environ_base=LOCAL).status_code != 403


def test_ipv6_loopback_is_also_the_machine_itself(client):
    assert client.post("/api/settings", json={},
                       environ_base={"REMOTE_ADDR": "::1"}).status_code == 200


# ------------------------------------------------------------- the blank-token save

def test_a_blank_token_does_not_erase_a_stored_one(client):
    saved = client.post("/api/settings", json={"plex_token": "a-real-looking-key"},
                        environ_base=LOCAL)
    assert saved.status_code == 200
    assert _stored(client)["plex_token"] == "a-real-looking-key"

    # What the Preferences window actually sends on an ordinary save: the token field
    # is blank, because GET never filled it in.
    again = client.post("/api/settings",
                        json={"plex_token": "", "playlist_name_template": "like-{seed}"},
                        environ_base=LOCAL)
    assert again.status_code == 200
    assert _stored(client)["plex_token"] == "a-real-looking-key", (
        "an ordinary save with a blank field wiped a working Plex key")
    assert _stored(client)["playlist_name_template"] == "like-{seed}", (
        "the rest of the save was dropped along with the blank token")


def test_a_non_blank_token_does_replace_the_stored_one(client):
    client.post("/api/settings", json={"plex_token": "first-key"}, environ_base=LOCAL)
    client.post("/api/settings", json={"plex_token": "second-key"}, environ_base=LOCAL)
    assert _stored(client)["plex_token"] == "second-key"


def test_get_settings_never_hands_the_token_back(client):
    client.post("/api/settings", json={"plex_token": "a-real-looking-key"},
                environ_base=LOCAL)
    body = client.get("/api/settings", environ_base=LOCAL).get_data(as_text=True)
    assert "a-real-looking-key" not in body, (
        "the Plex key was sent to the browser; the blank-field contract above depends "
        "on it never being there")


def test_a_cold_start_did_not_copy_a_real_key_from_the_workspace_env(client):
    """ISSUES.md row 48. Redirecting APPDATA alone does not stop export.find_env
    walking up to the workspace .env and the carryover writing a real Plex key into
    the scratch folder. This asserts the ATTUNE_ENV pin above actually holds."""
    assert os.environ.get("ATTUNE_ENV"), "the test fixture stopped pinning ATTUNE_ENV"
    assert os.path.getsize(os.environ["ATTUNE_ENV"]) == 0
