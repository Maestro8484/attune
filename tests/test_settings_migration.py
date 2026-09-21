"""Locks in the one-time .env carryover in web/app.py:
  * _migrate_plex_env_once is gated on a REAL stored flag (plex_env_migrated)
    and runs its copy at most once per machine, ever. This is the defect that
    wrote the operator's real Plex key into a scratch settings file on every
    regression-gate run: without the flag, a key cleared in Preferences came
    straight back from .env on the next launch, and every cold start of a
    throwaway config dir copied the secret in again;
  * the flag is set whether or not anything was actually copied, so a machine
    with no .env at all is a no-op that still closes the door;
  * a second call must NOT overwrite a value the user has since changed;
  * when the settings file cannot be read, the flag is NOT written, so the
    carryover simply retries on the next launch rather than being lost;
  * _migrate_env_roots_once is deliberately NOT flag-gated -- it refills an
    empty export root from .env whenever one is empty, because a path is not a
    secret and env still outranks settings.json day to day. That difference is
    intentional and is locked in here so nobody "fixes" one to match the other.

web/app.py is loaded by file path (the same way tests/test_bridge_dest_ok.py
loads bridge.py) and the whole module is skipped if it cannot be imported, so
a machine without flask fails loudly on its own terms rather than here.
APPDATA is redirected at a tmp_path in every test: nothing can reach the
operator's own settings file.
"""
from __future__ import annotations
import importlib.util
import os
import sys

import pytest

import config as cfg

HERE = os.path.dirname(os.path.abspath(__file__))
ATTUNE_ROOT = os.path.dirname(HERE)
APP_PY = os.path.join(ATTUNE_ROOT, "web", "app.py")


@pytest.fixture(scope="module")
def appmod():
    if not os.path.exists(APP_PY):
        pytest.skip(f"web/app.py not found at {APP_PY}")
    web_dir = os.path.dirname(APP_PY)
    added = web_dir not in sys.path
    if added:
        sys.path.insert(0, web_dir)
    try:
        spec = importlib.util.spec_from_file_location("attune_app_under_test", APP_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod
    except Exception as e:          # pragma: no cover - environment-dependent
        pytest.skip(f"web/app.py could not be imported: {type(e).__name__}: {e}")
    finally:
        if added and web_dir in sys.path:
            sys.path.remove(web_dir)


@pytest.fixture
def cfgdir(tmp_path, monkeypatch):
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
    d = cfg.config_dir()
    os.makedirs(d, exist_ok=True)
    return d


ENV_WITH_PLEX = {
    "PLEX_URL": "http://192.168.1.50:32400",
    "PLEX_ACCOUNT_TOKEN": "a-real-looking-key",
    "PLEX_SECTION_KEY": "3",
    "PLEX_MACHINE_ID": "abc123",
    # Invented. An earlier draft of this file used the real server's name, which is a
    # private string in a public repository and exactly what the leak scan exists to
    # stop -- it got past the scan only because that name was not one of the literals
    # the scan loads. Nothing here may be copied from the workspace .env.
    "PLEX_SERVER_NAME": "ExampleServer",
}


def test_plex_carryover_copies_once_and_sets_the_flag(appmod, cfgdir):
    wrote = appmod._migrate_plex_env_once(cfgmod=cfg, settings=cfg.load(),
                                          env_cfg=dict(ENV_WITH_PLEX))
    assert wrote is True
    after = cfg.load()
    assert after["plex_token"] == "a-real-looking-key"
    assert after["plex_url"] == "http://192.168.1.50:32400"
    assert after["plex_env_migrated"] is True


def test_the_flag_stops_it_running_a_second_time(appmod, cfgdir):
    appmod._migrate_plex_env_once(cfg, cfg.load(), dict(ENV_WITH_PLEX))
    wrote_again = appmod._migrate_plex_env_once(cfg, cfg.load(), dict(ENV_WITH_PLEX))
    assert wrote_again is False


def test_a_second_call_does_not_overwrite_a_value_the_user_has_changed(appmod, cfgdir):
    """The whole reason for the flag. Clearing the key in Preferences, or replacing it
    with a new one, must survive the next launch."""
    appmod._migrate_plex_env_once(cfg, cfg.load(), dict(ENV_WITH_PLEX))

    cfg.update({"plex_token": ""})                      # as "Forget this server" does
    appmod._migrate_plex_env_once(cfg, cfg.load(), dict(ENV_WITH_PLEX))
    assert cfg.load()["plex_token"] == "", (
        "a cleared Plex key was handed straight back from .env on the next launch")

    cfg.update({"plex_token": "the-key-the-user-typed"})
    appmod._migrate_plex_env_once(cfg, cfg.load(), dict(ENV_WITH_PLEX))
    assert cfg.load()["plex_token"] == "the-key-the-user-typed"


def test_a_machine_with_no_env_still_closes_the_door(appmod, cfgdir):
    wrote = appmod._migrate_plex_env_once(cfg, cfg.load(), {})
    assert wrote is True
    after = cfg.load()
    assert after["plex_env_migrated"] is True
    assert after["plex_token"] == ""


def test_an_unreadable_settings_file_leaves_the_flag_unset_so_it_retries(appmod,
                                                                        cfgdir,
                                                                        monkeypatch):
    cfg.save({"db_path": "D:/music/mixer.db"})
    path = cfg.settings_path()
    real_open = open

    def refuse(file, *a, **kw):
        if os.path.abspath(str(file)) == os.path.abspath(path):
            raise PermissionError("held by a backup agent")
        return real_open(file, *a, **kw)

    # A context, not monkeypatch.undo(): undo() would also drop the APPDATA redirect
    # this test is standing on, and every assertion below would then read the
    # operator's own settings file instead of the throwaway one.
    with monkeypatch.context() as held:
        held.setattr("builtins.open", refuse)
        wrote = appmod._migrate_plex_env_once(cfg, {}, dict(ENV_WITH_PLEX))

    assert wrote is False
    after = cfg.load()
    assert after["plex_env_migrated"] is False, (
        "the flag was set although nothing could be written, so the carryover would "
        "never happen")
    assert after["db_path"] == "D:/music/mixer.db", (
        "the settings file was overwritten although it could not be read")

    # And now that the file can be read again, it does happen.
    assert appmod._migrate_plex_env_once(cfg, cfg.load(), dict(ENV_WITH_PLEX)) is True
    assert cfg.load()["plex_token"] == "a-real-looking-key"


def test_export_roots_carryover_is_deliberately_not_flag_gated(appmod, cfgdir):
    """A path is not a secret. This one refills an EMPTY root from .env whenever it is
    empty, on purpose, so someone who keeps editing .env by hand sees no change. If
    this ever starts behaving like the Plex one, that is a decision, not a tidy-up."""
    env = {"UNC_LIBRARY_ROOT": "//server/share/music"}
    assert appmod._migrate_env_roots_once(cfg, cfg.load(), env) is True
    assert cfg.load()["unc_library_root"] == "//server/share/music"

    # Already set: a no-op.
    assert appmod._migrate_env_roots_once(cfg, cfg.load(), env) is False

    # Emptied again: refilled, unlike the Plex carryover above.
    cfg.update({"unc_library_root": ""})
    assert appmod._migrate_env_roots_once(cfg, cfg.load(), env) is True
    assert cfg.load()["unc_library_root"] == "//server/share/music"
