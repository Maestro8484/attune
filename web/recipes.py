"""Attune mix recipes — named, saved bundles of MIX PARAMETERS (weights/variety/flow/
dedup/size), NOT engine math. A recipe is a preset for the sliders/toggles the client
already sends to /api/mix; this module never touches HybridEngine, never scores a
track, and never picks what plays -- it only stores and validates the numbers a human
(or Genius) hands to the existing engine. LAW-safe by construction: no retrieval metric
is read or written here.

Saved recipes live in a `recipes` table, additive and OUTSIDE db.py's SCHEMA_VERSION
guard (same rationale as smartlists.py / usermeta: user data must survive a features
rebuild). A handful of BUILTINS are seeded once, the first time the table is created,
so a fresh library always has a few sane starting points.

Endpoints:
  GET  /api/recipe/list         -> {recipes:[{id,name,params,builtin}], default}
  POST /api/recipe/save {id?,name,params} -> {ok,id}
  POST /api/recipe/delete {id}  -> {ok}
  GET  /api/recipe/genius_seed  -> {i,tier} | 404 if the library is empty
"""
from __future__ import annotations

import json
import random
import sqlite3
import time

from flask import Blueprint, jsonify, request

# Whitelist of legal recipe-param keys. Anything else in a saved/validated params dict
# is rejected outright -- a recipe is a small, closed vocabulary, not an arbitrary blob.
PARAM_KEYS = {"clap", "lib", "genre", "bpm", "era", "variety", "flow", "dedup", "size"}

# Weight sliders the client exposes (app.py SLIDER_KEYS) -- floats, clamped 0..5.
_WEIGHT_KEYS = ("clap", "lib", "genre", "bpm", "era")

# Legal dedup field values -- MUST match app.py's DEDUP_FIELDS (web/app.py:180). Verified
# by reading app.py directly: it is ("song", "title", "file"), NOT "artist"/"album".
DEDUP_FIELDS = ("song", "title", "file")


def _ensure_schema(db_path):
    """Create the `recipes` table if missing. Returns True if the table ALREADY existed
    (so the caller can decide whether to seed BUILTINS), False if it was just created."""
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        row = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='recipes'").fetchone()
        existed = row is not None
        con.execute("""CREATE TABLE IF NOT EXISTS recipes (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT UNIQUE,
            params  TEXT,
            builtin INTEGER DEFAULT 0,
            created INTEGER
        )""")
        con.commit()
    finally:
        con.close()
    return existed


def _clamp(v, lo, hi):
    v = float(v)
    if v != v:  # NaN guard
        raise ValueError("not a number")
    return max(lo, min(hi, v))


def validate_params(p):
    """Normalize a recipe params dict, or raise ValueError on anything illegal.
    Unknown keys are a hard error -- a typo'd param should not silently no-op."""
    if not isinstance(p, dict):
        raise ValueError("params must be an object")
    unknown = sorted(set(p) - PARAM_KEYS)
    if unknown:
        raise ValueError(f"unknown param(s): {', '.join(unknown)}")

    out = {}
    for k in _WEIGHT_KEYS:
        if k in p and p[k] is not None:
            out[k] = _clamp(p[k], 0.0, 5.0)

    for k in ("variety", "flow"):
        if k in p and p[k] is not None:
            v = p[k]
            out[k] = 1 if v in (True, "true", "1", 1, "yes") else 0

    if "dedup" in p and p["dedup"] is not None:
        d = p["dedup"]
        if d not in DEDUP_FIELDS:
            raise ValueError(f"dedup must be one of {DEDUP_FIELDS} or absent")
        out["dedup"] = d

    if "size" in p and p["size"] is not None:
        try:
            out["size"] = int(_clamp(p["size"], 20, 150))
        except (TypeError, ValueError):
            raise ValueError("size must be a number")

    return out


# Literal weight numbers below are derived from hybrid.py's DEFAULT_WEIGHTS (verified by
# reading src/hybrid.py:35): {"clap": 1.0, "lib": 0.4, "genre": 0.3, "bpm": 0.3, "era": 0.1,
# "key": 0.0}. Computed here, NOT at runtime, so a recipe's stored params stay stable even
# if a future tuning pass changes the engine's live defaults.
BUILTINS = [
    ("Classic Journey", {"variety": 1, "flow": 1}),
    ("Sound-Alike", {"clap": 1.5, "lib": 0.6, "variety": 0, "flow": 0}),
    ("Same-Era Deep Cuts", {"era": 0.2, "variety": 1}),
    ("Genre Purist", {"genre": 0.6, "variety": 0, "flow": 0}),
    ("Wander Far", {"variety": 1, "flow": 1, "genre": 0.15}),
]


