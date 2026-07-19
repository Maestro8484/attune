"""Common engine interface so the shell can swap mix engines without caring which one is
live: `V2Engine` (our own CLAP/librosa hybrid, src/hybrid.py) and `MusicIPAdapter` (a live
MusicIP Mixer, src/musicip_engine.py) both satisfy the same small contract below.

Refs are always OUR DB POOL INDEX (an int position into the pool of paths the shell already
knows -- e.g. HybridEngine.paths). Every search()/similar() result carries a resolvable
pool_i, so existing playback/export code -- built entirely around that pool index (see
web/app.py's /api/mix, /api/search) -- keeps working no matter which engine produced it.
Anything an engine can't resolve into that pool is DROPPED (and logged), never returned
with a fake/None index.

Reuse, not reinvent:
  * V2Engine is a thin wrapper over hybrid.HybridEngine. search() reuses the exact
    label-substring-scan web/app.py's /api/search already does; similar() calls mix().
    The extra V2-only capabilities (refine/mmr/flow/explain/weights) are thin pass-throughs
    to the matching HybridEngine method -- no logic is duplicated, only index<->path
    translation at the boundary.
  * MusicIPAdapter is a thin wrapper over musicip_engine.MusicIPEngine, resolving MusicIP's
    UNC paths to our pool index via musicip_engine.build_reconciler() (already written for
    eval/bakeoff_musicip.py) -- same relkey mirror-invariant used there.
"""
from __future__ import annotations

import importlib.util
import os
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional

HERE = os.path.dirname(os.path.abspath(__file__))


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@dataclass(frozen=True)
class Ref:
    """One search/similar hit, resolved to OUR pool. `pool_i` is what playback/export
    should use; `label` is display-only ("Artist - Title", falling back to basename)."""
    pool_i: int
    label: str


def _as_pool_i(x):
    return x.pool_i if isinstance(x, Ref) else int(x)


class Engine:
    """Common contract. Subclasses set `name`/`capabilities` and implement search/similar;
    both return `[Ref]` resolved to the caller's pool so the shell never has to know which
    concrete engine produced a result."""
    name: str = "base"
    capabilities: frozenset = frozenset()

    def search(self, q: str, limit: int = 40) -> list:
        raise NotImplementedError

    def similar(self, seed_ref, size: int = 25, **controls) -> list:
        raise NotImplementedError


def _label(eng, path):
    """Same "Artist - Title" (falling back to basename) label web/app.py's _label() uses."""
    m = eng.meta.get(path, {})
    return f"{m.get('artist') or '?'} - {m.get('title') or os.path.basename(path)}"


