"""The hybrid ("pro") mixing engine — Attune's best, and the default when neural
embeddings are present.

It blends a music-trained CLAP embedding (the "ears") with a z-scored librosa timbre
vector and light rules:

  score(track) =  w_clap  * cosine(CLAP)
                + w_lib   * cosine(z-scored librosa 79-dim)   # the "lib" timbre term
                + w_genre * genre-tag Jaccard(seed)
                - w_bpm   * |tempo gap| / 40                    # linear (see below)
                - w_era   * |year gap| / 25
                + w_key   * key_compatibility(seed)            # OFF by default

This is **V2**, the recipe that won a blind A/B/C/V2/V3 listening test against genuine
MusicIP (extracted/human_ratings.md). Notes from that result:
  * the librosa "lib" term stays — dropping it (the V3/V4 lineage) lost by ear;
  * the key term is OFF by default: it lost by ear, and its _key_compat was reversed
    until fixed. It's available (set "key">0) now that the formula is correct;
  * tempo is a LINEAR gap, not octave-folded — folding is an un-eared idea kept out of
    the shipped default (the folded form lives in coherence.py's offline metric).

Default weights = V2 (see DEFAULT_WEIGHTS); override via data/engine_v2.json or the
constructor. Requires a `clap` table (see embed.py) and, for the lib term, librosa
features (scan.py analyze); with lib active the pool is the clap∩librosa intersection.
If CLAP is absent, use the librosa engine in mixer.py.
"""
from __future__ import annotations
import os, json, sqlite3, pathlib
import numpy as np

# V2 — the recipe that WON the human blind listening test (see extracted/human_ratings.md).
# CLAP ears + librosa 'lib' timbre + genre + linear tempo + era. NO key term by default:
# the key idea was sound but lost by ear (and its code was reversed until fixed). The key
# term remains available (set "key">0) now that _key_compat is correct, but ships off.
DEFAULT_WEIGHTS = {"clap": 1.0, "lib": 0.4, "genre": 0.3, "bpm": 0.3, "era": 0.1, "key": 0.0}

# Krumhansl-Schmuckler key profiles (major / minor) for key estimation from chroma.
_KRUM_MAJ = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
_KRUM_MIN = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])


def _connect_readonly(db_path, timeout=30):
    """Open db_path strictly read-only via a file: URI (mode=ro), so this engine can
    never accidentally CREATE the DB file (e.g. a typo'd path) or write to it. Falls
    back to a plain (non-uri) connect if db_path can't be turned into a file: URI —
    e.g. it's already a "file:...?..." URI itself, or pathlib chokes on it."""
    if db_path.startswith("file:"):
        return sqlite3.connect(db_path, timeout=timeout, uri=True)
    try:
        uri = pathlib.Path(os.path.abspath(db_path)).as_uri() + "?mode=ro"
        return sqlite3.connect(uri, timeout=timeout, uri=True)
    except ValueError:
        # not representable as a URI for some reason -- fall back rather than fail
        return sqlite3.connect(db_path, timeout=timeout)


def _tags(g):
    if not g:
        return set()
    return {t.strip().lower() for part in g.split(";") for t in part.split(",") if t.strip()}


def _key_from_chroma(chroma12):
    """(tonic 0-11, is_major) by profile correlation, or None."""
    if chroma12 is None or chroma12.std() < 1e-9:
        return None
    best = (-2.0, None)
    for rot in range(12):
        c = np.roll(chroma12, -rot)
        for prof, ismaj in ((_KRUM_MAJ, True), (_KRUM_MIN, False)):
            s = float(np.corrcoef(c, prof)[0, 1])
            if s > best[0]:
                best = (s, (rot, ismaj))
    return best[1]


