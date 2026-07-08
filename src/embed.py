"""Optional neural embeddings for the hybrid ("pro") engine.

Embeds each track with a music-specialized CLAP model into a `clap` table in the
same SQLite DB the scanner uses. This is what lifts Attune from "decent" to
"blind-tested competitive with MusicIP" — but it's optional: it needs PyTorch +
transformers, and benefits from a GPU. Without it, the standalone librosa engine
(mixer.py) still works.

Pipelined: several CPU decode workers feed the GPU in batches, so the card stays
fed instead of idle. Resumes from whatever is already embedded.

  pip install torch transformers        # (+ a CUDA build of torch for GPU)
  python embed.py --db ../data/mixer.db [--workers 6] [--batch 6]
"""
from __future__ import annotations
import os, sys, time, sqlite3, argparse, threading, queue
import numpy as np

MODEL = "laion/larger_clap_music"   # music-trained CLAP; general audio CLAP is weaker here
SR = 48000
WIN_S = 10.0
WIN_FRACS = (0.15, 0.5, 0.85)       # start / middle / end windows, mean-pooled


def _import_stack():
    try:
        import torch, librosa
        from transformers import ClapModel, ClapProcessor
        return torch, librosa, ClapModel, ClapProcessor
    except ImportError as e:
        sys.exit(f"neural embedding needs torch + transformers + librosa ({e}).\n"
                 "  pip install torch transformers librosa\n"
                 "For GPU: install a CUDA build of torch from pytorch.org.")


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


def _audio_embeds(out, torch):
    if torch.is_tensor(out):
        return out
    for attr in ("audio_embeds", "pooler_output"):
        v = getattr(out, attr, None)
        if v is not None:
            return v
    return out[0]


def embed(db_path, workers=6, batch=6, path_map=None):
    torch, librosa, ClapModel, ClapProcessor = _import_stack()
    dev = "cuda" if torch.cuda.is_available() else "cpu"
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
    print(f"{len(done)} already embedded, {len(todo)} to go, device={dev}", flush=True)
    if not todo:
        conn.close()
        return

    model = ClapModel.from_pretrained(MODEL).to(dev).eval()
    proc = ClapProcessor.from_pretrained(MODEL)
    print("model ready", flush=True)

    q = queue.Queue(maxsize=batch * 4)
    feed = queue.Queue()
    for p in todo:
        feed.put(p)

    def producer():
        # every claimed path MUST emit exactly one queue item, or the consumer
        # (which waits for len(todo) items) hangs. Wrap decode so a crash still emits.
        while True:
            try:
                p = feed.get_nowait()
            except queue.Empty:
                return
            try:
                clips, err = _decode(librosa, p, path_map)
            except Exception as e:
                clips, err = None, f"decode: {e.__class__.__name__}"
            q.put((p, clips, err))

    for _ in range(workers):
        threading.Thread(target=producer, daemon=True).start()

    t0 = time.time(); ok = err = got = 0
    pending = []

    def _embed_items(items):
        """Embed [(path, clips), ...]; return (n_ok, n_err). On a batch-level failure
        with >1 item, binary-split and retry so ONE bad/transient item can't poison the
        whole batch (the old bug: a single GPU hiccup marked up to `batch` tracks as
        permanent err, lost forever on resume). A lone item that still fails is recorded
        as err. Shape/finite are asserted before anything is stored."""
        if not items:
            return 0, 0
        allclips = [c for _, cs in items for c in cs]
        try:
            inp = proc(audio=allclips, sampling_rate=SR, return_tensors="pt")
            inp = {k: v.to(dev) for k, v in inp.items()}
            with torch.no_grad():
                E = _audio_embeds(model.get_audio_features(**inp), torch).detach().cpu().numpy()
            if E.shape[0] != 3 * len(items) or not np.isfinite(E).all():
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
        n_ok = 0
        for bi, (path, _) in enumerate(items):
            v = E[bi * 3:(bi + 1) * 3].mean(axis=0)
            nrm = np.linalg.norm(v)
            if nrm > 1e-9:
                v = v / nrm
            conn.execute("INSERT OR REPLACE INTO clap VALUES(?,?,?,?)",
                         (path, int(v.shape[0]), v.astype(np.float32).tobytes(), None))
            n_ok += 1
        return n_ok, 0

    def flush():
        nonlocal ok, err
        if not pending:
            return
        o, e = _embed_items(list(pending))
        ok += o; err += e
        pending.clear()

    while got < len(todo):
        path, clips, e = q.get()
        got += 1
        if clips is None:
            conn.execute("INSERT OR REPLACE INTO clap VALUES(?,?,?,?)", (path, None, None, e))
            err += 1
        else:
            pending.append((path, clips))
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
    ap = argparse.ArgumentParser(description="Attune neural embedder: CLAP vectors -> clap table")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "mixer.db"))
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--batch", type=int, default=6)
    a = ap.parse_args()
    embed(a.db, a.workers, a.batch)


if __name__ == "__main__":
    main()