class V2Engine(Engine):
    """Thin wrapper over hybrid.HybridEngine. HybridEngine's own `.paths` pool IS the
    shared pool here (no reconciliation needed) -- pool_i is just eng.idx[path]."""
    name = "v2"
    capabilities = frozenset({"search", "similar", "refine", "mmr", "flow", "explain", "weights"})

    def __init__(self, db_path=None, hybrid_engine=None):
        if hybrid_engine is None:
            hy = _load("hybrid", os.path.join(HERE, "hybrid.py"))
            hybrid_engine = hy.HybridEngine(db_path)
        self.eng = hybrid_engine
        # precompute lowercased labels once, exactly like web/app.py's create_app() does,
        # so search() is a cheap scan rather than re-labelling on every call.
        self._labels = [_label(self.eng, p) for p in self.eng.paths]
        self._labels_lc = [s.lower() for s in self._labels]

    def _ref(self, pool_i):
        return Ref(pool_i=pool_i, label=self._labels[pool_i])

    def search(self, q, limit=40):
        q = (q or "").strip().lower()
        if not q:
            return []
        out = []
        for i, lab in enumerate(self._labels_lc):
            if q in lab:
                out.append(self._ref(i))
                if len(out) >= limit:
                    break
        return out

    def similar(self, seed_ref, size=25, artist_spacing=3, **_ignored):
        i = _as_pool_i(seed_ref)
        if not (0 <= i < len(self.eng.paths)):
            return []
        picks = self.eng.mix(self.eng.paths[i], size=size, artist_spacing=artist_spacing)
        if not picks:
            return []
        return [self._ref(self.eng.idx[p]) for p in picks]

    # ---- thin pass-throughs for the V2-only capabilities (no logic duplicated) ----

    def refine(self, seed_ref, liked=(), disliked=(), size=25, alpha=1.0, beta=0.75):
        i = _as_pool_i(seed_ref)
        liked_i = [_as_pool_i(x) for x in liked]
        disliked_i = [_as_pool_i(x) for x in disliked]
        q = self.eng.refine(i, liked_idx=liked_i, disliked_idx=disliked_i, alpha=alpha, beta=beta)
        picks = self.eng.mix_from_vector(q, size=size, exclude=[i] + liked_i + disliked_i)
        return [self._ref(self.eng.idx[p]) for p in picks]

    def mmr(self, candidate_refs, k=None, lambda_=0.5, query_ref=None):
        cand_paths = [self.eng.paths[_as_pool_i(c)] for c in candidate_refs]
        query = self.eng.X[_as_pool_i(query_ref)] if query_ref is not None else None
        picks = self.eng.mmr(cand_paths, k=k, lambda_=lambda_, query=query)
        return [self._ref(self.eng.idx[p]) for p in picks]

    def flow(self, refs):
        paths = [self.eng.paths[_as_pool_i(r)] for r in refs]
        ordered = self.eng.order_by_flow(paths)
        return [self._ref(self.eng.idx[p]) for p in ordered]

    def explain(self, seed_ref, cand_ref):
        return self.eng.explain(_as_pool_i(seed_ref), _as_pool_i(cand_ref))

    def set_weights(self, weights: dict):
        self.eng.w.update(weights)


class MusicIPAdapter(Engine):
    """Wraps musicip_engine.MusicIPEngine (a LIVE MusicIP Mixer over HTTP) and resolves
    every result to OUR pool index, so a MusicIP-driven mix looks exactly like a V2-driven
    one to the shell. Seed direction: our pool index -> our L: path -> relkey -> MusicIP's
    UNC path -> /api/mix. Tracks MusicIP returns that don't reconcile into our pool are
    skipped (small loss, logged) -- MusicIP's own catalog is a subset of ours (~17.4k
    Albums vs our full CLAP-embedded pool)."""
    name = "musicip"
    capabilities = frozenset({"search", "similar", "style", "variety"})

    def __init__(self, pool_paths, url="http://localhost:10002", mip=None,
                 log: Optional[Callable[[str], None]] = None):
        me = _load("musicip_engine", os.path.join(HERE, "musicip_engine.py"))
        self._me = me
        self.mip = mip or me.MusicIPEngine(url)
        self.pool_paths = pool_paths                    # our engine's ordered path list
        self.recon = me.build_reconciler(pool_paths)     # relkey -> our pool index
        self._pool_meta = None                           # optional, see attach_meta()
        self._log = log or (lambda msg: None)
        self._unc_by_relkey = None                       # lazy: relkey -> MusicIP UNC path

    def attach_meta(self, meta_by_path: dict):
        """Optional: attach {path: {'artist','title',...}} (e.g. HybridEngine.meta) so
        results carry a real "Artist - Title" label instead of a bare basename."""
        self._pool_meta = meta_by_path

    def _label_for(self, pool_i):
        p = self.pool_paths[pool_i]
        if self._pool_meta:
            m = self._pool_meta.get(p, {})
            if m.get("artist") or m.get("title"):
                return f"{m.get('artist') or '?'} - {m.get('title') or os.path.basename(p)}"
        return os.path.basename(p)

    def _resolve(self, unc_paths: Iterable[str]):
        """MusicIP UNC paths -> [Ref] over OUR pool; unreconciled tracks are dropped and
        the loss is logged (never returned with a fake index)."""
        unc_paths = list(unc_paths)
        out, dropped = [], 0
        for u in unc_paths:
            i = self.recon.get(self._me.relkey(u))
            if i is None:
                dropped += 1
                continue
            out.append(Ref(pool_i=i, label=self._label_for(i)))
        if dropped:
            self._log(f"MusicIPAdapter: dropped {dropped}/{len(unc_paths)} "
                       f"MusicIP tracks not present in our pool")
        return out

    def _seed_unc(self, seed_ref):
        """our pool index -> our L: path -> relkey -> MusicIP UNC path (None if MusicIP
        doesn't know this track)."""
        i = _as_pool_i(seed_ref)
        if self._unc_by_relkey is None:
            self._unc_by_relkey = {self._me.relkey(p): p for p in self.mip.songs()}
        return self._unc_by_relkey.get(self._me.relkey(self.pool_paths[i]))

    def search(self, q, limit=40):
        return self._resolve(self.mip.search(q, limit=limit))[:limit]

    def similar(self, seed_ref, size=25, style=40, variety=5, **_ignored):
        seed_unc = self._seed_unc(seed_ref)
        if seed_unc is None:
            self._log("MusicIPAdapter: seed not known to MusicIP (unreconciled path)")
            return []
        mix = self.mip.similar(seed_unc, size=size, style=style, variety=variety)
        return self._resolve(mix)


