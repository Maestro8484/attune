"""Attune auto-playlists — user-defined smart-playlist RULES (MusicBee parity).

Studio already has fixed Smart Views (loved / top-rated / recent / …). This adds the
open-ended version: a rule set the user builds and saves, evaluated live against the
library every time it's opened, so it stays current as ratings/plays change.

A rule set is JSON:
    {
      "match": "all" | "any",            # AND vs OR across conditions
      "conditions": [ {field, op, value}, ... ],
      "sort": "<field>", "desc": bool,   # optional
      "limit": int                        # optional (0/absent = no cap)
    }

Evaluation runs over the SAME in-memory LibraryIndex arrays studio.py/userdata.py fill
(rating, plays, loved, date_added, last_played, year, genre tags, artist, album, title,
seconds, analyzed) — no per-row DB or tag I/O, so a 21k-track library evaluates in a few ms.

Saved sets live in a `smartlists` table, additive and OUTSIDE db.py's SCHEMA_VERSION guard
(same rationale as usermeta: user data must survive a features rebuild).

Endpoints:
  GET  /api/smartlist/list                 -> [{id,name,rules}]
  POST /api/smartlist/save {id?,name,rules}-> {id}
  POST /api/smartlist/delete {id}
  POST /api/smartlist/preview {rules}       -> {rows,total}   (evaluate without saving)
  GET  /api/smartlist/open?id=              -> {name,rules,rows,total}
"""
from __future__ import annotations

import json
import sqlite3
import time

from flask import Blueprint, jsonify, request

# field -> (kind, accessor(lib, i)).  kind drives which operators are legal + how compared.
FIELDS = {
    "rating":      ("num",  lambda lib, i: lib.rating[i]),
    "plays":       ("num",  lambda lib, i: lib.plays[i]),
    "year":        ("num",  lambda lib, i: lib.year[i]),
    "length":      ("num",  lambda lib, i: lib.seconds[i]),          # seconds
    "loved":       ("bool", lambda lib, i: bool(lib.loved[i])),
    "analyzed":    ("bool", lambda lib, i: bool(lib.analyzed[i])),
    "added":       ("date", lambda lib, i: lib.date_added[i]),       # epoch secs
    "last_played": ("date", lambda lib, i: lib.last_played[i]),
    "artist":      ("text", lambda lib, i: lib.artist[i]),
    "album":       ("text", lambda lib, i: lib.album[i]),
    "title":       ("text", lambda lib, i: lib.title[i]),
    "genre":       ("tags", lambda lib, i: lib.gtags[i]),            # list of tag strings
}

NUM_OPS = {"eq", "ne", "gte", "lte", "gt", "lt", "between"}
TEXT_OPS = {"is", "is_not", "contains", "not_contains", "starts", "ends"}
BOOL_OPS = {"is"}
DATE_OPS = {"in_last_days", "not_in_last_days", "before_days", "gte", "lte"}


def _ensure_schema(db_path):
    con = sqlite3.connect(db_path, timeout=30)
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("""CREATE TABLE IF NOT EXISTS smartlists (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            name    TEXT UNIQUE,
            rules   TEXT,
            created INTEGER
        )""")
        con.commit()
    finally:
        con.close()


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _make_predicate(cond):
    """Compile one condition dict into a fn(lib, i)->bool. Raises ValueError on a bad rule."""
    field = cond.get("field")
    op = cond.get("op")
    val = cond.get("value")
    if field not in FIELDS:
        raise ValueError(f"unknown field: {field!r}")
    kind, get = FIELDS[field]

    if kind == "num":
        if op not in NUM_OPS:
            raise ValueError(f"op {op!r} not valid for {field}")
        if op == "between":
            lo, hi = (val if isinstance(val, (list, tuple)) and len(val) == 2 else (0, 0))
            lo, hi = _num(lo), _num(hi)
            return lambda lib, i: lo <= get(lib, i) <= hi
        rhs = _num(val)
        cmp = {"eq": lambda x: x == rhs, "ne": lambda x: x != rhs,
               "gte": lambda x: x >= rhs, "lte": lambda x: x <= rhs,
               "gt": lambda x: x > rhs, "lt": lambda x: x < rhs}[op]
        return lambda lib, i: cmp(get(lib, i))

    if kind == "bool":
        want = val in (True, "true", "1", 1, "yes")
        return lambda lib, i: get(lib, i) == want

    if kind == "date":
        # values are "days ago" style; compare against now at eval time.
        days = _num(val)
        if op in ("in_last_days", "not_in_last_days"):
            neg = op == "not_in_last_days"
            def pred(lib, i, days=days, neg=neg):
                t = get(lib, i)
                if not t:
                    return neg              # never-played counts as "not in last N"
                within = (time.time() - t) <= days * 86400
                return (not within) if neg else within
            return pred
        if op == "before_days":            # older than N days
            return lambda lib, i: bool(get(lib, i)) and (time.time() - get(lib, i)) > days * 86400
        # gte/lte on raw epoch (rarely used directly)
        rhs = _num(val)
        return (lambda lib, i: get(lib, i) >= rhs) if op == "gte" \
            else (lambda lib, i: get(lib, i) <= rhs)

    if kind == "tags":
        needle = str(val or "").casefold()
        if op in ("contains", "is"):
            # membership / substring across the multi-valued genre tags
            def pred(lib, i, needle=needle, exact=(op == "is")):
                for t in get(lib, i):
                    tc = t.casefold()
                    if (tc == needle) if exact else (needle in tc):
                        return True
                return False
            return pred
        if op == "not_contains":
            inner = _make_predicate({"field": "genre", "op": "contains", "value": val})
            return lambda lib, i: not inner(lib, i)
        raise ValueError(f"op {op!r} not valid for genre")

    # text
    if op not in TEXT_OPS:
        raise ValueError(f"op {op!r} not valid for {field}")
    needle = str(val or "").casefold()
    def tpred(lib, i, op=op, needle=needle):
        s = str(get(lib, i) or "").casefold()
        if op == "is":            return s == needle
        if op == "is_not":        return s != needle
        if op == "contains":      return needle in s
        if op == "not_contains":  return needle not in s
        if op == "starts":        return s.startswith(needle)
        if op == "ends":          return s.endswith(needle)
        return False
    return tpred


