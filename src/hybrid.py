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

This is **V2**, the recipe that came out ahead of genuine MusicIP in a five-seed, partly
blind A/B/C/V2/V3 listening test (human_ratings.md, in the private MusicIP research
folder, not in this repository; docs/ENGINES.md has the public account). Notes from that result:
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

# V2 — the recipe that came out ahead in the partly blind listening test (human_ratings.md,
# in the private MusicIP research folder, not in this repository).
# CLAP ears + librosa 'lib' timbre + genre + linear tempo + era. NO key term by default:
# the key idea was sound but lost by ear (and its code was reversed until fixed). The key
# term remains available (set "key">0) now that _key_compat is correct, but ships off.
DEFAULT_WEIGHTS = {"clap": 1.0, "lib": 0.4, "genre": 0.3, "bpm": 0.3, "era": 0.1, "key": 0.0,
                   "feel": 0.0}

# Scoring switches (2026-10-01). ALL OFF BY DEFAULT: with fusion "raw", clap_space "raw"
# and the feel weight at 0, _score() runs the V2 code it always ran and the byte-identical
# regression gate holds it there. None of them becomes a default on a number (LAW 1); each
# waits on a blind listening test.
#
#   fusion      how the ingredients are put on one scale before their weights apply.
#               "raw"  add the raw numbers (V2 as eared).
#               "z"    each ingredient as distance from its own library average, in units
#                      of its own spread, for this seed.
#               "rank" each ingredient as its percentile in the library for this seed, so a
#                      weight is a share of say rather than a multiplier on whatever range
#                      the raw number happens to have. This is the search-engine practice
#                      of normalizing scores before combining them (CombSUM score fusion).
#   clap_space  which form of the stored fingerprint the clap term compares.
#               "raw"       the stored vectors.
#               "centered"  the library's average fingerprint subtracted, length 1 again.
#               "abtt1..3"  centered, then the 1 to 3 strongest shared directions removed
#                           ("all-but-the-top", Mu and Viswanath 2018).
#               Computed in memory at load from the stored vectors; nothing is written.
#   feel        weight of the feel-closeness ingredient: how near a candidate's five feel
#               scores (danceable, happy, intense, instrumental, acoustic) sit to the
#               seed's. The scores come from the stored fingerprints and a small file of
#               text directions, models/feel_prompts.json; see _load_feel().
FUSIONS = ("raw", "z", "rank")
CLAP_SPACES = ("raw", "centered", "abtt1", "abtt2", "abtt3")
FEEL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models",
                         "feel_prompts.json")

# The fit line: the count becomes a MAXIMUM, not a quota, when a walk is given `min_fit`.
# Both values are PROVISIONAL starting points (LAW 1: where "fits" ends is judged by ear,
# never by a retrieval number). They are NOT tuned. They are applied on top of the scores
# above, which are computed exactly as before.
#
#   FIT_LINE_DEFAULT  applies to the weighted walks (mix, radio): fit is the candidate's
#                     score divided by the seed's own score against itself, so 1.0 is a
#                     perfect twin under the active weights and the line means the same
#                     thing under every recipe. 0.70 is a starting point placed inside
#                     the range the scores actually occupy (measured 2026-09-30, see
#                     audit-collections/harness/score_shape.txt), so the switch does
#                     something on day one. It is NOT justified by that measurement or
#                     by any rank or retrieval figure: whether 0.70 is where "fits"
#                     ends is a listening decision, and the listening set is staged.
#   CLAP_LINE_DEFAULT applies to the CLAP-only walks (blend, steering, adventure), where
#                     the number is the plain cosine. Same status: a starting point in
#                     the occupied range. The CLAP cosines of a whole library sit between
#                     about 0.88 and 1.00, so this line has little to bite on; that is a
#                     finding about the stored vectors, reported in ISSUES.md, not
#                     something this file changes.
FIT_LINE_DEFAULT = 0.70
CLAP_LINE_DEFAULT = 0.99

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


