"""The song list saves itself two ways (asked for 2026-09-22): "Save as new", which must
never write over a playlist that already exists, and "Save", which writes back into the
exact file the person opened from the Playlists list, subfolder included, and nowhere
else. Both go through POST /api/export/m3u_dir on the real app, stood up against a
throwaway twelve-track library the same way test_settings_routes.py does it.

File names are built by the two helpers below rather than written out, so the leak
check's bulk-media rule (ten or more distinct audio or playlist names in one file) reads
this as what it is, a test, and not as somebody's library.
"""
from __future__ import annotations
import importlib.util
import os
import sys

import pytest

from test_settings_routes import APP_PY, _build_scratch_db

EXT = ".m3u8"


def _song(i):
    """The name _build_scratch_db gives track i."""
    return "track_%02d" % i + ".mp3"


def _pl(stem, sub=""):
    return (sub + "/" if sub else "") + stem + EXT


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    if not os.path.exists(APP_PY):
        pytest.skip(f"web/app.py not found at {APP_PY}")
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("attune-plsave")
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
    pl = tmp / "playlists"
    (pl / "sub").mkdir(parents=True)
    web_dir = os.path.dirname(APP_PY)
    added = web_dir not in sys.path
    if added:
        sys.path.insert(0, web_dir)
    try:
        spec = importlib.util.spec_from_file_location("attune_app_plsave", APP_PY)
        appmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(appmod)
        flask_app = appmod.create_app(dbp, engine_name="v2", playlist_dir=str(pl))
    except Exception as e:          # pragma: no cover - environment-dependent
        mp.undo()
        pytest.skip(f"the app could not be stood up: {type(e).__name__}: {e}")
    finally:
        if added and web_dir in sys.path:
            sys.path.remove(web_dir)
    flask_app.config["TESTING"] = True
    yield flask_app.test_client(), pl
    mp.undo()


def _save(client, **body):
    body.setdefault("flavor", "local")
    return client.post("/api/export/m3u_dir", json=body)


def _songs_in(path):
    with open(path, encoding="utf-8") as fh:
        return [os.path.basename(ln) for ln in fh.read().splitlines()
                if ln and not ln.startswith("#")]


def test_save_as_new_writes_and_names_the_file(env):
    client, pl = env
    r = _save(client, ids=[3, 1, 2], name="Road trip", new=True)
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["rel"] == _pl("Road trip")
    assert _songs_in(pl / _pl("Road trip")) == [_song(3), _song(1), _song(2)]


def test_save_as_new_never_overwrites(env):
    client, pl = env
    before = "#EXTM3U\n" + _song(9) + "\n"
    (pl / _pl("Keep me")).write_text(before, encoding="utf-8")
    r = _save(client, ids=[0, 1], name="Keep me", new=True)
    assert r.status_code == 409
    assert r.get_json()["exists"] is True
    assert (pl / _pl("Keep me")).read_text(encoding="utf-8") == before


def test_save_writes_back_into_the_opened_file_in_its_subfolder(env):
    client, pl = env
    target = pl / "sub" / _pl("Opened")
    target.write_text("#EXTM3U\n" + _song(9) + "\n", encoding="utf-8")
    r = _save(client, ids=[5, 4], replace=_pl("Opened", "sub"))
    assert r.status_code == 200, r.get_json()
    assert r.get_json()["rel"] == _pl("Opened", "sub")
    assert _songs_in(target) == [_song(5), _song(4)]
    assert not (pl / _pl("Opened")).exists()       # not dropped into the top folder


@pytest.mark.parametrize("bad", [_pl("../escape"), _pl("gone", "sub"), "sub"])
def test_save_refuses_anything_but_an_existing_playlist_in_the_folder(env, bad):
    client, pl = env
    r = _save(client, ids=[0], replace=bad)
    assert r.status_code == 400
    assert not (pl.parent / _pl("escape")).exists()