def _seed_builtins(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        now = int(time.time())
        for name, params in BUILTINS:
            con.execute(
                "INSERT OR IGNORE INTO recipes(name,params,builtin,created) VALUES(?,?,1,?)",
                (name, json.dumps(params), now))
        con.commit()
    finally:
        con.close()


def _pick_genius_seed(lib):
    """Tiered seed pick for the Genius button: prefer a loved-and-rested track, then a
    highly-rated one, then anything -- analyzed-only at every tier (an unanalyzed pool
    index has no CLAP/librosa vectors, so the engine can't mix from it). Random choice
    within the first non-empty tier. Returns (i, tier) or (None, None) if nothing qualifies."""
    fourteen_days_ago = time.time() - 14 * 86400

    tier1 = [i for i in range(lib.n)
             if lib.analyzed[i] and lib.loved[i]
             and (lib.last_played[i] == 0 or lib.last_played[i] < fourteen_days_ago)]
    if tier1:
        return random.choice(tier1), 1

    tier2 = [i for i in range(lib.n) if lib.analyzed[i] and lib.rating[i] >= 4]
    if tier2:
        return random.choice(tier2), 2

    tier3 = [i for i in range(lib.n) if lib.analyzed[i]]
    if tier3:
        return random.choice(tier3), 3

    return None, None


def register(app, ctx):
    """ctx: dict(db_path, lib, cfg)."""
    lib = ctx["lib"]
    db_path = ctx["db_path"]
    cfg = ctx["cfg"]
    existed = _ensure_schema(db_path)
    if not existed:
        _seed_builtins(db_path)

    bp = Blueprint("recipes", __name__)

    @bp.get("/api/recipe/list")
    def r_list():
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            out = [{"id": r[0], "name": r[1], "params": json.loads(r[2] or "{}"),
                    "builtin": bool(r[3])}
                   for r in con.execute(
                       "SELECT id,name,params,builtin FROM recipes ORDER BY builtin DESC, name")]
        finally:
            con.close()
        default_name = cfg.load().get("default_recipe") or ""
        return jsonify(recipes=out, default=default_name)

    @bp.post("/api/recipe/save")
    def r_save():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        params = body.get("params")
        if not name:
            return jsonify(ok=False, error="name required"), 400
        try:
            norm = validate_params(params if params is not None else {})
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400

        pj = json.dumps(norm)
        rid = body.get("id")
        con = sqlite3.connect(db_path, timeout=30)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            if rid:
                # An UPDATE may rename/edit ANY recipe, builtins included -- that's fine,
                # a builtin is just a starting point, not immutable.
                con.execute("UPDATE recipes SET name=?, params=? WHERE id=?",
                            (name, pj, int(rid)))
                sid = int(rid)
            else:
                # Deliberately NOT "INSERT OR REPLACE" like smartlists.py: a name collision
                # on a brand-new recipe most likely means the user meant to edit an EXISTING
                # one (and should pick it from the list / pass its id), not silently blow
                # away whatever already had that name. Surface a clear 400 instead.
                try:
                    cur = con.execute(
                        "INSERT INTO recipes(name,params,builtin,created) VALUES(?,?,0,?)",
                        (name, pj, int(time.time())))
                except sqlite3.IntegrityError:
                    return jsonify(ok=False,
                                    error=f"a recipe named {name!r} already exists"), 400
                sid = cur.lastrowid
            con.commit()
        except sqlite3.Error as e:
            return jsonify(ok=False, error=str(e)), 500
        finally:
            con.close()
        return jsonify(ok=True, id=int(sid))

    @bp.post("/api/recipe/delete")
    def r_delete():
        body = request.get_json(silent=True) or {}
        try:
            rid = int(body.get("id"))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="bad id"), 400
        con = sqlite3.connect(db_path, timeout=30)
        try:
            row = con.execute("SELECT name FROM recipes WHERE id=?", (rid,)).fetchone()
            con.execute("DELETE FROM recipes WHERE id=?", (rid,))
            con.commit()
        finally:
            con.close()
        # If the deleted recipe was the configured default, clear it so Genius/Studio
        # don't keep pointing at a name that no longer resolves to anything.
        if row:
            settings = cfg.load()
            if settings.get("default_recipe") == row[0]:
                cfg.update({"default_recipe": ""})
        return jsonify(ok=True)

    @bp.get("/api/recipe/genius_seed")
    def r_genius_seed():
        i, tier = _pick_genius_seed(lib)
        if i is None:
            return jsonify(error="no analyzed tracks in the library"), 404
        return jsonify(i=i, tier=tier)

    app.register_blueprint(bp)
