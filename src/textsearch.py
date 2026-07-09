"""CLAP text -> track search: rank library tracks by similarity to a text prompt.

Loads the SAME music-CLAP model embed.py uses to build the `clap` table
(laion/larger_clap_music), encodes a text prompt with get_text_features, L2-normalizes
it exactly like embed.py normalizes audio vectors, and ranks every track by dot product
(cosine, since both sides are unit vectors) against its stored CLAP embedding.

Read-only: only ever SELECTs from the DB (opened with sqlite mode=ro); does not touch
hybrid.py or the clap/tracks tables.

  python textsearch.py --db ../data/mixer.db --q "aggressive gym metal" --n 15
"""
from __future__ import annotations
import os, sys, argparse, sqlite3
import numpy as np

MODEL = "laion/larger_clap_music"   # must match embed.py so text/audio share one embedding space


def _import_stack():
    try:
        import torch
        from transformers import ClapModel, ClapProcessor
        return torch, ClapModel, ClapProcessor
    except ImportError as e:
        sys.exit(f"text search needs torch + transformers ({e}).\n"
                  "  pip install torch transformers")


def load_model(dev=None):
    torch, ClapModel, ClapProcessor = _import_stack()
    dev = dev or ("cuda" if torch.cuda.is_available() else "cpu")
    model = ClapModel.from_pretrained(MODEL).to(dev).eval()
    proc = ClapProcessor.from_pretrained(MODEL)
    return torch, model, proc, dev


def embed_text(query, torch, model, proc, dev):
    """Text -> L2-normalized float32 512-d vector, same normalization embed.py applies
    to audio vectors, so a plain dot product against the clap table is a cosine score.

    Deliberately does NOT call model.get_text_features()/pooler_output. Verified against
    this checkpoint (laion/larger_clap_music): ClapTextModel.pooler.dense.bias loads as
    exactly all-zero (never present in the released weights), so its tanh pooler collapses
    every input to nearly the same direction -- pairwise cosine between totally unrelated
    prompts (e.g. "calm solo piano" vs "aggressive metal") came out ~0.999, and search
    results were ~identical regardless of query. Attention-masked mean-pooling of
    last_hidden_state before the (checkpoint-trained) text_projection layer avoids the
    broken pooler and spreads unrelated prompts to ~0.90 cosine -- the ranking actually
    responds to the query. text_projection itself is unaffected (it's a plain linear
    layer, still loaded from the checkpoint)."""
    inp = proc(text=[query], return_tensors="pt", padding=True)
    inp = {k: v.to(dev) for k, v in inp.items()}
    with torch.no_grad():
        text_out = model.text_model(input_ids=inp["input_ids"], attention_mask=inp.get("attention_mask"))
        mask = inp["attention_mask"].unsqueeze(-1).float()
        mean_pooled = (text_out.last_hidden_state * mask).sum(1) / mask.sum(1).clamp(min=1e-9)
        proj = model.text_projection(mean_pooled)
    v = proj[0].detach().cpu().numpy().astype(np.float32)
    nrm = np.linalg.norm(v)
    if nrm > 1e-9:
        v = v / nrm
    return v


def load_clap(db_path):
    """path -> stored (already L2-normalized) CLAP vector, read-only."""
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    clap = {}
    for p, dim, blob in conn.execute("SELECT path,dim,vec FROM clap WHERE vec IS NOT NULL"):
        v = np.frombuffer(blob, np.float32)
        if v.shape[0] == dim:
            clap[p] = v
    conn.close()
    return clap


def load_track_meta(db_path, paths):
    """path -> (artist, title, genre), read-only, only for the given paths."""
    uri = f"file:{os.path.abspath(db_path)}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=30)
    meta = {}
    placeholders = ",".join("?" * len(paths))
    if paths:
        rows = conn.execute(
            f"SELECT path, artist, title, genre FROM tracks WHERE path IN ({placeholders})",
            paths,
        ).fetchall()
        for p, artist, title, genre in rows:
            meta[p] = (artist, title, genre)
    conn.close()
    return meta


def rank(clap_paths, X, query_vec, n=15):
    """X: (N,512) L2-normalized CLAP matrix aligned with clap_paths. Returns top-n
    (path, score) sorted by descending dot product."""
    scores = X @ query_vec
    n = min(n, len(clap_paths))
    order = np.argpartition(-scores, n - 1)[:n]
    order = order[np.argsort(-scores[order])]
    return [(clap_paths[i], float(scores[i])) for i in order]


def search(db_path, query, n=15, session=None):
    """One-shot search. `session` is an optional (torch, model, proc, dev) tuple from
    load_model(), so callers doing multiple queries (e.g. the eval script) load the
    model once and reuse it instead of paying model-load cost per prompt."""
    clap = load_clap(db_path)
    if not clap:
        sys.exit("no CLAP embeddings found in DB — run embed.py first")
    paths = list(clap)
    X = np.vstack([clap[p] for p in paths])

    torch, model, proc, dev = session or load_model()
    qv = embed_text(query, torch, model, proc, dev)

    results = rank(paths, X, qv, n)
    meta = load_track_meta(db_path, [p for p, _ in results])
    return results, meta, (torch, model, proc, dev)


def format_results(query, results, meta):
    lines = [f'query: "{query}"']
    for rank_i, (p, score) in enumerate(results, 1):
        artist, title, genre = meta.get(p, (None, None, None))
        name = f"{artist} - {title}" if (artist or title) else os.path.basename(p)
        lines.append(f"{rank_i:2d}. {score:.4f}  [{genre or '?'}]  {name}")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(description="CLAP text-to-track search")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "mixer.db"))
    ap.add_argument("--q", required=True, help='text prompt, e.g. "aggressive gym metal"')
    ap.add_argument("--n", type=int, default=15)
    a = ap.parse_args()

    results, meta, _ = search(a.db, a.q, a.n)
    print(format_results(a.q, results, meta))


if __name__ == "__main__":
    main()
