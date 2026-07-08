"""The mixing engine: seed track(s) -> ranked, listenable playlist.

Replaces MusicIP's closed distance algorithm with a transparent, tunable one over
the acoustic descriptors from features.py.

Knobs (mirroring MusicIP):
  style   0..100  weight of timbre vs. the rest (low = pure timbre match,
                  high = let harmony/rhythm/texture matter more -> broader "style")
  variety 0..9    0 = strict nearest-neighbours; higher widens the candidate pool
                  and samples from it, so the queue explores further from the seed
  size            number of tracks
  artist_spacing  minimum gap between tracks by the same artist (MusicIP "restrict")

Normalization: features are z-scored per dimension across the analyzed library so no
single high-variance dimension dominates the euclidean distance.
"""
from __future__ import annotations
import os
import numpy as np
from features import FEATURE_GROUPS, FEATURE_DIM


class Engine:
    def __init__(self, paths, matrix, meta):
        self.paths = paths
        self.meta = meta
        self.index = {p: i for i, p in enumerate(paths)}
        # tolerant lookup: normalize separators + case so a seed given as
        # "a/b/x.mp3" resolves against a stored "A\\B\\x.mp3", and vice versa.
        self._norm_index = {self._norm(p): i for i, p in enumerate(paths)}
        X = np.asarray(matrix, dtype=np.float64)
        self.mu = X.mean(axis=0)
        self.sd = X.std(axis=0)
        self.sd[self.sd < 1e-9] = 1e-9
        self.Z = (X - self.mu) / self.sd          # normalized features [N,dim]

    def _group_weights(self, style: float) -> np.ndarray:
        """Per-dimension weight vector. style in 0..100.
        Low style -> emphasize timbre (MFCC). High style -> flatten weights so
        harmony/rhythm/texture contribute more (a looser, more 'stylistic' match)."""
        s = np.clip(style, 0, 100) / 100.0
        w = np.ones(FEATURE_DIM)
        # timbre weight goes from 2.5 (s=0) down to 1.0 (s=1)
        timbre_w = 2.5 - 1.5 * s
        # everything-else weight goes from 0.6 (s=0) up to 1.1 (s=1)
        other_w = 0.6 + 0.5 * s
        for g, (a, b) in FEATURE_GROUPS.items():
            if g == "timbre":
                w[a:b] = timbre_w
            elif g == "tempo":
                # tempo kept modest; scale raw bpm down so it doesn't dominate z-space
                w[a:b] = 0.4 + 0.6 * s
            else:
                w[a:b] = other_w
        return w

    def _distances(self, seed_idx, style):
        w = self._group_weights(style)
        Zw = self.Z * w
        seed = Zw[seed_idx]
        # euclidean in weighted normalized space
        d = np.sqrt(((Zw - seed) ** 2).sum(axis=1))
        return d

    @staticmethod
    def _norm(p):
        return os.path.normcase(str(p).replace("\\", "/"))

    def resolve(self, seed_path):
        """Return the canonical stored path for a seed given as absolute or relative,
        in any separator/case; falls back to a unique basename match. None if no match."""
        if seed_path in self.index:
            return seed_path
        for cand in (seed_path, os.path.abspath(seed_path)):
            i = self._norm_index.get(self._norm(cand))
            if i is not None:
                return self.paths[i]
        # last resort: unique basename match
        base = self._norm(os.path.basename(seed_path))
        hits = [p for p in self.paths if self._norm(os.path.basename(p)) == base]
        return hits[0] if len(hits) == 1 else None

    def rank(self, seed_path, style=40.0):
        """Return list of (path, distance) sorted nearest-first, excluding the seed."""
        canon = self.resolve(seed_path)
        if canon is None:
            return None
        si = self.index[canon]
        d = self._distances(si, style)
        order = np.argsort(d)
        out = [(self.paths[i], float(d[i])) for i in order if i != si]
        return out

    def mix(self, seed_path, size=25, style=40.0, variety=0, artist_spacing=3, rng=None):
        """Build a playlist. Deterministic when variety=0; variety>0 samples from a
        widened candidate pool using rng (np.random.Generator)."""
        ranked = self.rank(seed_path, style)
        if ranked is None:
            return None
        if rng is None:
            rng = np.random.default_rng(0)

        v = np.clip(variety, 0, 9) / 9.0
        # candidate pool: v=0 -> tight (top size*1); v=1 -> wide (top size*8)
        pool_mult = 1 + int(round(v * 7))
        pool = ranked[: max(size * pool_mult, size)]

        chosen, recent_artists = [], []

        def _too_close(art):
            # artist_spacing<=0 means "no restriction"; guard against recent[-0:] == the
            # WHOLE list (which would ban every artist that ever appeared).
            return bool(art) and artist_spacing > 0 and art in recent_artists[-artist_spacing:]

        if v == 0:
            # strict nearest-neighbour walk with artist spacing
            for path, dist in ranked:
                art = (self.meta.get(path, {}) or {}).get("artist")
                if _too_close(art):
                    continue
                chosen.append((path, dist))
                recent_artists.append(art)
                if len(chosen) >= size:
                    break
        else:
            # sample from pool with probability decaying by rank; temperature ~ variety
            idxs = np.arange(len(pool))
            temp = 3 + v * 30
            probs = np.exp(-idxs / temp)
            probs /= probs.sum()
            avail = list(idxs)
            guard = 0
            while len(chosen) < size and avail and guard < size * 50:
                guard += 1
                p_avail = probs[avail] / probs[avail].sum()
                pick = int(rng.choice(avail, p=p_avail))
                path, dist = pool[pick]
                art = (self.meta.get(path, {}) or {}).get("artist")
                # drop the pick from the pool FIRST, so an artist-rejected pick is never
                # redrawn (the old bug: `continue` without removing burned the guard budget
                # re-picking the same track). If we're running low we accept anyway.
                avail.remove(pick)
                if _too_close(art) and len(avail) + len(chosen) > size:
                    continue
                chosen.append((path, dist))
                recent_artists.append(art)
        return chosen


def build_engine(db_path):
    import db as dbm
    conn = dbm.connect(db_path)
    paths, mat, meta = dbm.load_matrix(conn)
    if len(paths) == 0:
        raise SystemExit("no analyzed tracks in DB — run scan.py analyze first")
    return Engine(paths, mat, meta)


def main():
    import argparse
    ap = argparse.ArgumentParser(description="Attune librosa engine: seed -> playlist")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "mixer.db"))
    ap.add_argument("--seed", required=True)
    ap.add_argument("--size", type=int, default=25)
    ap.add_argument("--style", type=float, default=40)
    ap.add_argument("--variety", type=int, default=0)
    a = ap.parse_args()
    eng = build_engine(a.db)
    mix = eng.mix(a.seed, a.size, a.style, a.variety)
    if mix is None:
        print("seed not found in analyzed set"); raise SystemExit(1)
    for i, (p, d) in enumerate(mix, 1):
        m = eng.meta.get(p, {})
        print(f"{i:2d}. d={d:5.2f}  {m.get('artist','?')} - {m.get('title', os.path.basename(p))}")


if __name__ == "__main__":
    main()