def _keys_from_chroma_batch(chroma_rows):
    """Vectorized _key_from_chroma over a list of 12-dim chroma vectors (or None).

    One (N,24) Pearson matrix + argmax instead of N*24 np.corrcoef calls -- the
    per-track loop was 23s of a 26s engine load on the 21k library (90% of boot),
    for a key term that ships with weight 0.0. Identity: corr(roll(c,-rot), prof)
    == corr(c, roll(prof, rot)), so the 24 rotated/standardized profiles are
    precomputed and each track is a single dot product. Column order (rot0 maj,
    rot0 min, rot1 maj, ...) mirrors the loop's scan order, so argmax's
    first-max tie-break matches the loop's strict '>'. Parity-gated: identical
    (tonic, is_major)/None on all 21,236 prod pool tracks (2026-07-22).
    """
    n = len(chroma_rows)
    have = [i for i, c in enumerate(chroma_rows) if c is not None]
    out = [None] * n
    if not have:
        return out
    C = np.vstack([np.asarray(chroma_rows[i], np.float64) for i in have])
    std = C.std(axis=1)
    valid = std >= 1e-9
    Cz = (C - C.mean(axis=1, keepdims=True)) / np.where(std < 1e-9, 1.0, std)[:, None]
    P = np.empty((24, 12))
    for rot in range(12):
        for k, prof in enumerate((_KRUM_MAJ, _KRUM_MIN)):
            pr = np.roll(prof, rot)
            P[rot * 2 + k] = (pr - pr.mean()) / pr.std()
    col = np.argmax(Cz @ P.T, axis=1)   # /12 omitted: constant scale, same argmax
    for r, i in enumerate(have):
        if valid[r]:
            out[i] = (int(col[r] // 2), col[r] % 2 == 0)
    return out


def _key_compat(k1, k2):
    """1.0 same key; 0.8 relative maj/min or a fifth away; 0.4 two fifths; else 0. None-safe."""
    if not k1 or not k2:
        return 0.5
    (t1, m1), (t2, m2) = k1, k2
    if m1 == m2:
        if t1 == t2:
            return 1.0
        step = (t2 - t1) % 12
        return {7: 0.8, 5: 0.8, 2: 0.4, 10: 0.4}.get(step, 0.0)
    # normalize to t1=minor tonic, t2=major tonic; relative pair iff major = minor+3
    # (e.g. A-minor tonic 9 is relative of C-major tonic 0: (9+3)%12 == 0).
    if m1 and not m2:
        t1, t2 = t2, t1
    return 0.8 if (t2 == (t1 + 3) % 12) else 0.0


class HybridEngine:
    """Load once, then mix() many times. Reads tracks + clap (+ chroma from features)."""

    # feature.py stores chroma means at offsets 40:52 in the 79-dim vector
    CHROMA_SLICE = (40, 52)
    LIB_DIM = 79            # expected librosa descriptor length (see features.FEATURE_DIM)
    # features.py's FEATURE_GROUPS['texture'] = (71, 78), ordered centroid/bandwidth/
    # rolloff/zcr/rms_mean/rms_std/flatness -> rms_mean (loudness) is offset 71+4 = 75.
    # This is radio_next()'s energy-arc axis: TEACHER_MECHANICS.md B.3 measured it as one
    # of MusicIP's most strongly-held corridor axes (ratio 0.563 vs a matched-CLAP-distance
    # control, second only to brightness/texture).
    RMS_MEAN_INDEX = 75

    # radio_next() energy-arc tuning (see that method's docstring for the corridor math).
    # Measured empirically against the prod-sized library (_scratch-journey, 21,236 tracks,
    # seed pool idx 100, variety=3, n=20): period=20/sigma=0.35 gives rise/fall/wave
    # Spearman rho vs. the target curve of 0.82 / -0.85 / 0.87, and holds a 'flat' pull
    # to a mean |deviation| of 0.25 (population std=1) from the seed's own energy --
    # period=40/sigma=0.8 only reached rho=0.41 for rise (too loose a corridor, and too
    # long a period to show a clear trend within one n=20 batch).
    ARC_PERIOD = 20         # tracks per full rise/fall/wave cycle
    ARC_SIGMA = 0.35        # corridor width, in z-scored rms_mean units (std=1 population)

    def __init__(self, db_path, weights=None):
        self.w = dict(DEFAULT_WEIGHTS)
        cfg = os.path.join(os.path.dirname(db_path), "engine_v2.json")
        if os.path.exists(cfg):
            try:
                self.w.update(json.load(open(cfg)).get("best_weights", {}))
            except Exception:
                pass
        if weights:
            self.w.update(weights)

        conn = _connect_readonly(db_path, timeout=30)
        conn.execute("PRAGMA busy_timeout=15000")
        meta = {p: (a, t, al, g, y) for p, a, t, al, g, y in
                conn.execute("SELECT path,artist,album,title,genre,year FROM tracks")}
        # librosa 79-dim features: full vector (V2 'lib' term), chroma slice (key), tempo.
        a0, b0 = self.CHROMA_SLICE
        lib_vec, chroma, tempo = {}, {}, {}
        try:
            for p, blob, dim, tp in conn.execute(
                    "SELECT path,vec,dim,tempo FROM features WHERE vec IS NOT NULL"):
                v = np.frombuffer(blob, np.float32)
                # only the expected 79-dim descriptor: a stray dim (e.g. a 143-dim
                # experiment row) would break np.vstack, and a non-finite value would
                # poison the z-score stats and NaN out every lib score.
                if dim == self.LIB_DIM and v.shape[0] == dim and np.isfinite(v).all():
                    v = v.astype(np.float64)
                    lib_vec[p] = v
                    chroma[p] = v[a0:b0]
                if tp:
                    tempo[p] = tp
        except sqlite3.OperationalError:
            pass
        try:
            for p, tp in conn.execute("SELECT path,tempo FROM features WHERE tempo IS NOT NULL"):
                tempo.setdefault(p, tp)
        except sqlite3.OperationalError:
            pass
        # CLAP vectors (required)
        clap = {}
        for p, dim, blob in conn.execute("SELECT path,dim,vec FROM clap WHERE vec IS NOT NULL"):
            v = np.frombuffer(blob, np.float32)
            if v.shape[0] == dim:
                clap[p] = v
        conn.close()   # all reads done at load; don't leak the handle / WAL reader
        if not clap:
            raise SystemExit("no CLAP embeddings found — run embed.py first, "
                             "or use the librosa engine (mixer.py)")

        # z-score + L2-normalize the librosa matrix over ALL analyzed tracks, exactly as the
        # V2 tuner did, so the 'lib' cosine is comparable across dimensions.
        self.use_lib = bool(self.w.get("lib")) and len(lib_vec) > 0
        libN = {}
        if self.use_lib:
            lp = list(lib_vec)
            LZ = np.vstack([lib_vec[p] for p in lp])
            mu, sd = LZ.mean(0), LZ.std(0); sd[sd < 1e-9] = 1e-9
            LZ = (LZ - mu) / sd
            nrm = np.linalg.norm(LZ, axis=1, keepdims=True); nrm[nrm < 1e-9] = 1e-9
            LZ = LZ / nrm
            libN = {p: LZ[i] for i, p in enumerate(lp)}

        # engine pool: CLAP tracks; when the lib term is active, restrict to the
        # clap∩librosa joint pool exactly as V2 did (no librosa => can't score lib).
        paths = [p for p in clap if p in libN] if self.use_lib else list(clap)
        if not paths:
            raise SystemExit("no tracks with both CLAP and librosa features — run "
                             "scan.py analyze + embed.py, or set the 'lib' weight to 0")

        self.paths = paths
        self._all_clap = set(clap)                     # every CLAP-known path (superset of pool)
        self.X = np.vstack([clap[p] for p in paths])   # L2-normalized already
        self.L = np.vstack([libN[p] for p in paths]) if self.use_lib else None
        self.idx = {p: i for i, p in enumerate(paths)}
        self.artist = [meta.get(p, (None,)*5)[0] for p in paths]
        self.genre_tags = [_tags(meta.get(p, (None,)*5)[3]) for p in paths]
        self.year = np.array([meta.get(p, (None,)*5)[4] or 0 for p in paths], float)
        self.bpm = np.array([tempo.get(p, 0) or 0 for p in paths], float)
        self.key = _keys_from_chroma_batch([chroma.get(p) for p in paths])
        self.meta = {p: {"artist": meta[p][0], "album": meta[p][1], "title": meta[p][2],
                         "genre": meta[p][3], "year": meta[p][4]} for p in meta}

        # radio_next()'s energy-arc axis: z-scored rms_mean (loudness), computed from
        # lib_vec (EVERY analyzed track, not libN) so the corridor works whether or not
        # the 'lib' weight/pool-restriction is active -- independent of self.use_lib by
        # design. NaN for a pool track with no librosa vector (e.g. lib=0 widened the pool
        # to CLAP-only tracks); radio_next() treats NaN as "unknown, don't bias".
        if lib_vec:
            raw_e = np.array([v[self.RMS_MEAN_INDEX] for v in lib_vec.values()])
            mu_e, sd_e = raw_e.mean(), raw_e.std()
            sd_e = sd_e if sd_e > 1e-9 else 1e-9
            self.energy = np.array(
                [(lib_vec[p][self.RMS_MEAN_INDEX] - mu_e) / sd_e if p in lib_vec else np.nan
                 for p in paths])
            lo, hi = np.nanpercentile(self.energy, [10, 90])
            self._energy_lo, self._energy_hi = float(lo), float(hi)
        else:
            self.energy = np.full(len(paths), np.nan)
            self._energy_lo, self._energy_hi = -1.0, 1.0   # unreachable: no energy data

    def _score(self, si):
        w = self.w
        s = w["clap"] * (self.X @ self.X[si])
        # librosa timbre cosine (the V2 'lib' term)
        if self.use_lib and w.get("lib"):
            s = s + w["lib"] * (self.L @ self.L[si])
        # genre
        st = self.genre_tags[si]
        if w.get("genre"):
            jac = np.array([len(st & g) / len(st | g) if st and g else 0.0
                            for g in self.genre_tags])
            s = s + w["genre"] * jac
        # V2 tempo term: LINEAR |Δbpm|/40 (the eared engine did NOT octave-fold), 0.5 when
        # either tempo is unknown. Octave-folding is a later, un-eared idea — deliberately
        # kept out of the shipped default; the folded form lives in coherence.py's metric.
        if w.get("bpm"):
            sb = self.bpm[si]
            dbpm = np.where((self.bpm > 0) & (sb > 0), np.abs(self.bpm - sb) / 40.0, 0.5)
            s = s - w["bpm"] * dbpm
        # key compatibility (off by default; correct now, but lost by ear)
        if w.get("key") and self.key[si]:
            kc = np.array([_key_compat(self.key[si], kj) for kj in self.key])
            s = s + w["key"] * kc
        # era: |Δyear|/25, 0.5 when either year is unknown (V2 form)
        if w.get("era"):
            sy = self.year[si]
            dera = np.where((self.year > 0) & (sy > 0), np.abs(self.year - sy) / 25.0, 0.5)
            s = s - w["era"] * dera
        return s

    def resolve(self, seed):
        if seed in self.idx:
            return seed
        # a known CLAP track that the lib-restricted pool excluded is NOT mixable here;
        # return None rather than silently basename-matching a DIFFERENT same-named file.
        if seed in self._all_clap:
            return None
        base = os.path.basename(seed).lower()
        hits = [p for p in self.paths if os.path.basename(p).lower() == base]
        return hits[0] if len(hits) == 1 else None

    def mix(self, seed, size=25, artist_spacing=3):
        # GOTCHA (do not "fix" without a human ear test first -- shipped behavior):
        # a candidate skipped below for violating artist_spacing is dropped for good,
        # not deferred/retried later in this single pass -- see eval/harness.py's
        # mix_rank_array() for a fuller writeup of the consequence.
        canon = self.resolve(seed)
        if canon is None:
            return None
        si = self.idx[canon]
        order = np.argsort(-self._score(si))
        out, recent = [], []
        for j in order:
            if j == si:
                continue
            a = self.artist[j]
            if a and a in recent[-artist_spacing:]:
                continue
            out.append(self.paths[j]); recent.append(a)
            if len(out) >= size:
                break
        return out

    def radio_next(self, seed, n=20, exclude=None, variety=0.0, artist_spacing=3,
                   arc="flat", pos=0, rng=None):
        """Journey/Radio mode: one batch of the next `n` tracks for an infinite queue,
        stateless (caller tracks `exclude` + `pos` across calls).

        Reimplements MusicIP Mixer's actual variety mechanism, per the overnight
        teacher-loop reverse-engineering (TEACHER_MECHANICS.md B.1/B.7): variety is
        NOT a diversity/packing constraint, it is i.i.d. Bernoulli thinning of a fixed
        rank list -- `p = 1/(1+variety)` per candidate, single ordered scan, no lookback,
        no walk relative to the previous pick. That is exactly what's below: the SAME
        _score(seed) ranking mix() uses, the SAME artist-spacing drop-for-good walk
        (mix()'s GOTCHA applies here too -- a spacing violator is skipped for good, not
        deferred), plus `exclude` (already played/queued) folded into the skip set.

        variety<=0 degenerates to a DETERMINISTIC top-k walk -- no coin flip, no energy
        bias -- so radio_next(seed, n, exclude=[], variety=0, arc=<anything>) is exactly
        mix(seed, size=n)'s walk (same order, same spacing), which is what the byte-
        identical /api/mix regression gate (and this feature's own verification #1)
        checks against. The energy-arc corridor is an IMPROVEMENT over the teacher (which
        has no corridor at all, see TEACHER_MECHANICS.md B.3's finding that MusicIP DOES
        hold acoustic axes but not by any disclosed corridor mechanism): it only ever
        acts as a second, independent Bernoulli-style accept/reject layered on top of the
        variety coin, so it never fires in the deterministic v<=0 path either.

        arc: 'flat' holds near the SEED's own energy (steady); 'rise'/'fall' sweep
        linearly across the pool's 10th-90th percentile band over ARC_PERIOD tracks;
        'wave' is a sine over the same band/period. `pos` phases the arc across
        repeated calls (t = pos + tracks already accepted in THIS call).

        rng: optional numpy Generator (or anything with .random()) for deterministic
        tests; a fresh np.random.default_rng() is used if not given -- radio is meant
        to vary call to call, unlike mix()'s pinned rank order.
        """
        canon = self.resolve(seed)
        if canon is None:
            return None
        si = self.idx[canon]
        order = np.argsort(-self._score(si))
        rng = rng if rng is not None else np.random.default_rng()

        excl = {si}
        for e in (exclude or []):
            ei = self._as_index(e)
            if ei is not None:
                excl.add(ei)

        variety = max(0.0, float(variety))
        p_variety = 1.0 / (1.0 + variety)

        seed_energy = self.energy[si]
        lo, hi = self._energy_lo, self._energy_hi
        mid, amp = (lo + hi) / 2.0, (hi - lo) / 2.0

        def _target(t):
            frac = (t % self.ARC_PERIOD) / self.ARC_PERIOD
            if arc == "rise":
                return lo + (hi - lo) * frac
            if arc == "fall":
                return hi - (hi - lo) * frac
            if arc == "wave":
                return mid + amp * np.sin(2 * np.pi * frac)
            # 'flat' (or any unrecognised value -- fail toward the safe/inert default):
            # hold at the seed's own energy, or the corridor midpoint if unknown.
            return seed_energy if not np.isnan(seed_energy) else mid

        out, recent = [], []
        for j in order:
            j = int(j)
            if j in excl:
                continue
            if variety > 0:
                if rng.random() >= p_variety:
                    continue                      # variety coin: permanent skip, no lookback
                ej = self.energy[j]
                if not np.isnan(ej):
                    target = _target(pos + len(out))
                    z = (ej - target) / self.ARC_SIGMA
                    accept_prob = float(np.exp(-0.5 * z * z))
                    if rng.random() >= accept_prob:
                        continue                  # corridor coin: also a permanent skip
            a = self.artist[j]
            if a and a in recent[-artist_spacing:]:
                continue
            out.append(self.paths[j]); recent.append(a)
            if len(out) >= n:
                break
        return out

    # ------------------------------------------------------------------
    # Relevance-feedback / re-ranking helpers (additive; V2's mix()/_score()
    # above are untouched, and none of these are called by mix() or main()).
    # ------------------------------------------------------------------

    def _as_index(self, x):
        """Accept either a path (resolved like mix()'s seed) or a raw pool index.
        A raw index is bounds-checked: negative or out-of-range values are NOT passed
        through (numpy would silently wrap a negative index, or a plain > len fancy
        index would raise a confusing IndexError deep in some other call) -- reject
        them here with a clear error instead."""
        if isinstance(x, str):
            canon = self.resolve(x)
            return None if canon is None else self.idx[canon]
        i = int(x)
        if not (0 <= i < len(self.paths)):
            raise ValueError(
                f"track index out of range: {i} (pool has {len(self.paths)} tracks)")
        return i

    def _unit(self, v):
        v = np.asarray(v, dtype=np.float64)
        n = np.linalg.norm(v)
        return v / n if n > 1e-9 else v

    def mix_from_vector(self, q, size=25, artist_spacing=3, exclude=None):
        """Rank the pool by cosine similarity to an arbitrary CLAP-space query vector
        `q` (e.g. a Rocchio-refined vector from refine()), instead of a library seed.
        Same top-K + artist-spacing behaviour as mix(). `exclude` is an optional
        iterable of paths/indices to drop from the results (e.g. tracks already shown)."""
        qn = self._unit(q)
        if np.linalg.norm(qn) < 1e-9:
            raise ValueError("zero query vector")
        s = self.X @ qn
        order = np.argsort(-s)
        excl = set()
        for e in (exclude or []):
            ei = self._as_index(e)
            if ei is not None:
                excl.add(ei)
        out, recent = [], []
        for j in order:
            j = int(j)
            if j in excl:
                continue
            a = self.artist[j]
            if a and a in recent[-artist_spacing:]:
                continue
            out.append(self.paths[j]); recent.append(a)
            if len(out) >= size:
                break
        return out

    def refine(self, seed_or_vec, liked_idx=None, disliked_idx=None, alpha=1.0, beta=0.75):
        """Rocchio relevance feedback: q' = alpha*q0 + beta*mean(liked) - beta*mean(disliked),
        re-normalized to unit length (all CLAP rows in self.X are already unit vectors, so
        this keeps q' comparable to them via plain dot product / mix_from_vector()).

        seed_or_vec: a library path/index (used as-is, i.e. its raw CLAP row) or an
        already-CLAP-space vector to start from.
        liked_idx / disliked_idx: iterables of paths or pool indices (thumbs-up/down).
        """
        if isinstance(seed_or_vec, (str, int, np.integer)):
            i0 = self._as_index(seed_or_vec)
            if i0 is None:
                raise ValueError(f"seed not found/not embedded: {seed_or_vec}")
            q0 = self.X[i0].astype(np.float64)
        else:
            q0 = self._unit(seed_or_vec)

        liked = [self._as_index(x) for x in (liked_idx or [])]
        liked = [i for i in liked if i is not None]
        disliked = [self._as_index(x) for x in (disliked_idx or [])]
        disliked = [i for i in disliked if i is not None]

        q = alpha * q0
        if liked:
            q = q + beta * self.X[liked].mean(axis=0)
        if disliked:
            q = q - beta * self.X[disliked].mean(axis=0)
        return self._unit(q)

    def mmr(self, candidates, k=None, lambda_=0.5, query=None):
        """Maximal Marginal Relevance re-ranking of `candidates` (paths, already ranked
        by relevance, e.g. the output of mix()/mix_from_vector()) for diversity.

        relevance(cand)  = cosine(cand, query) if `query` given, else the candidate's
                            rank position in the input list (first = most relevant);
        diversity(cand)  = max CLAP cosine similarity to an already-selected candidate.
        Greedily picks argmax[ lambda_*relevance - (1-lambda_)*diversity ] until k picked.
        lambda_=1.0 reduces to plain top-k by relevance; lower lambda_ favors diversity.
        """
        idxs = [self.idx[c] for c in candidates if c in self.idx]
        kept = [c for c in candidates if c in self.idx]
        if not idxs:
            return []
        k = len(idxs) if k is None else max(0, min(k, len(idxs)))
        if k == 0:
            return []
        Xc = self.X[idxs]
        if query is not None:
            rel = Xc @ self._unit(query)
        else:
            # no explicit query: trust the caller's input ordering as relevance rank
            rel = np.linspace(1.0, 0.0, num=len(idxs))
        sim = Xc @ Xc.T

        selected = [int(np.argmax(rel))]
        remaining = [i for i in range(len(idxs)) if i != selected[0]]
        while remaining and len(selected) < k:
            sel_arr = np.array(selected)
            best_j, best_score = None, -np.inf
            for j in remaining:
                div = float(np.max(sim[j, sel_arr]))
                score = lambda_ * rel[j] - (1.0 - lambda_) * div
                if score > best_score:
                    best_score, best_j = score, j
            selected.append(best_j)
            remaining.remove(best_j)
        return [kept[i] for i in selected]

    def order_by_flow(self, paths):
        """Greedy nearest-neighbour ordering over CLAP vectors: start at paths[0] and
        repeatedly append the closest not-yet-used track (by CLAP cosine), to minimize
        abrupt back-to-back jumps ("flow") in a mix. Unknown paths are dropped."""
        kept = [p for p in paths if p in self.idx]
        if len(kept) <= 2:
            return kept
        idxs = [self.idx[p] for p in kept]
        Xc = self.X[idxs]
        sim = Xc @ Xc.T
        n = len(kept)
        used = [False] * n
        order = [0]
        used[0] = True
        cur = 0
        for _ in range(n - 1):
            best_j, best_sim = None, -np.inf
            for j in range(n):
                if not used[j] and sim[cur, j] > best_sim:
                    best_sim, best_j = sim[cur, j], j
            order.append(best_j)
            used[best_j] = True
            cur = best_j
        return [kept[i] for i in order]

    def explain(self, seed, cand):
        """Per-term breakdown of _score() for a single (seed, candidate) pair: the same
        weighted clap/lib/genre/bpm/era/key contributions _score() sums over the whole
        pool, computed here for one candidate so comp["total"] == _score(si)[ci]."""
        si, ci = self._as_index(seed), self._as_index(cand)
        if si is None or ci is None:
            raise ValueError("seed/cand not found/not embedded")
        w = self.w
        comp = {"clap": 0.0, "lib": 0.0, "genre": 0.0, "bpm": 0.0, "key": 0.0, "era": 0.0}

        comp["clap"] = float(w["clap"] * (self.X[ci] @ self.X[si]))

        if self.use_lib and w.get("lib"):
            comp["lib"] = float(w["lib"] * (self.L[ci] @ self.L[si]))

        if w.get("genre"):
            st, gt = self.genre_tags[si], self.genre_tags[ci]
            jac = len(st & gt) / len(st | gt) if st and gt else 0.0
            comp["genre"] = float(w["genre"] * jac)

        if w.get("bpm"):
            sb, cb = self.bpm[si], self.bpm[ci]
            dbpm = abs(cb - sb) / 40.0 if (sb > 0 and cb > 0) else 0.5
            comp["bpm"] = float(-w["bpm"] * dbpm)

        if w.get("key") and self.key[si]:
            comp["key"] = float(w["key"] * _key_compat(self.key[si], self.key[ci]))

        if w.get("era"):
            sy, cy = self.year[si], self.year[ci]
            dera = abs(cy - sy) / 25.0 if (sy > 0 and cy > 0) else 0.5
            comp["era"] = float(-w["era"] * dera)

        comp["total"] = comp["clap"] + comp["lib"] + comp["genre"] + comp["bpm"] + comp["key"] + comp["era"]
        return comp


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Attune hybrid (V2) engine: CLAP + librosa + rules")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "mixer.db"))
    ap.add_argument("--seed", required=True)
    ap.add_argument("--size", type=int, default=25)
    a = ap.parse_args()
    eng = HybridEngine(a.db)
    mix = eng.mix(a.seed, a.size)
    if mix is None:
        print("seed not found / not embedded"); raise SystemExit(1)
    for i, p in enumerate(mix, 1):
        m = eng.meta.get(p, {})
        print(f"{i:2d}. {m.get('artist','?')} - {m.get('title', os.path.basename(p))}")


if __name__ == "__main__":
    main()
