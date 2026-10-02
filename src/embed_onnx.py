"""Torch-free neural embeddings — ONNX twin of embed.py (ROADMAP_STANDALONE Phase A).

Same protocol as embed.py (laion/larger_clap_music with its makers' trained weights, see
CLAP_WEIGHTS below; 48 kHz, three 10 s windows at 0.15/0.5/0.85, mean-pooled, L2-normalized,
written to the same `clap` table), but:
  decode  -> librosa (as before)
  log-mel -> numpy port of transformers' ClapFeatureExtractor (bit-exact, see
             models/clap_norm.json for constants + provenance)
  network -> models/clap_music.onnx via onnxruntime (CPU EP; the graph contains
             tower + projection + per-clip L2 + mean-over-3-windows + final L2,
             so its output IS the finished track vector)

Deps: numpy + librosa + onnxruntime. torch/transformers must NEVER be imported
here — embed.py stays as the reference/training path.

  python embed_onnx.py --db ../data/mixer.db [--workers 6] [--batch 8]
"""
from __future__ import annotations
import os, sys, time, json, sqlite3, argparse, threading, queue
try:
    import numpy as np
except ImportError:                                    # a bare Python, before pip install
    sys.exit("this needs numpy.\n  pip install -r requirements.txt")
import features                 # sibling module, for its second decoder — see _decode()

MODEL = "laion/larger_clap_music"   # the architecture; the weights live in the .onnx
SR = 48000
WIN_S = 10.0
WIN_FRACS = (0.15, 0.5, 0.85)       # start / middle / end windows, mean-pooled

MODELS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "models")
ONNX_PATH = os.path.join(MODELS_DIR, "clap_music.onnx")
NORM_PATH = os.path.join(MODELS_DIR, "clap_norm.json")

