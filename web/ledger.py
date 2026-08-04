"""Export/mix ledger: Attune writes down what it HANDS OUT, never what anyone
listens to (operator ruling B1, 2026-08-04, RULINGS_SHEET_2026-08-04.md; this is
HORIZON's D1-DEST). One JSON line per event in <config_dir>/ledger.jsonl: when,
what kind, where it went, the seed, and for exports the tracklist.

Why it exists: the operator rotates USB sticks by wiping most of a stick and
loading the next generated playlist, so per-track deletions carry no taste signal;
the signal is what SURVIVES rotations, and a later read of a stick can only be
diffed against a record of what was written there and when (HANDOFF_RESUME S17,
C3). Without this file the app stays ignorant of its own output.

Deliberately a sidecar jsonl, not a mixer.db table: the DB is production data
behind a SCHEMA_VERSION refuse-to-load guard (CLAUDE.md section 5); an append-only
text file needs no migration and its absence loses nothing. A write failure must
never break a mix or an export, so it is swallowed after one stderr note.
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

_lock = threading.Lock()
_path = ""
_warned = False


def init(config_dir):
    """Point the ledger at <config_dir>/ledger.jsonl. Called once by create_app()."""
    global _path
    _path = os.path.join(config_dir, "ledger.jsonl")


def record(kind, **fields):
    """Append one event line. kind: mix | blend | adventure | export_m3u |
    export_dir | export_copy | export_plex. No-op if init() never ran."""
    global _warned
    if not _path:
        return
    fields["kind"] = kind
    fields["at"] = int(time.time())
    try:
        line = json.dumps(fields, ensure_ascii=False)
        with _lock:
            os.makedirs(os.path.dirname(_path), exist_ok=True)
            with open(_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
    except (OSError, TypeError, ValueError) as e:
        if not _warned:
            _warned = True
            print(f"ledger: write failed ({e}); further failures stay silent",
                  file=sys.stderr)
