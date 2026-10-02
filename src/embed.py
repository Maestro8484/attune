"""Optional neural embeddings for the hybrid ("pro") engine.

Embeds each track with a music-specialized CLAP model (its makers' trained weights, see
CLAP_WEIGHTS below) into a `clap` table in the same SQLite DB the scanner uses. This is what lifts Attune from "decent" to
"competitive with MusicIP in a partly blind listening test" — but it's optional: it needs PyTorch +
transformers, and benefits from a GPU. Without it, the standalone librosa engine
(mixer.py) still works.

Pipelined: several CPU decode workers feed the GPU in batches, so the card stays
fed instead of idle. Resumes from whatever is already embedded.

  pip install torch transformers        # (+ a CUDA build of torch for GPU)
  python embed.py --db ../data/mixer.db [--workers 6] [--batch 6]
"""
from __future__ import annotations
import os, re, sys, time, sqlite3, argparse, threading, queue
import numpy as np

MODEL = "laion/larger_clap_music"   # the architecture and the audio processor
SR = 48000
WIN_S = 10.0
WIN_FRACS = (0.15, 0.5, 0.85)       # start / middle / end windows, mean-pooled


# Which weights this embedder's model carries: the checkpoint the model's makers trained,
# CKPT_FILE in CKPT_REPO (CC0-1.0). The Hugging Face release named by MODEL holds the same
# architecture with weights that were never trained, so it supplies the layout and the audio
# processor only, and load_model() fills it from the checkpoint. A library records which weights
# made its fingerprints in its meta table (clap_weights; absent = the release). The two kinds
# of fingerprint are unrelated numbers, so new songs are never fingerprinted into a library
# made with the other kind.
CLAP_WEIGHTS = "music_audioset_epoch_15_esc_90.14"
CKPT_REPO, CKPT_FILE = "lukewys/laion_clap", CLAP_WEIGHTS + ".pt"
REFINGERPRINT_HINT = "python tools/refingerprint.py --db <library>"


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
    """Record which weights made this library's fingerprints, beside the first one written.
    Not committed here: it lands in the same commit as that fingerprint."""
    if weights is None:
        return
    conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
    conn.execute("INSERT OR IGNORE INTO meta(key, value) VALUES('clap_weights', ?)", (weights,))


# The makers' checkpoint names its tensors the way their own code does. This is the renaming the
# transformers project uses to load such a checkpoint into its ClapModel: ported from
# convert_clap_original_pytorch_to_hf.py in HuggingFace transformers (Apache-2.0).
_CKPT_KEYS = {"text_branch": "text_model", "audio_branch": "audio_model.audio_encoder",
              "attn": "attention.self", "self.proj": "output.dense",
              "attention.self_mask": "attn_mask", "mlp.fc1": "intermediate.dense",
              "mlp.fc2": "output.dense", "norm1": "layernorm_before", "norm2": "layernorm_after",
              "bn0": "batch_norm"}


def _rename_checkpoint(state):
    out = {}
    for key, value in state.items():
        for a, b in _CKPT_KEYS.items():
            if a in key:
                key = key.replace(a, b)
        m = re.match(r".*sequential.(\d+).*", key)
        m2 = re.match(r".*_projection.(\d+).*", key)
        if m:
            key = key.replace(f"sequential.{m.group(1)}.", f"layers.{int(m.group(1)) // 3}.linear.")
        elif m2:
            n = int(m2.group(1))
            key = key.replace(f"_projection.{n}.", f"_projection.linear{1 if n == 0 else 2}.")
        if "qkv" in key:
            d = value.size(0) // 3
            out[key.replace("qkv", "query")] = value[:d]
            out[key.replace("qkv", "key")] = value[d:2 * d]
            out[key.replace("qkv", "value")] = value[2 * d:]
        else:
            out[key] = value
    return out


def load_model(torch, ClapModel, ClapProcessor):
    """(model, processor): MODEL's architecture filled with the makers' trained weights.
    The checkpoint (2.2 GiB) is downloaded once into the Hugging Face cache. It is read with
    torch's safe loader, with only the plain numpy value types its bookkeeping needs let through."""
    import numpy
    from huggingface_hub import hf_hub_download
    ck = hf_hub_download(CKPT_REPO, CKPT_FILE)
    allow = [numpy.dtype, numpy.ndarray,
             (numpy._core.multiarray.scalar, "numpy.core.multiarray.scalar"),
             (numpy._core.multiarray._reconstruct, "numpy.core.multiarray._reconstruct")]
    allow += [type(numpy.dtype(t)) for t in ("float64", "float32", "int64", "int32")]
    with torch.serialization.safe_globals(allow):
        raw = torch.load(ck, map_location="cpu", weights_only=True)
    sd = raw.get("state_dict", raw)
    sd = _rename_checkpoint({(k[7:] if k.startswith("module.") else k): v for k, v in sd.items()})
    model = ClapModel(ClapModel.from_pretrained(MODEL).config).eval()
    res = model.load_state_dict(sd, strict=False)
    # the one tensor the checkpoint does not supply is a constant buffer of zeros
    if res.missing_keys not in ([], ["text_model.embeddings.token_type_ids"]):
        raise SystemExit(f"the checkpoint {CKPT_FILE} did not fill the model: missing {res.missing_keys[:5]}")
    return model, ClapProcessor.from_pretrained(MODEL)


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
    _refuse_to_mix(conn, len(todo))
    if not todo:
        conn.close()
        return

    model, proc = load_model(torch, ClapModel, ClapProcessor)
    model = model.to(dev).eval()
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
        _mark_library(conn)
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