def evaluate(lib, rules):
    """Return a list of pool indices matching `rules`, sorted+limited. Raises ValueError."""
    conds = rules.get("conditions") or []
    if not isinstance(conds, list):
        raise ValueError("conditions must be a list")
    preds = [_make_predicate(c) for c in conds]
    match_any = (rules.get("match") or "all") == "any"

    if not preds:
        idxs = list(range(lib.n))
    else:
        idxs = []
        for i in range(lib.n):
            if match_any:
                ok = any(p(lib, i) for p in preds)
            else:
                ok = all(p(lib, i) for p in preds)
            if ok:
                idxs.append(i)

    # Sort on the RAW field value with honest asc/desc — NOT lib.SORTS, whose rating/plays/
    # added keys are negated so the library table shows "best first" by default; reusing them
    # here would invert the editor's explicit ascending/descending choice.
    sort = rules.get("sort")
    if sort in FIELDS:
        kind, get = FIELDS[sort]
        if kind == "tags":
            keyf = lambda i: (get(lib, i)[0].casefold() if get(lib, i) else "￿")
        elif kind == "text":
            keyf = lambda i: (str(get(lib, i)) or "￿").casefold()
        else:                                  # num / date / bool: sort on the number itself
            keyf = lambda i: get(lib, i)
        idxs.sort(key=keyf, reverse=bool(rules.get("desc")))
    elif sort in lib.SORTS:                     # any other saved key: fall back to table order
        idxs.sort(key=lambda i: lib.SORTS[sort](lib, i), reverse=bool(rules.get("desc")))
    limit = int(rules.get("limit") or 0)
    if limit > 0:
        idxs = idxs[:limit]
    return idxs


def register(app, ctx):
    """ctx: dict(db_path, lib)."""
    lib = ctx["lib"]
    db_path = ctx["db_path"]
    _ensure_schema(db_path)
    bp = Blueprint("smartlists", __name__)

    def _rows(idxs):
        return [lib.row(i) for i in idxs]

    @bp.get("/api/smartlist/list")
    def sl_list():
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            out = [{"id": r[0], "name": r[1], "rules": json.loads(r[2] or "{}")}
                   for r in con.execute("SELECT id,name,rules FROM smartlists ORDER BY name")]
        finally:
            con.close()
        return jsonify(smartlists=out)

    @bp.post("/api/smartlist/preview")
    def sl_preview():
        body = request.get_json(silent=True) or {}
        rules = body.get("rules")
        if not isinstance(rules, dict):
            return jsonify(error="expected {rules:{...}}"), 400
        try:
            idxs = evaluate(lib, rules)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        cap = min(len(idxs), 500)
        return jsonify(total=len(idxs), rows=_rows(idxs[:cap]),
                       ids=idxs)       # full id list so the client can queue/export all

    @bp.get("/api/smartlist/open")
    def sl_open():
        try:
            sid = int(request.args.get("id", ""))
        except ValueError:
            return jsonify(error="bad id"), 400
        con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = con.execute("SELECT name,rules FROM smartlists WHERE id=?", (sid,)).fetchone()
        finally:
            con.close()
        if not row:
            return jsonify(error="not found"), 404
        rules = json.loads(row[1] or "{}")
        try:
            idxs = evaluate(lib, rules)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        cap = min(len(idxs), 500)
        return jsonify(id=sid, name=row[0], rules=rules,
                       total=len(idxs), rows=_rows(idxs[:cap]), ids=idxs)

    @bp.post("/api/smartlist/save")
    def sl_save():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        rules = body.get("rules")
        if not name or not isinstance(rules, dict):
            return jsonify(ok=False, error="name and rules required"), 400
        try:
            evaluate(lib, rules)          # validate the rules before persisting
        except ValueError as e:
            return jsonify(ok=False, error=str(e)), 400
        rj = json.dumps(rules)
        con = sqlite3.connect(db_path, timeout=30)
        try:
            con.execute("PRAGMA journal_mode=WAL")
            sid = body.get("id")
            if sid:
                con.execute("UPDATE smartlists SET name=?, rules=? WHERE id=?",
                            (name, rj, int(sid)))
            else:
                cur = con.execute(
                    "INSERT OR REPLACE INTO smartlists(name,rules,created) VALUES(?,?,?)",
                    (name, rj, int(time.time())))
                sid = cur.lastrowid
            con.commit()
        except sqlite3.Error as e:
            return jsonify(ok=False, error=str(e)), 500
        finally:
            con.close()
        return jsonify(ok=True, id=int(sid))

    @bp.post("/api/smartlist/delete")
    def sl_delete():
        body = request.get_json(silent=True) or {}
        try:
            sid = int(body.get("id"))
        except (TypeError, ValueError):
            return jsonify(ok=False, error="bad id"), 400
        con = sqlite3.connect(db_path, timeout=30)
        try:
            con.execute("DELETE FROM smartlists WHERE id=?", (sid,))
            con.commit()
        finally:
            con.close()
        return jsonify(ok=True)

    app.register_blueprint(bp)
