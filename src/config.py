"""Attune settings — the ONE config source of truth (HANDOFF §4 / TRAJECTORY §2).

A JSON file in %APPDATA%\\Attune\\settings.json (or $XDG_CONFIG_HOME/attune on
non-Windows), read by the desktop app, the LAN server, and the analyzer alike.
Precedence when a value is asked for: CLI arg > environment > settings.json > default.
CLI/env never write back; only explicit saves (the Preferences panel) touch the file.

Secrets stay OUT of this file: Plex tokens / Last.fm keys keep living in .env
(export.py's loader). This file is safe to read aloud, diff, and back up.

Server-side settings only. Presentation state (EQ curve, volume, column layout,
splitter positions) lives in the browser's localStorage — it is per-front-end by
nature and changes far too often for a file the analyzer also reads.
"""
from __future__ import annotations

import json
import os
import tempfile

SETTINGS_VERSION = 1

DEFAULTS = {
    "settings_version": SETTINGS_VERSION,
    "db_path": "",                 # analyzed+embedded mixer.db
    "playlist_dir": "",            # folder of .m3u/.m3u8 playlists (browse + save target)
    "engine": "auto",              # auto | v2 | musicip | learned  (auto = probe MusicIP, fall back v2)
    "musicip_url": "http://localhost:10002",
    "library_folders": [],         # watched/scanned music roots
    "ml_venv_python": "",          # python.exe of the heavy analyze venv ('' = analysis off)
    "scan_on_launch": False,       # run an incremental rescan when the app starts
    "watch_folders": False,        # live-watch library_folders; new/changed audio triggers an incremental scan
    "theme": "attune",             # UI theme id — the Winamp-throwback house identity
    "default_recipe": "",          # name of the recipe applied by Genius / preselected in Studio ('' = engine defaults)
}

# keys that only take effect after the server process restarts
RESTART_KEYS = {"db_path", "engine", "musicip_url"}


def config_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Attune")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "attune")


def settings_path():
    return os.path.join(config_dir(), "settings.json")


def load():
    """DEFAULTS overlaid with whatever the file holds. Unknown keys are kept (a newer
    build's settings survive an older build reading them); missing keys get defaults."""
    out = dict(DEFAULTS)
    try:
        with open(settings_path(), encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            out.update(data)
    except (OSError, ValueError):
        pass                        # no file / corrupt file -> pure defaults; save() heals it
    return out


def save(settings: dict):
    """Atomic write (tmp + replace) so a crash mid-save can't truncate the file."""
    d = config_dir()
    os.makedirs(d, exist_ok=True)
    settings = dict(settings)
    settings["settings_version"] = SETTINGS_VERSION
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(settings, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, settings_path())
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
    return settings


def update(patch: dict):
    """Merge a partial dict into the stored settings. Returns (settings, needs_restart)."""
    cur = load()
    needs_restart = sorted(k for k in patch
                           if k in RESTART_KEYS and patch[k] != cur.get(k))
    cur.update(patch)
    return save(cur), needs_restart


def effective(cli_value, env_key, setting_key, settings=None):
    """One value through the precedence chain: CLI > env > settings.json > default."""
    if cli_value not in (None, ""):
        return cli_value
    env = os.environ.get(env_key) if env_key else None
    if env:
        return env
    s = settings if settings is not None else load()
    return s.get(setting_key) or DEFAULTS.get(setting_key)
