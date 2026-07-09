"""Genre-family exploration: cluster tracks by CLAP embedding, see what genre tags
fall into each cluster.

Pure exploration tool (v1) — NOT wired into the mixer/hybrid engine. It answers one
question: do acoustic (CLAP) clusters line up with sensible "super-genre" families
well enough to bootstrap a genre-family feature later? Prior art: sklearn KMeans
(already a project dependency) — no hand-rolled clustering or keyword map.

Reads the `clap` table (see embed.py) and `tracks.genre` from the mixer DB, read-only.

  python genre_cluster.py --db ../../mixer-ng/data/mixer.db --k 12
"""
from __future__ import annotations
import argparse
import sqlite3
from collections import Counter

import numpy as np
from sklearn.cluster import KMeans


def _tags(g):
    """Split a raw (possibly multi-valued) genre field into individual lowercase tags.
    Same convention as hybrid.py's _tags(): comma/semicolon separated, so a track tagged
    "Classic Rock, Rock, Singer-Songwriter" contributes to all three tags."""
    if not g:
        return set()
    return {t.strip().lower() for part in g.split(";") for t in part.split(",") if t.strip()}


def load_clap_and_genre(db_path: str):
    """Read (paths, CLAP matrix [N,dim], raw genre strings) read-only from db_path.
    Only rows with a non-null CLAP vector are included."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            """SELECT t.path, t.genre, c.vec, c.dim
               FROM tracks t JOIN clap c ON c.path = t.path
               WHERE c.vec IS NOT NULL"""
        ).fetchall()
    finally:
        conn.close()

    paths, vecs, genres = [], [], []
    dim0 = None
    for path, genre, blob, dim in rows:
        v = np.frombuffer(blob, dtype=np.float32)
        if dim and v.shape[0] != dim:
            continue
        if dim0 is None:
            dim0 = v.shape[0]
        elif v.shape[0] != dim0:
            continue  # skip any row whose vector dim disagrees with the rest
        paths.append(path)
        vecs.append(v)
        genres.append(genre)
    mat = np.vstack(vecs) if vecs else np.zeros((0, 0), np.float32)
    return paths, mat, genres


def cluster_report(mat: np.ndarray, genres: list, k: int, top_n: int, seed: int):
    km = KMeans(n_clusters=k, random_state=seed, n_init=10)
    labels = km.fit_predict(mat)

    lines = []
    lines.append(f"n_tracks={mat.shape[0]}  dim={mat.shape[1]}  k={k}  "
                  f"inertia={km.inertia_:.1f}")
    lines.append("")

    for c in range(k):
        idx = np.where(labels == c)[0]
        size = len(idx)
        if size == 0:
            lines.append(f"--- cluster {c}: empty ---")
            continue

        raw_counts = Counter(genres[i] for i in idx if genres[i])
        tag_counts = Counter()
        for i in idx:
            tag_counts.update(_tags(genres[i]))

        n_tagged = sum(1 for i in idx if genres[i])
        top_tags = tag_counts.most_common(top_n)
        top_tag_frac = (top_tags[0][1] / size) if top_tags else 0.0

        lines.append(f"--- cluster {c}: {size} tracks ({n_tagged} with a genre tag) "
                      f"top-tag-share={top_tag_frac:.0%} ---")
        lines.append("  top genre tags:")
        for tag, cnt in top_tags:
            lines.append(f"    {cnt:5d}  ({cnt/size:5.1%})  {tag}")

        top_raw = raw_counts.most_common(top_n)
        lines.append("  top raw genre strings:")
        for raw, cnt in top_raw:
            lines.append(f"    {cnt:5d}  ({cnt/size:5.1%})  {raw!r}")
        lines.append("")

    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser(
        description="Cluster CLAP embeddings (KMeans) and report top genre tags per cluster "
                    "(exploration only; not wired into the mixer engine)."
    )
    ap.add_argument("--db", required=True, help="path to mixer.db (read-only)")
    ap.add_argument("--k", type=int, default=12, help="number of KMeans clusters")
    ap.add_argument("--top-n", type=int, default=8, help="top-N genre tags/strings to show per cluster")
    ap.add_argument("--seed", type=int, default=0, help="KMeans random_state")
    a = ap.parse_args()

    paths, mat, genres = load_clap_and_genre(a.db)
    if mat.shape[0] == 0:
        raise SystemExit(f"no rows with a CLAP vector found in {a.db}")
    if a.k > mat.shape[0]:
        raise SystemExit(f"k={a.k} > n_tracks={mat.shape[0]}")

    report = cluster_report(mat, genres, a.k, a.top_n, a.seed)
    print(report)


if __name__ == "__main__":
    main()
