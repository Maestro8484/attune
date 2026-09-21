"""Attune settings — the ONE config source of truth (HANDOFF §4 / TRAJECTORY §2).

A JSON file in %APPDATA%\\Attune\\settings.json (or $XDG_CONFIG_HOME/attune on
non-Windows), read by the desktop app, the LAN server, and the analyzer alike.
Precedence when a value is asked for: CLI arg > environment > settings.json > default.
CLI/env never write back; only explicit saves (the Preferences panel) touch the file.

The Plex key now lives in THIS file (Preferences -> Plex writes it, see plex_token
below), in the user's own profile folder. That makes this file a credential the moment
it holds one: it is no longer safe to paste, screenshot, or read aloud, and it should
be backed up the way any other secret is, not shared like a plain diff.

Server-side settings only. Presentation state (EQ curve, volume, column layout,
splitter positions) lives in the browser's localStorage — it is per-front-end by
nature and changes far too often for a file the analyzer also reads.
"""
from __future__ import annotations

import json
import os
import tempfile
import time

SETTINGS_VERSION = 1

DEFAULTS = {
    "settings_version": SETTINGS_VERSION,
    "db_path": "",                 # analyzed+embedded mixer.db
    "playlist_dir": "",            # folder of .m3u/.m3u8 playlists (browse + save target)
    "playlist_name_template": "like-{seed}",  # what every export route (download,
                                   # save-to-folder, copy-to-folder) expands into a
                                   # file/playlist name -- see plexmatch.resolve_title and
                                   # web/app.py's expand_playlist_name. This default
                                   # reproduces the exact name Attune has always written,
                                   # so nobody's existing exports change just because this
                                   # setting now exists.
    "engine": "auto",              # auto | v2 | musicip | learned  (auto = probe MusicIP, fall back v2)
    "musicip_url": "http://localhost:10002",
    "library_folders": [],         # watched/scanned music roots
    "exclude_folders": [],         # folder prefixes import-folder never walks (ruling
                                   # B2 2026-08-04: _SYNCAPP\Versioning seeded 199 dead
                                   # rows; scan.py prunes these during the walk)
    "local_library_root": "",      # local drive path matching paths as stored in the DB
                                   # (export flavor: local). '' = derived from the DB's own
                                   # path prefix at startup (see export.derive_local_root).
    "unc_library_root": "",        # \\server\share -- playlist paths for other LAN devices
                                   # (export flavor: unc, the default). Never guessed.
    "plex_library_root": "",       # path prefix as your Plex server sees the library
                                   # (export flavor: plex). Never guessed. This is a path,
                                   # not a credential -- the credential (plex_token, below)
                                   # lives in this same file now; see the module docstring.
    "plex_url": "",                 # Plex server address, e.g. http://192.168.1.50:32400.
                                   # Written by Preferences -> Plex once Test connection
                                   # passes. '' means Plex has not been set up yet.
    "plex_token": "",               # the Plex key itself -- SECRET. Written by Preferences
                                   # -> Plex; GET /api/settings never sends it back to the
                                   # browser (see web/app.py's _public_settings). Cleared
                                   # only by "Forget this server" (POST /api/plex/forget) --
                                   # never by an ordinary save, which must not silently wipe
                                   # a working key just because the field arrived blank.
    "plex_section_key": "",         # which Plex library (by its numeric section id) exports
                                   # go into. No safe default: the old .env-only code fell
                                   # back to "1", which on a server whose first section
                                   # isn't music would silently add tracks to a photo or
                                   # video library. Empty REFUSES the export instead.
    "plex_machine_id": "",          # the server's own machine identifier, needed to build a
                                   # playlist URI. Read off /identity by Test connection --
                                   # never typed by hand, since nobody could guess it right.
    "plex_server_name": "",         # display only, so Preferences can say which server is
                                   # connected without another round trip to Plex.
    "plex_env_migrated": False,     # a real one-shot flag: True once the old .env Plex
                                   # values (if any) have been copied into the five keys
                                   # above. Without a flag, re-copying whenever a key is
                                   # empty would hand a cleared token back on the next
                                   # launch -- see web/app.py's _migrate_plex_env_once.
    "path_flavor": "unc",           # how playlist paths are written: local | unc | plex.
                                   # Used to live only in the browser's localStorage
                                   # (attune.flavor), which forgot the choice on a second
                                   # install or a cleared browser profile. The setting is
                                   # the one memory of it now.
    "copy_layout": "flat",          # starting value of the USB-copy layout dropdown
                                   # (flat | tree). A per-export choice with a remembered
                                   # default, not a setting that removes the choice.
    "ml_venv_python": "",          # python.exe of the heavy analyze venv ('' = analysis off)
    "scan_on_launch": False,       # run an incremental rescan when the app starts
    "watch_folders": False,        # live-watch library_folders; new/changed audio triggers an incremental scan
    "theme": "bee",                # UI theme id, and the one memory of it as of CONNECT
                                   # (2026-09-20): prefs.js now reads this key back on open
                                   # and paints the theme grid from it. localStorage still
                                   # holds a copy, used only for the very first paint before
                                   # the settings fetch returns, so the window does not flash.
                                   # 'bee' matches the client default since the S11 MusicBee UI.
    "default_recipe": "",          # name of the recipe applied by Genius / preselected in Studio ('' = engine defaults)
    "plex_sync_folder": "",        # the folder web/plexsyncjob.py mirrors onto a Plex playlist.
                                   # NOT a library root: this is a hand-built roster living
                                   # outside the library (the car-USB folder), which is why it
                                   # is matched by tags rather than by a path rewrite.
    "plex_sync_title": "",         # NAME TEMPLATE for that playlist, e.g.
                                   # "DrivingTunesUSB ({date})". Expanded once per run
                                   # (plexmatch.resolve_title) and then matched EXACTLY --
                                   # never a prefix match, since picking the wrong existing
                                   # playlist would rewrite a real one. A template carrying
                                   # {date} makes a new playlist each day and leaves the
                                   # previous one standing; a fixed name updates one
                                   # forever. '' = nothing set up yet.
    "plex_sync_order": "folder",   # running order written into that playlist:
                                   # folder | shuffle | artist (plexmatch.ORDERS).
    "window_geometry": None,       # {"x","y","width","height"} of the desktop window at last
                                   # close (app_desktop.py). None = not saved yet / invalid ->
                                   # pywebview's own OS-placed default. Desktop-only native OS
                                   # window chrome, not page state, so it can't live in the
                                   # browser's localStorage the way `store` in studio.js does.
}

