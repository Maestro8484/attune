"""Attune collections (web/collection.py, singular: a module named collections.py would shadow the standard library one): a named, saved subset of the analyzed library to draw mixes from.

A collection is a list of song paths with a name. It is NOT a second library and NOT a
second engine: the one full-library engine stays loaded and a collection is applied to
its walks as a mask over the pool (hybrid.HybridEngine._walk's `allowed`). That is the
whole design, and it is what makes the same seed, recipe and song give the same fits or
does-not-fit answer whether the song sits in a 500-song collection or the full library:
every score is computed over the same vectors with the same library-wide statistics,
and the mask only decides which songs the walk may take.

Where they live. The library database is never written by this module (it is the
analyzed library, production data) and gets no new table. Collections are files in the
app's own settings folder, one JSON file per collection under <config_dir>/collections/:

    {"name": "Experiment 500", "created": 1760000000, "paths": ["...", ...]}

That is the settings.json / ledger.jsonl idiom (src/config.py), chosen over the
smartlists/recipes table idiom because this session's rule was that mixer.db is never
written. A collection names songs by PATH, not pool index, because pool indices change
on every reload and a path is what the library itself keys on.

Endpoints (the smartlists.py shape):
  GET  /api/collection/list                 -> {collections:[{id,name,size,missing}]}
  POST /api/collection/save {name, ids}     -> {ok,id,size}    ids = pool indices
  POST /api/collection/delete {id}          -> {ok}
  GET  /api/collection/open?id=             -> {id,name,total,missing,rows,ids}

`register()` returns a resolver the mix routes use: resolver.mask(cid) -> (mask, info),
mask a numpy bool array over the engine pool, info = {id, name, size, missing}.
"""
from __future__ import annotations

import json
import os
import re
import time

import numpy as np
from flask import Blueprint, jsonify, request

FOLDER = "collections"
MAX_SONGS = 5000            # a collection is a subset by construction, not a second library


def collections_dir(cfgmod):
    return os.path.join(cfgmod.config_dir(), FOLDER)


def _slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", (name or "").strip().lower()).strip("-")
    return s[:60] or "collection"


def _read(path):
    with open(path, encoding="utf-8") as fh:
        d = json.load(fh)
    if not isinstance(d, dict) or not isinstance(d.get("paths"), list):
        raise ValueError("not a collection file")
    return d


class Store:
    """The files on disk, read fresh on every call: a collection saved from another
    window, or dropped in by hand, is seen without a restart."""

    def __init__(self, cfgmod):
        self.cfgmod = cfgmod

    def _dir(self):
        d = collections_dir(self.cfgmod)
        os.makedirs(d, exist_ok=True)
        return d

    def _path(self, cid):
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", cid or ""):
            return None
        return os.path.join(self._dir(), cid + ".json")

    def list(self):
        out = []
        d = self._dir()
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            cid = fn[:-5]
            try:
                doc = _read(os.path.join(d, fn))
            except (OSError, ValueError):
                continue                        # a bad file is skipped, never fatal
            out.append((cid, doc))
        return out

    def get(self, cid):
        p = self._path(cid)
        if not p or not os.path.exists(p):
            return None
        try:
            return _read(p)
        except (OSError, ValueError):
            return None

    def save(self, name, paths):
        """A new collection. The id is the name's slug, made unique with a suffix, so a
        second 'Road trip' becomes road-trip-2 rather than replacing the first."""
        base = _slug(name)
        cid, n = base, 1
        while os.path.exists(self._path(cid)):
            n += 1
            cid = f"{base}-{n}"
        doc = {"name": name, "created": int(time.time()), "paths": list(paths)}
        tmp = self._path(cid) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=1)
        os.replace(tmp, self._path(cid))
        return cid

    def delete(self, cid):
        p = self._path(cid)
        if p and os.path.exists(p):
            os.remove(p)
            return True
        return False


class Resolver:
    """Collection id -> boolean mask over the engine pool. `eng` is the live
    HybridEngine, whose .idx (path -> pool index) is rebuilt in place on a library
    reload; the mask is computed against whatever .idx holds at call time. Not cached
    on purpose: a few hundred dictionary lookups per request is nothing, and a cache
    keyed on anything less than the pool's exact contents could serve a stale mask
    after a reload that swapped one song for another."""

    def __init__(self, store, eng):
        self.store = store
        self.eng = eng

    def mask(self, cid):
        """(mask, info) for a collection id, or (None, None) if there is no such
        collection. info: {id, name, size (songs found in the pool), missing (paths
        the pool no longer holds)}."""
        doc = self.store.get(cid)
        if doc is None:
            return None, None
        m = np.zeros(len(self.eng.paths), dtype=bool)
        missing = 0
        for path in doc["paths"]:
            i = self.eng.idx.get(path)
            if i is None:
                missing += 1
            else:
                m[i] = True
        info = {"id": cid, "name": doc.get("name") or cid, "size": int(m.sum()),
                "missing": missing}
        return m, info


def register(app, ctx):
    """ctx: dict(cfgmod, eng, lib, locked). Returns the Resolver."""
    cfgmod, eng, lib, locked = ctx["cfgmod"], ctx["eng"], ctx["lib"], ctx["locked"]
    store = Store(cfgmod)
    resolver = Resolver(store, eng)
    bp = Blueprint("collections", __name__)

    @bp.get("/api/collection/list")
    @locked
    def c_list():
        out = []
        for cid, doc in store.list():
            m, info = resolver.mask(cid)
            if info is not None:
                out.append(info)
        return jsonify(collections=out)

    @bp.post("/api/collection/save")
    @locked
    def c_save():
        body = request.get_json(silent=True) or {}
        name = (body.get("name") or "").strip()
        ids = body.get("ids")
        if not name or not isinstance(ids, list):
            return jsonify(ok=False, error="name and ids required"), 400
        try:
            ids = [int(x) for x in ids]
        except (TypeError, ValueError):
            return jsonify(ok=False, error="ids must be pool indices"), 400
        bad = [i for i in ids if not (0 <= i < len(eng.paths))]
        if bad:
            return jsonify(ok=False, error=f"unknown song index: {bad[0]}"), 400
        seen, paths = set(), []
        for i in ids:
            if i not in seen:
                seen.add(i)
                paths.append(eng.paths[i])
        if not paths:
            return jsonify(ok=False, error="a collection needs at least one song"), 400
        if len(paths) > MAX_SONGS:
            return jsonify(ok=False, error=f"a collection holds at most {MAX_SONGS} songs"), 400
        for cid, doc in store.list():
            if (doc.get("name") or "").strip().lower() == name.lower():
                return jsonify(ok=False, error=f"a collection named {name!r} already exists"), 400
        cid = store.save(name, paths)
        return jsonify(ok=True, id=cid, size=len(paths))

    @bp.post("/api/collection/delete")
    @locked
    def c_delete():
        body = request.get_json(silent=True) or {}
        cid = str(body.get("id") or "")
        if not store.delete(cid):
            return jsonify(ok=False, error="not found"), 404
        return jsonify(ok=True)

    @bp.get("/api/collection/open")
    @locked
    def c_open():
        cid = request.args.get("id") or ""
        m, info = resolver.mask(cid)
        if m is None:
            return jsonify(error="not found"), 404
        idxs = [int(i) for i in np.flatnonzero(m)]
        cap = min(len(idxs), 500)
        return jsonify(id=cid, name=info["name"], total=len(idxs), missing=info["missing"],
                       rows=[lib.row(i) for i in idxs[:cap]], ids=idxs)

    app.register_blueprint(bp)
    return resolver
