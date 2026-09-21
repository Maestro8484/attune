"""Locks in the settings-file rescue in attune/src/config.py:
  * load() tells MISSING, UNREADABLE, CORRUPT and OK apart, because they are
    four different events and used to be handled as one -- everything fell
    through to defaults and the next save() wrote those defaults over the
    top, silently destroying the library path, the export roots and the Plex
    key on any machine whose settings.json had been truncated;
  * a corrupt file is RENAMED aside rather than deleted, so the values are
    still recoverable by hand;
  * two corruptions in the same second produce two separate rescue files
    rather than the second overwriting the first -- the timestamp alone is
    only one-second resolution, so the name carries a counter as well;
  * update() REFUSES to write over a file it could not read. A settings file
    held for a few seconds by a backup agent or an antivirus scanner at
    launch was otherwise replaced by pure defaults, silently and totally.

Every test redirects APPDATA (and XDG_CONFIG_HOME) at a tmp_path, so nothing
here can reach the operator's own settings file.
"""
from __future__ import annotations
import json
import os

import pytest

import config as cfg


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
    """A throwaway config dir. config.config_dir() reads the environment on every
    call, so redirecting it here is enough -- no module state to reset."""
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    # Pinned as well as APPDATA, and on purpose. Nothing in this file calls
    # export.find_env today, but redirecting APPDATA alone does NOT stop that walk
    # reaching the workspace .env and a real Plex key being copied into the scratch
    # folder (ISSUES.md row 48, measured 2026-09-21). Every config-touching test
    # file pins both, so a test added here later inherits the guarantee instead of
    # discovering the hard way that it never had it.
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("ATTUNE_ENV", str(empty_env))
    # _SAID_CORRUPT is a module global that only silences a repeated print; reset it
    # so each test starts from the same place rather than depending on test order.
    monkeypatch.setattr(cfg, "_SAID_CORRUPT", False, raising=False)
    d = cfg.config_dir()
    os.makedirs(d, exist_ok=True)
    return d


def test_missing_file_is_not_an_error_and_yields_defaults(cfgdir):
    settings, status = cfg.load(with_status=True)
    assert status == "missing"
    assert settings["db_path"] == cfg.DEFAULTS["db_path"]


def test_valid_file_is_read_and_unknown_keys_survive(cfgdir):
    cfg.save({"db_path": "D:/music/mixer.db", "some_future_key": 42})
    settings, status = cfg.load(with_status=True)
    assert status == "ok"
    assert settings["db_path"] == "D:/music/mixer.db"
    assert settings["some_future_key"] == 42, (
        "an unknown key was dropped; a newer build's settings must survive an older "
        "build reading them")


def test_corrupt_json_is_moved_aside_not_deleted(cfgdir):
    path = cfg.settings_path()
    with open(path, "w", encoding="utf-8") as fh:
        fh.write('{"db_path": "D:/music/mixer.db"')      # truncated, as by a power cut

    settings, status = cfg.load(with_status=True)
    assert status == "corrupt"
    assert settings["db_path"] == cfg.DEFAULTS["db_path"]
    assert not os.path.exists(path), "the corrupt file should have been renamed away"

    rescued = [f for f in os.listdir(cfgdir) if ".corrupt-" in f]
    assert len(rescued) == 1, f"expected one rescue file, found {rescued}"
    kept = open(os.path.join(cfgdir, rescued[0]), encoding="utf-8").read()
    assert "D:/music/mixer.db" in kept, "the rescue file must still hold the real values"


def test_json_that_is_not_an_object_counts_as_corrupt(cfgdir):
    path = cfg.settings_path()
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(["not", "a", "settings", "object"], fh)
    _settings, status = cfg.load(with_status=True)
    assert status == "corrupt"
    assert [f for f in os.listdir(cfgdir) if ".corrupt-" in f]


def test_two_corruptions_in_the_same_second_keep_both_copies(cfgdir, monkeypatch):
    """The rescue name is stamped to the second. Two rescues inside one second would
    land on the same name and os.replace would overwrite the FIRST one, throwing away
    the very copy the rescue exists to keep. Time is frozen here so the collision is
    forced rather than hoped for."""
    monkeypatch.setattr(cfg.time, "strftime", lambda _fmt: "20260921-120000")
    path = cfg.settings_path()

    for marker in ("FIRST_VALUE", "SECOND_VALUE"):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write('{"db_path": "' + marker + '"')     # corrupt on purpose
        _settings, status = cfg.load(with_status=True)
        assert status == "corrupt"

    rescued = sorted(f for f in os.listdir(cfgdir) if ".corrupt-" in f)
    assert len(rescued) == 2, f"a rescue file was overwritten; found {rescued}"
    kept = "".join(open(os.path.join(cfgdir, f), encoding="utf-8").read()
                   for f in rescued)
    assert "FIRST_VALUE" in kept and "SECOND_VALUE" in kept


def test_unreadable_file_yields_defaults_and_is_left_exactly_where_it_is(cfgdir,
                                                                        monkeypatch):
    """An unreadable file still HOLDS the values, so unlike a corrupt one it must not
    be moved, and load() must say so rather than reporting an ordinary first run."""
    path = cfg.settings_path()
    cfg.save({"db_path": "D:/music/mixer.db"})
    before = (os.path.getsize(path), os.path.getmtime(path))

    real_open = open

    def refuse(file, *a, **kw):
        if os.path.abspath(str(file)) == os.path.abspath(path):
            raise PermissionError("held by a backup agent")
        return real_open(file, *a, **kw)

    # A context, not monkeypatch.undo(): undo() would also drop the APPDATA redirect
    # this test is standing on.
    with monkeypatch.context() as held:
        held.setattr("builtins.open", refuse)
        settings, status = cfg.load(with_status=True)

    assert status == "unreadable"
    assert settings["db_path"] == cfg.DEFAULTS["db_path"]
    assert os.path.exists(path)
    assert (os.path.getsize(path), os.path.getmtime(path)) == before
    assert not [f for f in os.listdir(cfgdir) if ".corrupt-" in f], (
        "an unreadable file must not be treated as corrupt and moved aside")


def test_update_refuses_to_overwrite_a_file_it_could_not_read(cfgdir, monkeypatch):
    path = cfg.settings_path()
    cfg.save({"db_path": "D:/music/mixer.db", "plex_token": "a-real-key"})
    before = open(path, encoding="utf-8").read()

    real_open = open

    def refuse(file, *a, **kw):
        if os.path.abspath(str(file)) == os.path.abspath(path) and "r" in str(
                kw.get("mode", a[0] if a else "r")):
            raise PermissionError("held by a backup agent")
        return real_open(file, *a, **kw)

    with monkeypatch.context() as held:
        held.setattr("builtins.open", refuse)
        with pytest.raises(cfg.SettingsUnreadable):
            cfg.update({"db_path": "E:/somewhere/else.db"})

    assert open(path, encoding="utf-8").read() == before, (
        "update() wrote over a settings file it could not read; the Plex key and the "
        "library path would both be gone")


def test_update_merges_and_reports_restart_keys(cfgdir):
    cfg.save({"db_path": "D:/music/mixer.db", "playlist_dir": "D:/lists"})
    settings, needs_restart = cfg.update({"db_path": "E:/other.db"})
    assert settings["db_path"] == "E:/other.db"
    assert settings["playlist_dir"] == "D:/lists", "update must merge, not replace"
    assert needs_restart == ["db_path"]

    _settings, needs_restart = cfg.update({"playlist_dir": "D:/lists2"})
    assert needs_restart == [], "playlist_dir does not need a restart"