# keys that only take effect after the server process restarts
RESTART_KEYS = {"db_path", "engine", "musicip_url",
                "local_library_root", "unc_library_root", "plex_library_root"}


def config_dir():
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
        return os.path.join(base, "Attune")
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "attune")


def settings_path():
    return os.path.join(config_dir(), "settings.json")


_SAID_CORRUPT = False


def _set_aside_corrupt(path, why):
    """Rename an unreadable settings.json out of the way, once per process, and say so.

    A file that is MISSING and a file that is CORRUPT are not the same event and used to
    be handled as one: both fell through to pure defaults, and the next save() then wrote
    those defaults over the top. On a machine whose settings.json had been truncated by a
    power cut, that silently and permanently discarded the library path, the export roots,
    the Plex connection and every other preference -- the app looked merely forgetful
    while it was in fact destroying the only copy. (Raised by the cold audit of the five
    release streams, 2026-09-20, finding 11.)

    Renaming rather than deleting means the values are still recoverable by hand, and
    renaming rather than leaving it in place means the next save() writes a fresh file
    instead of failing forever. Best effort: if the rename itself fails there is nothing
    useful left to do but carry on with defaults, which is what the old code did anyway.
    """
    global _SAID_CORRUPT
    try:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        aside = f"{path}.corrupt-{stamp}"
        # Two rescues in the same second would otherwise land on the same name and
        # os.replace would overwrite the FIRST one, throwing away the very copy this
        # function exists to keep.
        n = 2
        while os.path.exists(aside):
            aside = f"{path}.corrupt-{stamp}-{n}"
            n += 1
        os.replace(path, aside)
        if not _SAID_CORRUPT:
            print(f"Your settings file could not be read ({why}). It has been kept as "
                  f"{aside} and Attune has started on its defaults. Nothing in it was "
                  f"overwritten.")
    except OSError:
        if not _SAID_CORRUPT:
            print(f"Your settings file could not be read ({why}) and could not be moved "
                  f"aside either. Attune has started on its defaults.")
    _SAID_CORRUPT = True


def load(with_status=False):
    """DEFAULTS overlaid with whatever the file holds. Unknown keys are kept (a newer
    build's settings survive an older build reading them); missing keys get defaults.

    Three outcomes, and they are NOT the same event, which is the whole point:

      "ok"          the file was read
      "missing"     there is no file yet: the ordinary first run, says nothing
      "unreadable"  the file is there and could not be opened -- held by a backup agent,
                    an antivirus scanner, OneDrive mid-sync, or on a drive that just went
                    away. The values are still in it. Nothing may overwrite it.
      "corrupt"     the file is there and is not valid settings. It has been moved aside
                    by _set_aside_corrupt, so writing a fresh one is now safe.

    `with_status=True` returns (settings, status). Every existing caller passes nothing
    and gets the dict alone, exactly as before. `update()` uses the status to refuse to
    write over a file it could not read -- without that, a settings file locked for a
    few seconds at launch got replaced by pure defaults, silently, taking the library
    path, the export roots and the Plex key with it. (Cold Fable audit, 2026-09-20.)"""
    out = dict(DEFAULTS)
    path = settings_path()
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:
        return (out, "missing") if with_status else out
    except OSError:
        return (out, "unreadable") if with_status else out
    except ValueError:
        _set_aside_corrupt(path, "it is not valid JSON")
        return (out, "corrupt") if with_status else out
    if not isinstance(data, dict):
        _set_aside_corrupt(path, f"it holds a {type(data).__name__}, not a set of settings")
        return (out, "corrupt") if with_status else out
    out.update(data)
    return (out, "ok") if with_status else out


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


class SettingsUnreadable(OSError):
    """The settings file is there and could not be read, so it must not be written over."""


def update(patch: dict):
    """Merge a partial dict into the stored settings. Returns (settings, needs_restart).

    Refuses when the file exists but could not be read. A merge is read-then-write, and
    a read that failed leaves nothing to merge INTO: writing then means replacing every
    real value with a default. That is not a hypothetical -- it is a file held for a few
    seconds by a backup agent or an antivirus scanner at exactly the moment the app
    starts, and the loss is silent and total. Callers that genuinely want to overwrite
    whatever is there call save() directly."""
    cur, status = load(with_status=True)
    if status == "unreadable":
        raise SettingsUnreadable(
            f"{settings_path()} exists but could not be read, so it was left alone "
            f"rather than overwritten. Close whatever is holding it and try again.")
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
