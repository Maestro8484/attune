"""Sanity check for src/textsearch.py: run a fixed set of text prompts against the
CLAP-embedded library and report, per prompt, how many of the top-N results have a
genre that plausibly matches -- a machine-checkable count (genre-substring match),
not a taste/quality judgment. Loads the CLAP model once and reuses it across prompts
(model load is the expensive part; per-query search is cheap).

Read-only: only imports search()/load_model() from textsearch.py, which itself only
SELECTs from the DB.

  python textsearch_check.py --db ../data/mixer.db [--n 15]
"""
from __future__ import annotations
import os, sys, argparse

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
from textsearch import search, format_results, load_model

# query -> genre substrings (case-insensitive) that would count as a plausible hit.
# Deliberately loose/heuristic -- a coarse sanity signal, not ground truth labels.
DEFAULT_CHECKS = {
    "calm solo piano": ["piano", "classical", "instrumental", "ambient", "acoustic"],
    "aggressive metal": ["metal", "hardcore", "punk", "industrial"],
    "smooth late-night jazz": ["jazz", "soul", "r&b", "lounge", "blues"],
    "upbeat dance pop": ["dance", "pop", "electronic", "disco", "edm"],
}


def genre_hits(genre, keywords):
    g = (genre or "").lower()
    return any(k in g for k in keywords)


def main():
    ap = argparse.ArgumentParser(
        description="Sanity-check textsearch.py rankings against expected genre keywords")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(__file__), "..", "data", "mixer.db"))
    ap.add_argument("--n", type=int, default=15)
    a = ap.parse_args()

    session = load_model()
    total_hits = total_n = 0
    for q, keywords in DEFAULT_CHECKS.items():
        results, meta, session = search(a.db, q, a.n, session=session)
        print(format_results(q, results, meta))
        hits = 0
        for p, _ in results:
            _, _, genre = meta.get(p, (None, None, None))
            if genre_hits(genre, keywords):
                hits += 1
        total_hits += hits
        total_n += len(results)
        print(f"  -> {hits}/{len(results)} top results genre-match {keywords}\n")
    print(f"overall: {total_hits}/{total_n} top results across "
          f"{len(DEFAULT_CHECKS)} prompts genre-matched their heuristic keyword set")


if __name__ == "__main__":
    main()
