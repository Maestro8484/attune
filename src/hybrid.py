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
import os, json, sqlite3
import numpy as np

# V2 — the recipe that WON the human blind listening test (see extracted/human_ratings.md).
# CLAP ears + librosa 'lib' timbre + genre + linear tempo + era. NO key term by default:
# the key idea was sound but lost by ear (and its code was reversed until fixed). The key
# term remains available (set "key">0) now that _key_compat is correct, but ships off.
DEFAULT_WEIGHTS = {"clap": 1.0, "lib": 0.4, "genre": 0.3, "bpm": 0.3, "era": 0.1, "key": 0.0}

# Krumhansl-Schmuckler key profiles (major / minor) for key estimation from chroma.
_KRUM_MAJ = np.array([6.35,2.23,3.48,2.33,4.38,4.09,2.52,5.19,2.39,3.66,2.29,2.88])
_KRUM_MIN = np.array([6.33,2.68,3.52,5.38,2.60,3.53,2.54,4.75,3.98,2.69,3.34,3.17])


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

        conn = sqlite3.connect(db_path, timeout=30)
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
        self.key = [_key_from_chroma(chroma.get(p)) for p in paths]
        self.meta = {p: {"artist": meta[p][0], "album": meta[p][1], "title": meta[p][2],
                         "genre": meta[p][3], "year": meta[p][4]} for p in meta}

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
