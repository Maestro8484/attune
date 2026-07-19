"""Torch-free neural embeddings — ONNX twin of embed.py (ROADMAP_STANDALONE Phase A).

Same protocol as embed.py (laion/larger_clap_music, 48 kHz, three 10 s windows at
0.15/0.5/0.85, mean-pooled, L2-normalized, written to the same `clap` table), but:
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
import numpy as np

MODEL = "laion/larger_clap_music"   # provenance; the weights live in the .onnx
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


def _import_stack():
    try:
        import librosa, onnxruntime
        return librosa, onnxruntime
    except ImportError as e:
        sys.exit(f"onnx embedding needs librosa + onnxruntime ({e}).\n"
                 "  pip install librosa onnxruntime")


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
        y, _ = librosa.load(local, sr=SR, mono=True)
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
    return onnxruntime.InferenceSession(onnx_path, providers=["CPUExecutionProvider"])


def track_mels(clips, mel_filters, window) -> np.ndarray:
    """3 clips -> (3, 1, 1001, 64) float32, the graph's per-track input block."""
    return np.stack([log_mel_clip(c, mel_filters, window)[None, :] for c in clips])


def embed(db_path, workers=6, batch=8, path_map=None, onnx_path=ONNX_PATH):
    librosa, onnxruntime = _import_stack()
    _check_norm_json()
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

    sess = make_session(onnxruntime, onnx_path)
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