class LearnedEngine(Engine):
    """The distilled-metric engine: an ONNX-exported projection head (trained to imitate
    MusicIP's similarity function, see the MusicIP workspace's extracted/distill.py) over
    the CLAP-512 + librosa-79 features ALREADY in mixer.db. No torch anywhere -- inference
    is onnxruntime, preprocessing is numpy fed by models/learned_norm.json (the z-score
    stats the head was trained with; they are the TRAINING pool's stats and must never be
    recomputed from the live library).

    SELECTABLE, NOT DEFAULT: per LAW 1 this engine may only become the default after a
    blind ear test (eval/abtest.py). Until then it ships behind --engine learned.

    Pool discipline per the module docstring: results are OUR pool indices (the shared
    HybridEngine pool). Pool tracks missing either feature are skipped at load (logged),
    never returned. Ranking is cosine in the projected space; the walk applies the same
    artist-spacing rule as hybrid.mix() (a spacing-violating candidate is dropped for
    good, not deferred -- shipped, eared behavior)."""
    name = "learned"
    capabilities = frozenset({"search", "similar"})
    version = "metric_head 2026-07-09 (591->4096 GELU->512, opset 17)"

    def __init__(self, db_path, hybrid_engine=None, models_dir=None,
                 log: Optional[Callable[[str], None]] = None):
        import json
        import sqlite3
        import numpy as np
        try:
            import onnxruntime as ort
        except ImportError:
            raise SystemExit("--engine learned needs onnxruntime in the runtime venv: "
                             "pip install onnxruntime")
        self._np = np
        self._log = log or (lambda msg: None)
        if hybrid_engine is None:
            hy = _load("hybrid", os.path.join(HERE, "hybrid.py"))
            hybrid_engine = hy.HybridEngine(db_path)
        self.eng = hybrid_engine

        mdir = models_dir or os.path.join(HERE, "models")
        with open(os.path.join(mdir, "learned_norm.json"), encoding="utf-8") as fh:
            nj = json.load(fh)
        mu = np.asarray(nj["mu"], np.float64)
        sd = np.asarray(nj["sd"], np.float64)
        clip = float(nj["clip"])
        if mu.shape != (79,) or sd.shape != (79,):
            raise SystemExit(f"learned_norm.json malformed: mu/sd shapes {mu.shape}/{sd.shape}")
        sess = ort.InferenceSession(os.path.join(mdir, "metric_head.onnx"),
                                    providers=["CPUExecutionProvider"])
        d_in = sess.get_inputs()[0].shape[1]
        if d_in != 591:
            raise SystemExit(f"metric_head.onnx expects d_in={d_in}, not 591 -- wrong model file")

        # raw features straight from the db (read-only): HybridEngine keeps only ITS
        # z-scored librosa matrix, and this head needs the raw vectors normalized with
        # the TRAINING stats above -- so read them again here, verifying dims.
        con = sqlite3.connect("file:" + db_path.replace("\\", "/") + "?mode=ro", uri=True)
        clap, lib79 = {}, {}
        for p, dim, blob in con.execute("SELECT path,dim,vec FROM clap WHERE vec IS NOT NULL"):
            v = np.frombuffer(blob, np.float32)
            if v.shape[0] == dim == 512:
                clap[p] = v
        for p, blob, dim in con.execute(
                "SELECT path,vec,dim FROM features WHERE vec IS NOT NULL AND error IS NULL"):
            if dim != 79:
                continue
            v = np.frombuffer(blob, np.float32)
            if v.shape[0] == 79 and np.isfinite(v).all():
                lib79[p] = v
        con.close()

        # exactly the training preprocessing (distill.py load_features + zscore):
        # CLAP row-L2-normalized; librosa z-scored with the persisted mu/sd, clipped.
        rows, feats = [], []
        for i, p in enumerate(self.eng.paths):
            c, l = clap.get(p), lib79.get(p)
            if c is None or l is None:
                continue
            cn = c / max(float(np.linalg.norm(c)), 1e-9)
            ln = np.clip(((l.astype(np.float64) - mu) / sd), -clip, clip).astype(np.float32)
            rows.append(i)
            feats.append(np.concatenate([cn, ln]))
        skipped = len(self.eng.paths) - len(rows)
        if skipped:
            self._log(f"LearnedEngine: skipped {skipped}/{len(self.eng.paths)} pool tracks "
                      f"missing CLAP-512 or librosa-79")
        if not rows:
            raise SystemExit("LearnedEngine: no pool track has both CLAP-512 and librosa-79")

        X = np.vstack(feats).astype(np.float32)
        E = np.empty((X.shape[0], 512), np.float32)
        for b in range(0, X.shape[0], 2048):        # project the whole pool once at startup
            E[b:b + 2048] = sess.run(["embedding"], {"features": X[b:b + 2048]})[0]
        self.E = E                                   # unit rows (L2-norm is inside the graph)
        self.rows = rows                             # projected row -> pool index
        self._row_of = {pi: r for r, pi in enumerate(rows)}
        self.pool_size = len(rows)                   # LAW 3: the pool THIS engine ranks over
        # covered-only labels so a search hit is always a usable seed here
        self._labels = [_label(self.eng, self.eng.paths[pi]) for pi in rows]
        self._labels_lc = [s.lower() for s in self._labels]
        self._log(f"LearnedEngine ready: {self.pool_size} tracks projected to 512-d")

    def _ref(self, pool_i):
        return Ref(pool_i=pool_i, label=self._labels[self._row_of[pool_i]])

    def search(self, q, limit=40):
        q = (q or "").strip().lower()
        if not q:
            return []
        out = []
        for r, lab in enumerate(self._labels_lc):
            if q in lab:
                out.append(self._ref(self.rows[r]))
                if len(out) >= limit:
                    break
        return out

    def similar(self, seed_ref, size=25, artist_spacing=3, **_ignored):
        np = self._np
        i = _as_pool_i(seed_ref)
        r = self._row_of.get(i)
        if r is None:
            self._log("LearnedEngine: seed has no projected embedding (missing features)")
            return []
        sim = self.E @ self.E[r]
        # same walk as hybrid.mix(): best-first, seed skipped, an artist-spacing violator
        # is dropped for good (see the GOTCHA note there -- shipped behavior, eared).
        out, recent = [], []
        for j in np.argsort(-sim):
            if j == r:
                continue
            pool_j = self.rows[j]
            a = self.eng.artist[pool_j]
            if a and a in recent[-artist_spacing:]:
                continue
            out.append(self._ref(pool_j))
            recent.append(a)
            if len(out) >= size:
                break
        return out