def _pct(x):
    """Each value's place in x as 0..1 (0 = lowest, 1 = highest); tied values share the
    average of the places they cover, so a column that is all one value reads 0.5
    throughout and has no say in an order."""
    x = np.asarray(x, np.float64)
    n = x.shape[0]
    if n < 2:
        return np.full(n, 0.5)
    _, inv, cnt = np.unique(x, return_inverse=True, return_counts=True)
    avg = np.cumsum(cnt) - (cnt + 1) / 2.0      # 0-based average place of each tied group
    return (avg / (n - 1))[inv]


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
    CLAP_DIM = 512          # laion/larger_clap_music embedding length (LAW 2: never swapped).
                            # Only used to shape an EMPTY pool's matrix -- a pool with any
                            # track at all takes its width from the stored vectors, so this
                            # constant can never silently reshape real data.
    # features.py's FEATURE_GROUPS['texture'] = (71, 78), ordered centroid/bandwidth/
    # rolloff/zcr/rms_mean/rms_std/flatness -> rms_mean (loudness) is offset 71+4 = 75.
    # This is radio_next()'s energy-arc axis: measured as one of MusicIP's most
    # strongly-held corridor axes (ratio 0.563 vs a matched-CLAP-distance control,
    # second only to brightness/texture). Recorded in TEACHER_MECHANICS.md B.3, in
    # the private MusicIP research folder, not in this repository.
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

    def __init__(self, db_path, weights=None, fusion=None, clap_space=None, feel_file=None):
        self.w = dict(DEFAULT_WEIGHTS)
        self.fusion, self.clap_space = "raw", "raw"
        cfg = os.path.join(os.path.dirname(db_path), "engine_v2.json")
        if os.path.exists(cfg):
            try:
                saved = json.load(open(cfg))
                self.w.update(saved.get("best_weights", {}))
                # the scoring switches ride the same file as the weights; a value this
                # engine does not know is ignored rather than trusted
                sc = saved.get("scoring", {})
                if sc.get("fusion") in FUSIONS:
                    self.fusion = sc["fusion"]
                if sc.get("clap_space") in CLAP_SPACES:
                    self.clap_space = sc["clap_space"]
            except Exception:
                pass
        if weights:
            self.w.update(weights)
        if fusion is not None:
            self.fusion = fusion
        if clap_space is not None:
            self.clap_space = clap_space
        if self.fusion not in FUSIONS:
            raise ValueError(f"fusion must be one of {FUSIONS}, not {self.fusion!r}")
        if self.clap_space not in CLAP_SPACES:
            raise ValueError(f"clap_space must be one of {CLAP_SPACES}, not {self.clap_space!r}")

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
        # CLAP vectors. AN EMPTY POOL IS A NORMAL STATE, not an error: a brand-new
        # install has a database with no `clap` table at all until the first scan
        # reaches its embed stage. This used to SystemExit, which is what made the app
        # unopenable on a machine that had never analyzed anything.
        #
        # THE MISSING TABLE IS ASKED ABOUT, NOT CAUGHT. A bare `except
        # sqlite3.OperationalError` around the read would also swallow "no such column:
        # vec" on a clap table of the wrong shape, and turn a real fault on a real
        # library into a silent empty pool -- the loud-failure-to-quiet-wrong-answer
        # trade this project refuses. Asking sqlite_master separates "nothing embedded
        # yet", which is normal, from "this table is wrong", which is not and still
        # raises. (Raised by the Codex audit of this change, 2026-09-20.)
        clap = {}
        if conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='clap'"
                        ).fetchone():
            for p, dim, blob in conn.execute("SELECT path,dim,vec FROM clap WHERE vec IS NOT NULL"):
                v = np.frombuffer(blob, np.float32)
                if v.shape[0] == dim:
                    clap[p] = v
        conn.close()   # all reads done at load; don't leak the handle / WAL reader

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
        # Verified-missing files leave the pool (ruling C6, 2026-08-04): a row whose
        # audio is gone can only ever serve dead picks. `filestate` is libverify.py's
        # additive table -- absent (older DB) means no exclusions; relinking or a
        # fresh verify clears the flag and the next reload restores the track.
        # Fresh handle on purpose: `conn` above is already closed by here.
        fs_conn = _connect_readonly(db_path, timeout=30)
        try:
            gone = {p for (p,) in fs_conn.execute(
                "SELECT path FROM filestate WHERE missing=1")}
        except sqlite3.OperationalError:
            gone = set()          # no filestate table in this DB
        finally:
            fs_conn.close()
        if gone:
            n0 = len(paths)
            paths = [p for p in paths if p not in gone]
            if n0 != len(paths):
                print(f"pool: excluded {n0 - len(paths)} verified-missing file(s)")
        if not paths:
            # Empty pool: name WHICH empty state this is, once, and carry on. Nothing is
            # mixable until a scan runs, every route answers emptily rather than failing,
            # and the first-run wizard is the whole point of the window being open.
            #
            # Saying which one matters. "No music at all" and "music imported but never
            # embedded" look identical from outside -- both are a window with nothing in
            # it -- and they need completely different things done about them. This used
            # to be a SystemExit that took the whole app down with one message covering
            # all three.
            if not meta:
                print("pool: 0 tracks — the library is empty, nothing scanned yet")
            elif not clap:
                print(f"pool: 0 tracks — {len(meta)} in the library, none embedded yet. "
                      f"Run the scan again to finish its last stage.")
            else:
                print(f"pool: 0 tracks — {len(meta)} in the library, but none has both an "
                      f"embedding and an analysis. See Library, Not Mixable.")

        self.paths = paths
        self._all_clap = set(clap)                     # every CLAP-known path (superset of pool)
        # np.vstack refuses an empty list, so an empty pool gets explicitly-shaped
        # zero-row matrices instead. Shape, not just emptiness, matters: code downstream
        # reads .shape[1].
        self.X = (np.vstack([clap[p] for p in paths]) if paths
                  else np.zeros((0, self.CLAP_DIM), np.float32))   # L2-normalized already
        self.L = None
        if self.use_lib:
            self.L = (np.vstack([libN[p] for p in paths]) if paths
                      else np.zeros((0, self.LIB_DIM), np.float64))
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
        if lib_vec and paths:
            raw_e = np.array([v[self.RMS_MEAN_INDEX] for v in lib_vec.values()])
            mu_e, sd_e = raw_e.mean(), raw_e.std()
            sd_e = sd_e if sd_e > 1e-9 else 1e-9
            self.energy = np.array(
                [(lib_vec[p][self.RMS_MEAN_INDEX] - mu_e) / sd_e if p in lib_vec else np.nan
                 for p in paths])
            # np.nanpercentile over an all-NaN slice warns and returns nan; that can
            # only happen when the lib weight widened the pool to CLAP-only tracks.
            # `paths` non-empty is guaranteed above, so the empty-array case is gone.
            if np.isnan(self.energy).all():
                self._energy_lo, self._energy_hi = -1.0, 1.0
            else:
                lo, hi = np.nanpercentile(self.energy, [10, 90])
                self._energy_lo, self._energy_hi = float(lo), float(hi)
        else:
            self.energy = np.full(len(paths), np.nan)
            self._energy_lo, self._energy_hi = -1.0, 1.0   # unreachable: no energy data

        # The other forms of the fingerprint, built on first use and kept; and the five
        # feel scores, one 0..1 percentile per song, or None when the file is absent.
        self._spaces = {}
        self.feel, self.feel_names, self.feel_labels = None, [], []
        self._load_feel(feel_file)

    def _clap_matrix(self, name):
        """The fingerprint matrix in the named space (see CLAP_SPACES): unit rows, same
        row order as self.X. self.X itself is never changed, so every walk that reads it
        directly (blend, steering, adventure, flow) is exactly as it was."""
        if name == "raw" or self.X.shape[0] < 2:
            return self.X
        M = self._spaces.get(name)
        if M is None:
            Xc = self.X.astype(np.float64)
            Xc = Xc - Xc.mean(0)
            d = CLAP_SPACES.index(name) - 1          # centered 0, abtt1 1, abtt2 2, abtt3 3
            if d > 0 and Xc.shape[0] > d:
                # the d strongest shared directions of what centering left: the top
                # eigenvectors of the 512x512 covariance (eigh returns them ascending)
                _, vecs = np.linalg.eigh(Xc.T @ Xc)
                U = vecs[:, -d:]
                Xc = Xc - (Xc @ U) @ U.T
            nrm = np.linalg.norm(Xc, axis=1, keepdims=True); nrm[nrm < 1e-9] = 1e-9
            M = (Xc / nrm).astype(np.float32)
            self._spaces[name] = M
        return M

    def _load_feel(self, path=None):
        """Five listener-meaningful scores per song, from the stored fingerprints alone.

        The fingerprint model was trained on sound and text together, so a description
        such as "dance beat, steady tempo" has a direction in the same space as the
        audio. A song's score is how much nearer its fingerprint sits to the positive
        description than to the negative one, turned into its percentile in this library
        (0 = least, 1 = most). The approach and the starting prompt pairs are Music
        Assistant's Sonic Analysis provider (Apache-2.0; see NOTICE.md).

        The file holds the directions already computed, so no text model is ever loaded
        or shipped. Each score carries either "pos"/"neg" (lists of text embeddings,
        averaged per side) or a ready direction "w"; "space" names the form of the
        fingerprint they are compared with. A missing or unreadable file means no feel
        scores, which is not an error: the feel weight then has nothing to act on."""
        path = path or FEEL_FILE
        try:
            with open(path, encoding="utf-8") as fh:
                spec = json.load(fh)
        except (OSError, ValueError):
            return
        if not self.paths:
            return
        space = spec.get("space", "raw")
        if space not in CLAP_SPACES:
            return
        M = self._clap_matrix(space).astype(np.float64)
        names, labels, cols = [], [], []
        for sc in spec.get("scores", []):
            if "w" in sc:
                wv = np.asarray(sc["w"], np.float64)
            else:
                wv = (np.asarray(sc["pos"], np.float64).reshape(-1, M.shape[1]).mean(0)
                      - np.asarray(sc["neg"], np.float64).reshape(-1, M.shape[1]).mean(0))
            if wv.shape != (M.shape[1],):
                continue
            names.append(sc["name"]); labels.append(sc.get("label", sc["name"]))
            cols.append(_pct(M @ wv))
        if cols:
            self.feel = np.column_stack(cols).astype(np.float32)
            self.feel_names, self.feel_labels = names, labels
        if space != self.clap_space:
            self._spaces.pop(space, None)     # only needed for this pass; do not hold it

    def _switched(self):
        """True when any scoring switch is on, i.e. when _score() leaves the V2 path."""
        return (self.fusion != "raw" or self.clap_space != "raw"
                or bool(self.w.get("feel") and self.feel is not None))

    def _terms(self, si):
        """The active ingredients of a switched score, in the order they are added: each
        as (name, weight, an array over the pool where BIGGER IS BETTER). The gaps (tempo,
        year, feel) are therefore negated here, which under "raw" fusion gives exactly the
        subtraction V2 writes."""
        w, out = self.w, []
        if w.get("clap"):
            M = self._clap_matrix(self.clap_space)
            out.append(("clap", w["clap"], (M @ M[si]).astype(np.float64)))
        if self.use_lib and w.get("lib"):
            out.append(("lib", w["lib"], self.L @ self.L[si]))
        if w.get("genre"):
            st = self.genre_tags[si]
            out.append(("genre", w["genre"],
                        np.array([len(st & g) / len(st | g) if st and g else 0.0
                                  for g in self.genre_tags])))
        if w.get("bpm"):
            sb = self.bpm[si]
            out.append(("bpm", w["bpm"],
                        -np.where((self.bpm > 0) & (sb > 0), np.abs(self.bpm - sb) / 40.0, 0.5)))
        if w.get("key") and self.key[si]:
            out.append(("key", w["key"],
                        np.array([_key_compat(self.key[si], kj) for kj in self.key])))
        if w.get("era"):
            sy = self.year[si]
            out.append(("era", w["era"],
                        -np.where((self.year > 0) & (sy > 0), np.abs(self.year - sy) / 25.0, 0.5)))
        if w.get("feel") and self.feel is not None:
            # mean gap across the five scores, each already 0..1
            out.append(("feel", w["feel"],
                        -np.abs(self.feel - self.feel[si]).mean(axis=1).astype(np.float64)))
        return out

    def _fuse(self, g):
        """Put one ingredient on the common scale the fusion mode names."""
        if self.fusion == "rank":
            return _pct(g)
        if self.fusion == "z":
            sd = g.std()
            return (g - g.mean()) / sd if sd > 1e-12 else np.zeros_like(g)
        return g

    def _score_switched(self, si):
        s = np.zeros(len(self.paths))
        for _name, wk, g in self._terms(si):
            s = s + wk * self._fuse(g)
        return s

    def _score(self, si):
        if self._switched():
            return self._score_switched(si)
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

    def _walk(self, order, scores, size, artist_spacing, excl, allowed=None, min_fit=None,
              ceiling=1.0, report=None, coin=None):
        """The one best-first walk every list in this engine is built by.

        `order` is pool indices best-first, `scores` the number each was ranked by.
        Candidates in `excl` are skipped. With `allowed` (a boolean array over the pool,
        the collection mask), a candidate outside it is skipped and the pool the walk
        reports is the mask's size, not the library's (LAW 3). A candidate whose fit
        (score / ceiling) is below `min_fit` ENDS the walk: the order is best-first, so
        everything after it is below the line too. That is what makes the count a
        maximum instead of a quota. Without `min_fit` the walk is exactly the loop
        mix() has always run, and the byte-identical regression gate holds it there.

        `coin(j)`, when given, is radio's variety and energy-corridor thinning: it says
        whether to keep candidate j, and runs AFTER the line check so the coins only
        ever choose among songs that fit.

        GOTCHA (do not "fix" without a human ear test first -- shipped behavior): a
        candidate skipped for violating artist_spacing is dropped for good, not
        deferred/retried later in this single pass -- see eval/harness.py's
        mix_rank_array() for a fuller writeup of the consequence.

        `report`, an optional dict, is filled with why the walk stopped and what sat on
        each side of the line, for the window to say in one sentence:
          stopped:        'size' (asked-for count reached), 'fit' (the next-best song sat
                          below the line), 'exhausted' (the pool ran out)
          pool:           how many songs the walk could choose from
          weakest_kept:   (pool index, fit) of the last song taken
          strongest_left: (pool index, fit) of the best song the line kept out
          fit:            {pool index: fit} for every song taken
        """
        out, recent = [], []
        fit_of = {}
        stopped = "exhausted"
        strongest_left = None
        coin_skipped = 0            # songs that fit but radio's coins passed over
        coin_left = None            # the best of those, (index, fit)
        pos = None                  # where in `order` the walk stopped
        for k, j in enumerate(order):
            j = int(j)
            if j in excl:
                continue
            if allowed is not None and not allowed[j]:
                continue
            if min_fit is not None:
                fit = float(scores[j]) / ceiling
                if fit < min_fit:
                    strongest_left = (j, fit)
                    stopped = "fit"
                    pos = k
                    break
            if coin is not None and not coin(j, len(out)):
                coin_skipped += 1
                if coin_left is None and min_fit is not None:
                    coin_left = (j, float(scores[j]) / ceiling)   # best song the coins passed over
                continue
            a = self.artist[j]
            if a and a in recent[-artist_spacing:]:
                continue
            out.append(self.paths[j]); recent.append(a)
            if min_fit is not None:
                fit_of[j] = float(scores[j]) / ceiling
            if len(out) >= size:
                stopped = "size"
                pos = k
                break
        if report is not None:
            # A full list left out its next-best song for COUNT, not for the line; name
            # it too, so a reader can see what the 51st would have been. Found by
            # scanning on from where the walk stopped to the next song it could have
            # taken; the picks above are already final and this changes nothing.
            left_for = "count" if stopped == "size" else "line"
            if stopped == "size" and min_fit is not None and pos is not None:
                for j in order[pos + 1:]:
                    j = int(j)
                    if j in excl or (allowed is not None and not allowed[j]):
                        continue
                    strongest_left = (j, float(scores[j]) / ceiling)
                    # exactly `size` songs fit: the next one was left out by the line,
                    # not by the count (cold reader, round two)
                    if strongest_left[1] < min_fit:
                        left_for = "line"
                    break
            report.update({
                "stopped": stopped,
                "pool": int(allowed.sum()) if allowed is not None else len(self.paths),
                "line": min_fit, "ceiling": float(ceiling),
                "weakest_kept": ((self.idx[out[-1]], fit_of[self.idx[out[-1]]])
                                 if out and min_fit is not None else None),
                "strongest_left": strongest_left,
                "left_for": left_for,
                "coin_skipped": coin_skipped,
                "coin_left": coin_left,
                "fit": fit_of,
            })
        return out

    def mix(self, seed, size=25, artist_spacing=3, allowed=None, min_fit=None, report=None):
        """Best-first playlist for one seed. See _walk() for `allowed` (a collection
        mask), `min_fit` (the count becomes a maximum) and `report` (why it stopped).
        With neither, this is the walk that has shipped since V2, unchanged."""
        canon = self.resolve(seed)
        if canon is None:
            return None
        si = self.idx[canon]
        s = self._score(si)
        order = np.argsort(-s)
        # The seed's own score against itself is the best any song could do under the
        # active weights, so fit = score / that is 1.0 for a perfect twin.
        ceiling = float(s[si]) if min_fit is not None and s[si] > 1e-9 else 1.0
        return self._walk(order, s, size, artist_spacing, {si}, allowed=allowed,
                          min_fit=min_fit, ceiling=ceiling, report=report)

    def radio_next(self, seed, n=20, exclude=None, variety=0.0, artist_spacing=3,
                   arc="flat", pos=0, rng=None, allowed=None, min_fit=None, report=None):
        """Journey/Radio mode: one batch of the next `n` tracks for an infinite queue,
        stateless (caller tracks `exclude` + `pos` across calls).

        Reimplements MusicIP Mixer's actual variety mechanism, per the overnight
        teacher-loop measurement of the running program through its own local HTTP
        API (recorded in TEACHER_MECHANICS.md B.1/B.7, in the private MusicIP
        research folder, not in this repository; see NOTICE.md section 4): variety is
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
        has no corridor at all, see TEACHER_MECHANICS.md B.3 in that same private
        folder, and its finding that MusicIP DOES hold acoustic axes but not by any
        disclosed corridor mechanism): it only ever acts as a second, independent
        Bernoulli-style accept/reject layered on top of the variety coin, so it never fires in the deterministic v<=0 path either.

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
        s = self._score(si)
        order = np.argsort(-s)
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

        # The two coins, as a keep/skip test the shared walk runs per candidate. `taken`
        # is how many this call has accepted so far, handed in by the walk, so the arc
        # phases exactly as the inline loop did with len(out).
        def coin(j, taken):
            if variety <= 0:
                return True
            if rng.random() >= p_variety:
                return False                      # variety coin: permanent skip, no lookback
            ej = self.energy[j]
            if not np.isnan(ej):
                target = _target(pos + taken)
                z = (ej - target) / self.ARC_SIGMA
                accept_prob = float(np.exp(-0.5 * z * z))
                if rng.random() >= accept_prob:
                    return False                  # corridor coin: also a permanent skip
            return True

        ceiling = float(s[si]) if min_fit is not None and s[si] > 1e-9 else 1.0
        return self._walk(order, s, n, artist_spacing, excl, allowed=allowed,
                          min_fit=min_fit, ceiling=ceiling, report=report, coin=coin)

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

    def mix_from_vector(self, q, size=25, artist_spacing=3, exclude=None, allowed=None,
                        min_fit=None, report=None):
        """Rank the pool by cosine similarity to an arbitrary CLAP-space query vector
        `q` (e.g. a Rocchio-refined vector from refine()), instead of a library seed.
        Same top-K + artist-spacing behaviour as mix(). `exclude` is an optional
        iterable of paths/indices to drop from the results (e.g. tracks already shown).
        `allowed`, `min_fit`, `report`: see _walk(). Here fit is the plain cosine (the
        query is a unit vector, so a perfect twin scores 1.0)."""
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
        return self._walk(order, s, size, artist_spacing, excl, allowed=allowed,
                          min_fit=min_fit, ceiling=1.0, report=report)

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

    def mix_multi(self, seeds, size=25, artist_spacing=3, allowed=None, min_fit=None,
                  report=None):
        """Blend mix: rank the pool against the unit-mean of 2+ seeds' CLAP rows --
        refine()'s liked-centroid math with no starting seed -- via the same
        mix_from_vector() walk. Returns (paths, cohesion). `cohesion` is the seeds'
        mean pairwise cosine (rows of X are unit vectors, so V @ V.T is cosine):
        near 1.0 the seeds agree, near 0 the centroid sits in thin space and the
        caller should SAY so rather than serve it quietly (ruling A4). CLAP-only on
        purpose, like refine(): _score()'s genre/bpm/era terms have no meaning for
        a virtual seed (ruling A3). Deterministic for a given seed set."""
        idxs = []
        for s in seeds:
            i = self._as_index(s)
            if i is None:
                return None, None
            idxs.append(i)
        if len(idxs) < 2:
            raise ValueError("mix_multi needs at least 2 seeds")
        V = self.X[idxs].astype(np.float64)
        sims = V @ V.T
        n = len(idxs)
        cohesion = float((sims.sum() - np.trace(sims)) / (n * (n - 1)))
        picks = self.mix_from_vector(V.mean(axis=0), size=size,
                                     artist_spacing=artist_spacing, exclude=idxs,
                                     allowed=allowed, min_fit=min_fit, report=report)
        return picks, cohesion

    def adventure(self, a, b, size=25, artist_spacing=3, allowed=None, min_fit=None,
                  report=None, liked=None, disliked=None, keep=None, exclude=None,
                  beta=0.75):
        """Ordered path FROM a TO b: normalized-lerp waypoints along the CLAP-space
        segment between the two seeds, each snapped to the nearest not-yet-used
        track (mix_from_vector over a shortlist). Returns the full ordered path
        INCLUDING both endpoints; monotone because the waypoints are. Deterministic
        for (a, b, size). Artist spacing cannot ride mix_from_vector's own walk
        across separate size=1 calls (each call starts a fresh `recent`), so it is
        enforced here over the accumulating path instead.

        Adventure is built to wander: the middle songs are MEANT to be unlike both
        ends, so a fit line against either seed would be the wrong shape. What can
        fit or not fit is each STOP: how close the snapped song sits to its waypoint
        (cosine, 1.0 = exactly on the line between the two). With `min_fit` a stop
        whose nearest song is below the line is left out and the path has one fewer
        stop, instead of being padded with whatever was nearest however far. The two
        endpoints are always kept. `allowed` restricts the middle to a collection.

        Steering an Adventure (2026-10-01) keeps it an Adventure instead of turning it
        into a plain mix from its start. All four are optional; with none of them the
        walk is exactly the one above.
          liked / disliked: pool indices whose CLAP rows bend every waypoint, by the same
            Rocchio step refine() takes: wp' = unit(wp) + beta*mean(liked)
            - beta*mean(disliked). The ends stay where they are.
          keep: pool indices that must be on the path (songs voted "more like this").
            Each sits right after the stop whose waypoint it is closest to.
          exclude: pool indices never picked (removed songs, blocked artists' songs,
            songs voted "less like this")."""
        ia, ib = self._as_index(a), self._as_index(b)
        if ia is None or ib is None:
            return None
        if ia == ib:
            raise ValueError("adventure needs two different tracks")
        size = max(int(size), 3)
        va = self.X[ia].astype(np.float64)
        vb = self.X[ib].astype(np.float64)
        keep = [k for k in dict.fromkeys(self._as_index(x) for x in (keep or []))
                if k is not None and k not in (ia, ib)]
        steer = None
        lk = [i for i in (self._as_index(x) for x in (liked or [])) if i is not None]
        dk = [i for i in (self._as_index(x) for x in (disliked or [])) if i is not None]
        if lk or dk:
            steer = np.zeros(self.X.shape[1])
            if lk:
                steer = steer + beta * self.X[lk].astype(np.float64).mean(axis=0)
            if dk:
                steer = steer - beta * self.X[dk].astype(np.float64).mean(axis=0)
        used, middle = [ia, ib] + keep, []
        for x in (exclude or []):
            xi = self._as_index(x)
            if xi is not None and xi not in (ia, ib):
                used.append(xi)
        stops = []                                  # (unit waypoint, index in middle)
        recent = [self.artist[ia]]
        skipped, strongest_left, fit_of = 0, None, {}
        ran_out = False
        for t in np.linspace(0.0, 1.0, size)[1:-1]:
            wp = (1.0 - t) * va + t * vb
            if steer is not None:
                wp = self._unit(wp) + steer
            cands = self.mix_from_vector(wp, size=artist_spacing + 1, exclude=used,
                                         allowed=allowed)
            if not cands:
                ran_out = True                     # the pool has no unused song left
                break
            if min_fit is not None:
                # The line is judged on the candidates BEFORE artist spacing chooses
                # among them, so a stop whose nearest song fits is never dropped just
                # because spacing preferred a farther one (cold reader, round two).
                wpu = self._unit(wp)
                fits = {p: float(self.X[self.idx[p]] @ wpu) for p in cands}
                ok = [p for p in cands if fits[p] >= min_fit]
                if not ok:
                    skipped += 1
                    best = cands[0]
                    if strongest_left is None or fits[best] > strongest_left[1]:
                        strongest_left = (self.idx[best], fits[best])
                    continue                       # this stop has no song near enough
                cands = ok
            pick = None
            for p in cands:
                pa = self.artist[self.idx[p]]
                if not pa or pa not in recent[-artist_spacing:]:
                    pick = p
                    break
            if pick is None:
                pick = cands[0]
            if min_fit is not None:
                fit_of[self.idx[pick]] = fits[pick]
            stops.append((self._unit(wp), len(middle)))
            middle.append(pick)
            used.append(self.idx[pick])
            recent.append(self.artist[self.idx[pick]])
        if keep:
            # each kept song goes right after the stop it sounds closest to; with no stop
            # at all it goes before the destination
            after = {}
            for k in keep:
                if stops:
                    j = max(range(len(stops)), key=lambda s: float(self.X[k] @ stops[s][0]))
                    after.setdefault(stops[j][1], []).append(self.paths[k])
                else:
                    after.setdefault(-1, []).append(self.paths[k])
            woven = list(after.get(-1, []))
            for m, p in enumerate(middle):
                woven.append(p)
                woven.extend(after.get(m, []))
            middle = woven
        if report is not None:
            weakest = min(fit_of.items(), key=lambda kv: kv[1]) if fit_of else None
            report.update({
                "stopped": "fit" if skipped else ("exhausted" if ran_out else "size"),
                "pool": int(allowed.sum()) if allowed is not None else len(self.paths),
                "line": min_fit, "ceiling": 1.0,
                "skipped_stops": skipped,
                "ran_out": ran_out,
                "weakest_kept": weakest, "strongest_left": strongest_left,
                "fit": fit_of,
            })
        return [self.paths[ia]] + middle + [self.paths[ib]]

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
        if self._switched():
            # same ingredients, same order, same adds as _score_switched(), so the total
            # here is that score to the last bit
            comp["feel"] = 0.0
            total = 0.0
            for name, wk, g in self._terms(si):
                comp[name] = float(wk * self._fuse(g)[ci])
                total = total + comp[name]
            comp["total"] = total
            return comp

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
