"""Locks in the `dest_ok` destination-allowlist bypass fixes in
attune/bridge/bridge.py (UNC, device-namespace, forward-slash, relative-path,
prefix-collision, and system-directory / persistence-path bypasses).

bridge.py is loaded BY FILE PATH (never edited) via importlib. It requires a
config.json next to it (see config.example.json) and, at import time, either
reads `library_json` from that config or reaches out over the network to a
running MusicIP instance to pull the catalog. Neither attune/bridge/config.json
nor a running MusicIP Mixer is guaranteed to exist in a test environment, so:

  * if attune/bridge/config.json already exists, we leave it alone (never
    overwrite a real user config) and instead point the import at nothing --
    the test session just uses it as-is via a plain import attempt.
  * if it does NOT exist, this suite creates a throwaway one (pointing
    `library_json` at a tiny fixture file so import never touches the
    network), imports bridge.py, and removes the throwaway config.json again
    in teardown so no stray file is left in the repo.
  * if importing bridge.py fails for ANY reason (missing config, missing
    flask/mutagen, network hang, etc.) the whole module is SKIPPED with a
    clear reason, per the task's explicit escape hatch -- these tests never
    touch or create files outside attune/tests/ except for this one
    intentionally-cleaned-up config.json.
"""
from __future__ import annotations
import importlib.util
import json
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ATTUNE_ROOT = os.path.dirname(HERE)
BRIDGE_DIR = os.path.join(ATTUNE_ROOT, "bridge")
BRIDGE_PY = os.path.join(BRIDGE_DIR, "bridge.py")
CONFIG_JSON = os.path.join(BRIDGE_DIR, "config.json")


@pytest.fixture(scope="module")
def bridge_mod(tmp_path_factory):
    if not os.path.exists(BRIDGE_PY):
        pytest.skip(f"bridge.py not found at {BRIDGE_PY}")

    created_config = False
    if not os.path.exists(CONFIG_JSON):
        # Build a throwaway config that never touches the network: a tiny
        # library_json fixture backs the catalog load instead of hitting
        # MIP's /api/songs endpoint.
        work = tmp_path_factory.mktemp("bridge_cfg")
        lib_json = work / "library.json"
        lib_json.write_text(json.dumps([
            {"file": "/lib/track1.mp3", "artist": "Artist A", "name": "Track 1",
             "album": "Album", "genre": "rock", "year": 2001, "seconds": 200},
        ]), encoding="utf-8")
        cfg = {
            "mip_url": "http://127.0.0.1:1",   # unused; library_json backs the catalog
            "port": 8765,
            "library_json": str(lib_json),
            "export_dir": str(work / "playlists"),
            "read_path_map": [],
            "m3u_path_map": [],
        }
        try:
            with open(CONFIG_JSON, "w", encoding="utf-8") as fh:
                json.dump(cfg, fh)
            created_config = True
        except OSError as e:
            pytest.skip(f"could not create throwaway {CONFIG_JSON}: {e}")

    try:
        spec = importlib.util.spec_from_file_location("bridge_under_test", BRIDGE_PY)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    except BaseException as e:  # SystemExit on missing config, ImportError, etc.
        pytest.skip(f"could not import attune/bridge/bridge.py: {type(e).__name__}: {e}")
    finally:
        if created_config and os.path.exists(CONFIG_JSON):
            os.remove(CONFIG_JSON)

    return mod


@pytest.fixture()
def allowed_root(tmp_path):
    root = tmp_path / "AllowedExportRoot"
    root.mkdir()
    return root


@pytest.fixture(autouse=True)
def _no_removable_drives(bridge_mod, allowed_root, monkeypatch):
    """Isolate dest_ok from whatever removable drives happen to be plugged
    into the machine running the tests, and pin the only allowed root to a
    tmp_path so tests are hermetic."""
    monkeypatch.setattr(bridge_mod, "_removable_roots", lambda: [])
    monkeypatch.setattr(bridge_mod, "DEVICE_EXPORT_ROOTS", [str(allowed_root)])


# ---------------------------------------------------------------------------
# DENY vectors -- each one is a bypass this fix specifically closes.
# ---------------------------------------------------------------------------

def test_deny_unc_path(bridge_mod):
    unc = chr(92) * 2 + "server" + chr(92) + "share"   # \\server\share
    ok, reason = bridge_mod.dest_ok(unc)
    assert ok is False
    assert "UNC" in reason or "network" in reason


def test_deny_device_namespace(bridge_mod):
    devns = chr(92) * 2 + "?" + chr(92) + "C:"   # \\?\C:
    ok, reason = bridge_mod.dest_ok(devns)
    assert ok is False


def test_deny_forward_slash_unc(bridge_mod):
    ok, reason = bridge_mod.dest_ok("//x/y")
    assert ok is False


def test_deny_system_directory(bridge_mod):
    ok, reason = bridge_mod.dest_ok("C:" + chr(92) + "Windows" + chr(92) + "System32")
    assert ok is False


def test_deny_startup_persistence_path(bridge_mod):
    startup = (
        "C:" + chr(92) + "Users" + chr(92) + "Test" + chr(92) + "AppData" + chr(92)
        + "Roaming" + chr(92) + "Microsoft" + chr(92) + "Windows" + chr(92)
        + "Start Menu" + chr(92) + "Programs" + chr(92) + "Startup"
    )
    ok, reason = bridge_mod.dest_ok(startup)
    assert ok is False


def test_deny_relative_path(bridge_mod):
    ok, reason = bridge_mod.dest_ok("Music" + chr(92) + "Mix")
    assert ok is False
    assert "absolute" in reason


def test_deny_empty_destination(bridge_mod):
    ok, reason = bridge_mod.dest_ok("")
    assert ok is False


def test_deny_prefix_collision_sibling(bridge_mod, allowed_root):
    """A sibling directory that merely shares a string PREFIX with the
    allowed root (e.g. allowed root 'Export' vs sibling 'Export_evil') must
    NOT be treated as "under" the allowed root."""
    sibling = str(allowed_root) + "_evil"
    ok, reason = bridge_mod.dest_ok(sibling)
    assert ok is False


# ---------------------------------------------------------------------------
# ALLOW vectors -- the legitimate paths must still work.
# ---------------------------------------------------------------------------

def test_allow_root_itself(bridge_mod, allowed_root):
    ok, reason = bridge_mod.dest_ok(str(allowed_root))
    assert ok is True
    assert reason is None


def test_allow_subdirectory_of_root(bridge_mod, allowed_root):
    ok, reason = bridge_mod.dest_ok(str(allowed_root / "Music" / "RoadTrip"))
    assert ok is True
    assert reason is None