# -- mel front-end constants (must match models/clap_norm.json, checked at start) --
N_FFT = 1024
HOP = 480
N_MELS = 64
FMIN = 50.0
FMAX = 14000.0
MEL_FLOOR = 1e-10
N_SAMPLES = int(WIN_S * SR)                                    # 480000
N_FRAMES = 1 + (N_SAMPLES + 2 * (N_FFT // 2) - N_FFT) // HOP   # 1001


# Which weights the model file this build expects carries: the checkpoint the model's makers
# trained (lukewys/laion_clap, music_audioset_epoch_15_esc_90.14.pt, CC0-1.0), exported to
# models/clap_music.onnx. The Hugging Face release of the same model, which every build before
# 2026-10-02 carried, holds weights that were never trained; its fingerprints are unrelated
# numbers. A library records which weights made its fingerprints in its meta table
# (clap_weights; absent = the release), and new songs are never fingerprinted into a library
# made with the other kind.
CLAP_WEIGHTS = "music_audioset_epoch_15_esc_90.14"
REFINGERPRINT_HINT = "python tools/refingerprint.py --db <library>"


def model_weights(sess):
    """Which weights a loaded model file carries, read from the file itself: the export stamps
    them in as metadata (clap_weights). A file without the stamp is the old release."""
    try:
        return sess.get_modelmeta().custom_metadata_map.get("clap_weights") or None
    except Exception:
        return None


def _library_weights(conn):
    """(what the library says made its fingerprints, whether it holds any fingerprint)."""
    try:
        row = conn.execute("SELECT value FROM meta WHERE key='clap_weights'").fetchone()
    except sqlite3.OperationalError:
        row = None
    try:
        has = conn.execute("SELECT 1 FROM clap WHERE vec IS NOT NULL LIMIT 1").fetchone() is not None
    except sqlite3.OperationalError:
        has = False
    return (row[0] if row else None), has


def _refuse_to_mix(conn, n_todo, weights=CLAP_WEIGHTS):
    """Stop, with the reason, before writing fingerprints of one kind into a library of the other.
    A library that holds no fingerprint and does not say is a new one: it goes ahead, and
    _mark_library() records the kind beside its first fingerprint."""
    theirs, has = _library_weights(conn)
    if not n_todo or theirs == weights or (theirs is None and not has):
        return
    conn.close()
    raise SystemExit(
        f"{n_todo} new song(s) were NOT fingerprinted. This library's fingerprints were made with "
        f"other weights of the fingerprint model ({theirs or 'the original release'}) than this "
        f"build carries ({weights or 'the original release'}). The two kinds are unrelated, and "
        f"a mix would treat them as one. The new songs stay out of mixes until the model file "
        f"matches the library. To move a library made with the original release over to this "
        f"build's fingerprints, every song has to be fingerprinted again:  {REFINGERPRINT_HINT}")


def _mark_library(conn, weights=CLAP_WEIGHTS):
    """Record which weights made this library's fingerprints, beside the first one written, and
    stop if the library has come to say another kind since this run started (a second run of
    the other kind, going at the same time). Called before every batch of fingerprints is
    written; not committed here, so the mark lands in the same commit as the fingerprints."""
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    row = conn.execute("SELECT value FROM meta WHERE key='clap_weights'").fetchone()
    if row is None:
        if weights is not None:
            conn.execute("INSERT INTO meta(key, value) VALUES('clap_weights', ?)", (weights,))
    elif row[0] != weights:
        conn.rollback()
        raise SystemExit(
            f"Stopped: while this run was going, the library came to say its fingerprints are made "
            f"with {row[0]}, and this run makes {weights or 'the original release'} ones. Nothing "
            f"more was written.")


def _import_stack():
    try:
        import librosa, onnxruntime
        return librosa, onnxruntime
    except ImportError as e:
        sys.exit(f"onnx embedding needs librosa + onnxruntime ({e}).\n"
                 "  pip install librosa onnxruntime")


_LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
_FETCH_HINT = "from the repository root, run:  python tools/fetch_model.py"


def _check_model_present(onnx_path=ONNX_PATH):
    """The model is not in the repository: it is 267 MiB and lives on a release instead.
    Say that in one sentence rather than letting onnxruntime raise a stack trace about a file
    it could not parse. A checkout made without git-lfs leaves a 134-byte text stub here, which
    looks present to `os.path.exists` and fails exactly the same way.

    This also fires inside the frozen analyzer if the machine that built it had a stub rather
    than the model, which is why the message names the file as well as the fix: in a packaged
    copy there is no tools/ directory to run, and the right answer is to reinstall."""
    if not os.path.exists(onnx_path):
        sys.exit(f"CLAP model not found: {onnx_path}\n"
                 f"  It is not kept in the repository. Fetch it:  {_FETCH_HINT}\n"
                 f"  (In an installed copy of Attune this file should have shipped inside the "
                 f"program folder; if it is missing there, reinstall.)")
    try:
        with open(onnx_path, "rb") as fh:
            head = fh.read(len(_LFS_POINTER_PREFIX))
    except OSError as e:
        sys.exit(f"CLAP model at {onnx_path} could not be read: {e}")
    if head == _LFS_POINTER_PREFIX:
        sys.exit(f"CLAP model at {onnx_path} is a Git LFS pointer stub, not the model.\n"
                 f"  That is what a clone made without git-lfs leaves behind. Fetch the real "
                 f"one:  {_FETCH_HINT}")
    if onnx_path == ONNX_PATH and os.path.getsize(onnx_path) < 1_000_000:
        # only the bundled model has a known size; a --onnx path is the caller's business
        sys.exit(f"CLAP model at {onnx_path} is {os.path.getsize(onnx_path)} bytes, far too "
                 f"small to be the model.\n  Fetch it again:  {_FETCH_HINT}")


# ---------------------------------------------------------------------------
# log-mel front-end: numpy port of transformers 5.13.0's exact numeric path for
# this checkpoint (ClapFeatureExtractor rand_trunc/not-longer branch). Ported
# from HuggingFace transformers audio_utils.py (Apache-2.0). Verified bit-exact
# against the live extractor (A1 gate) and end-to-end against 500 production
# rows (A3 parity gate) — see models/clap_norm.json. Do not "simplify" the
# float64/complex64 dtype choreography: it is part of the parity contract.
# ---------------------------------------------------------------------------

def _hertz_to_mel_slaney(freq):
    min_log_hertz, min_log_mel = 1000.0, 15.0
    logstep = 27.0 / np.log(6.4)
    mels = 3.0 * freq / 200.0
    if isinstance(freq, np.ndarray):
        log_region = freq >= min_log_hertz
        mels[log_region] = min_log_mel + np.log(freq[log_region] / min_log_hertz) * logstep
    elif freq >= min_log_hertz:
        mels = min_log_mel + np.log(freq / min_log_hertz) * logstep
    return mels


def _mel_to_hertz_slaney(mels):
    min_log_hertz, min_log_mel = 1000.0, 15.0
    logstep = np.log(6.4) / 27.0
    freq = 200.0 * mels / 3.0
    if isinstance(mels, np.ndarray):
        log_region = mels >= min_log_mel
        freq[log_region] = min_log_hertz * np.exp(logstep * (mels[log_region] - min_log_mel))
    elif mels >= min_log_mel:
        freq = min_log_hertz * np.exp(logstep * (mels - min_log_mel))
    return freq


def build_mel_filters() -> np.ndarray:
    """(513, 64) float64 slaney-scale slaney-norm filterbank (fmin 50, fmax 14000)."""
    num_frequency_bins = N_FFT // 2 + 1
    mel_freqs = np.linspace(_hertz_to_mel_slaney(FMIN), _hertz_to_mel_slaney(FMAX), N_MELS + 2)
    filter_freqs = _mel_to_hertz_slaney(mel_freqs)
    fft_freqs = np.linspace(0, SR // 2, num_frequency_bins)
    filter_diff = np.diff(filter_freqs)
    slopes = np.expand_dims(filter_freqs, 0) - np.expand_dims(fft_freqs, 1)
    down_slopes = -slopes[:, :-2] / filter_diff[:-1]
    up_slopes = slopes[:, 2:] / filter_diff[1:]
    mel_filters = np.maximum(np.zeros(1), np.minimum(down_slopes, up_slopes))
    enorm = 2.0 / (filter_freqs[2:N_MELS + 2] - filter_freqs[:N_MELS])
    mel_filters *= np.expand_dims(enorm, 0)
    return mel_filters


def build_window() -> np.ndarray:
    """(1024,) float64 periodic Hann."""
    return np.hanning(N_FFT + 1)[:-1]


def log_mel_clip(clip: np.ndarray, mel_filters: np.ndarray, window: np.ndarray) -> np.ndarray:
    """One 480000-sample float32 mono clip -> (1001, 64) float32 log-mel (dB)."""
    if clip.shape != (N_SAMPLES,):
        raise ValueError(f"expected ({N_SAMPLES},) clip, got {clip.shape}")
    w = clip.astype(np.float64)
    w = np.pad(w, (N_FFT // 2, N_FFT // 2), mode="reflect")
    frames = np.lib.stride_tricks.sliding_window_view(w, N_FFT)[::HOP][:N_FRAMES]
    frames = frames * window
    spec = np.fft.rfft(frames, axis=1).astype(np.complex64)   # deliberate precision match
    spec = np.abs(spec, dtype=np.float64) ** 2.0
    mel = np.maximum(MEL_FLOOR, np.dot(mel_filters.T, spec.T))
    mel = np.clip(mel, a_min=MEL_FLOOR, a_max=None)
    mel = 10.0 * np.log10(mel)
    return np.asarray(mel, np.float32).T                      # (1001, 64)


def _check_norm_json():
    """Refuse to run if models/clap_norm.json disagrees with this module's constants."""
    with open(NORM_PATH, encoding="utf-8") as fh:
        norm = json.load(fh)
    mel = norm["mel"]
    expect = {"n_fft": N_FFT, "hop_length": HOP, "n_mels": N_MELS, "fmin_hz": FMIN,
              "fmax_hz": FMAX, "frames": N_FRAMES}
    for k, v in expect.items():
        if mel.get(k) != v:
            sys.exit(f"clap_norm.json mismatch: {k}={mel.get(k)!r}, code expects {v!r}")
    proto = norm["protocol"]
    if proto.get("sample_rate") != SR or proto.get("window_fracs") != list(WIN_FRACS):
        sys.exit("clap_norm.json protocol mismatch with embed_onnx.py constants")
    return norm


# ---------------------------------------------------------------------------
# decode + windowing — byte-identical protocol to embed.py
# ---------------------------------------------------------------------------

def _decode(librosa, path, path_map):
    """Load audio (via optional local-mirror remap), return 3 fixed windows or (None, err)."""
    local = path
    for a, b in path_map:
        if path.startswith(a):
            cand = b + path[len(a):]
            if os.path.exists(cand):
                local = cand
            break
    try:
        # Not librosa.load directly: on the files librosa reads a fragment of, this asks
        # ffmpeg for the rest, so the vector describes the song and not its first three
        # seconds padded out with silence. Same decoder src/features.py already uses.
        y, _redecoded = features.load_audio(local, SR)
    except Exception as e:
        return None, f"load: {e.__class__.__name__}"
    if y is None or len(y) < SR * 2:
        return None, "too short"
    n = int(WIN_S * SR)
    clips = []
    for f in WIN_FRACS:
        c = int(f * len(y))
        s = max(0, min(c - n // 2, len(y) - n))
        clip = y[s:s + n]
        if len(clip) < n:
            clip = np.pad(clip, (0, n - len(clip)))
        clips.append(clip.astype(np.float32))
    return clips, None


def make_session(onnxruntime, onnx_path=ONNX_PATH):
    _check_model_present(onnx_path)
    return onnxruntime.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])


def _check_model_weights(sess, onnx_path=ONNX_PATH):
    """Which weights the loaded file carries. The bundled file must be the one this build
    expects: a checkout that still has the earlier model in place would otherwise write
    untrained fingerprints and label them trained. A --onnx path is the caller's business,
    and is taken for what the file says it is."""
    weights = model_weights(sess)
    if onnx_path == ONNX_PATH and weights != CLAP_WEIGHTS:
        sys.exit(f"CLAP model at {onnx_path} is not the one this build expects: it carries "
                 f"{weights or 'the original release, whose weights were never trained'}, and "
                 f"this build fingerprints with {CLAP_WEIGHTS}.\n"
                 f"  Fetch the current one:  {_FETCH_HINT}")
    return weights


def track_mels(clips, mel_filters, window) -> np.ndarray:
    """3 clips -> (3, 1, 1001, 64) float32, the graph's per-track input block."""
    return np.stack([log_mel_clip(c, mel_filters, window)[None, :] for c in clips])


def embed(db_path, workers=6, batch=8, path_map=None, onnx_path=ONNX_PATH):
    librosa, onnxruntime = _import_stack()
    _check_norm_json()
    _check_model_present(onnx_path)   # fail here, not after a full pass over the track table
    path_map = path_map or []
    workers = max(1, int(workers))   # 0 producers => consumer would block on q.get() forever
    batch = max(1, int(batch))

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("CREATE TABLE IF NOT EXISTS clap(path TEXT PRIMARY KEY, dim INT, vec BLOB, err TEXT)")
    conn.commit()

    done = {r[0] for r in conn.execute("SELECT path FROM clap WHERE vec IS NOT NULL OR err IS NOT NULL")}
    todo = [r[0] for r in conn.execute("SELECT path FROM tracks") if r[0] not in done]
    print(f"{len(done)} already embedded, {len(todo)} to go, device=cpu/onnx", flush=True)
    if not todo:
        conn.close()
        return
    if onnx_path == ONNX_PATH:
        _refuse_to_mix(conn, len(todo))   # the cheap refusal first, before the model loads

    sess = make_session(onnxruntime, onnx_path)
    weights = _check_model_weights(sess, onnx_path)
    _refuse_to_mix(conn, len(todo), weights)
    mel_filters, window = build_mel_filters(), build_window()
    print("model ready", flush=True)

    q = queue.Queue(maxsize=batch * 4)
    feed = queue.Queue()
    for p in todo:
        feed.put(p)

    def producer():
        # every claimed path MUST emit exactly one queue item, or the consumer
        # (which waits for len(todo) items) hangs. Wrap decode so a crash still emits.
        # The mel front-end runs here too: numpy releases the GIL, so it parallelizes.
        while True:
            try:
                p = feed.get_nowait()
            except queue.Empty:
                return
            try:
                clips, err = _decode(librosa, p, path_map)
                mels = track_mels(clips, mel_filters, window) if clips is not None else None
            except Exception as e:
                mels, err = None, f"decode: {e.__class__.__name__}"
            q.put((p, mels, err))

    for _ in range(workers):
        threading.Thread(target=producer, daemon=True).start()

    t0 = time.time(); ok = err = got = 0
    pending = []

    def _embed_items(items):
        """Embed [(path, mels), ...]; return (n_ok, n_err). Binary-split retry on a
        batch-level failure, exactly like embed.py, so one bad item can't poison
        the whole batch. The graph outputs the finished unit track vector."""
        if not items:
            return 0, 0
        try:
            X = np.stack([m for _, m in items])          # (B, 3, 1, 1001, 64)
            E = sess.run(None, {"mel": X})[0]            # (B, 512)
            if E.shape != (len(items), 512) or not np.isfinite(E).all():
                raise ValueError(f"bad embed output shape/finite {E.shape} for {len(items)} items")
        except Exception as e:
            if len(items) > 1:
                mid = len(items) // 2
                o1, e1 = _embed_items(items[:mid])
                o2, e2 = _embed_items(items[mid:])
                return o1 + o2, e1 + e2
            conn.execute("INSERT OR REPLACE INTO clap VALUES(?,?,?,?)",
                         (items[0][0], None, None, f"embed: {e.__class__.__name__}"))
            return 0, 1
        _mark_library(conn, weights)
        for bi, (path, _) in enumerate(items):
            v = E[bi]
            conn.execute("INSERT OR REPLACE INTO clap VALUES(?,?,?,?)",
                         (path, int(v.shape[0]), v.astype(np.float32).tobytes(), None))
        return len(items), 0

    def flush():
        nonlocal ok, err
        if not pending:
            return
        o, e = _embed_items(list(pending))
        ok += o; err += e
        pending.clear()

    while got < len(todo):
        path, mels, e = q.get()
        got += 1
        if mels is None:
            conn.execute("INSERT OR REPLACE INTO clap VALUES(?,?,?,?)", (path, None, None, e))
            err += 1
        else:
            pending.append((path, mels))
            if len(pending) >= batch:
                flush()
        if got % 100 == 0 or got == len(todo):
            flush(); conn.commit()
            r = got / (time.time() - t0)
            print(f"  {got}/{len(todo)} ok={ok} err={err} {r:.2f}/s "
                  f"eta={(len(todo)-got)/r/60:.0f}m", flush=True)
    flush(); conn.commit()
    conn.close()
    print(f"done: {ok} embedded, {err} errors, {(time.time()-t0)/60:.1f}m", flush=True)


def main():
    ap = argparse.ArgumentParser(description="Attune torch-free embedder: CLAP vectors -> clap table")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "mixer.db"))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--batch", type=int, default=8)
    ap.add_argument("--onnx", default=ONNX_PATH)
    a = ap.parse_args()
    embed(a.db, a.workers, a.batch, onnx_path=a.onnx)


if __name__ == "__main__":
    main()
