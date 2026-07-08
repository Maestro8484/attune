"""Locks in mixer.Engine.mix() bug fixes:
  * the pool-sampling loop always drops a rejected pick from `avail` before
    looping again, so it can't burn its whole `guard` budget re-drawing the
    same artist-blocked track forever (the old infinite-loop-adjacent bug);
  * `artist_spacing<=0` means "no restriction" and must not collapse output
    (the old `recent[-0:]` bug treated spacing=0 as "ban every artist that
    ever appeared", which starved the playlist down to near nothing).

Builds a tiny synthetic in-memory Engine (no DB, no filesystem) so this is a
pure, fast unit test of the ranking/sampling logic in attune/src/mixer.py.
"""
from __future__ import annotations
import time

import numpy as np
import pytest

from features import FEATURE_DIM
from mixer import Engine


def _make_engine(n=30, seed=42, artist_plan=None):
    """artist_plan: list of length n of artist names (or None -> auto mix of
    mostly-unique artists with a few repeats, matching the task's spec)."""
    rng = np.random.default_rng(seed)
    paths = [f"/lib/track_{i:02d}.mp3" for i in range(n)]
    matrix = rng.normal(size=(n, FEATURE_DIM))
    if artist_plan is None:
        n_repeat_groups = 3
        repeats = []
        for i in range(n_repeat_groups):
            repeats += [f"Repeat{i}"] * 2
        solos = [f"Solo{i}" for i in range(n - len(repeats))]
        artist_plan = solos + repeats
    assert len(artist_plan) == n
    meta = {
        p: {"artist": a, "album": "Al", "title": p, "genre": "", "year": 2000,
            "seconds": 200, "tempo": 120}
        for p, a in zip(paths, artist_plan)
    }
    return Engine(paths, matrix, meta), paths


def test_mix_returns_exactly_size_for_variety_0():
    eng, paths = _make_engine(n=30)
    out = eng.mix(paths[0], size=20, style=40, variety=0, artist_spacing=3,
                  rng=np.random.default_rng(0))
    assert out is not None
    assert len(out) == 20


def test_mix_returns_approximately_size_for_variety_5():
    eng, paths = _make_engine(n=30)
    out = eng.mix(paths[0], size=20, style=40, variety=5, artist_spacing=3,
                  rng=np.random.default_rng(0))
    assert out is not None
    # "approximately" size: allow a small shortfall from artist-spacing
    # pressure, but it must not be far off.
    assert 18 <= len(out) <= 20


@pytest.mark.parametrize("variety", [0, 5])
def test_artist_spacing_zero_does_not_collapse_output(variety):
    """Regression for the recent[-0:] bug: with artist_spacing=0 (no
    restriction) a library dominated by ONE repeated artist must still fill
    the playlist to `size`, not collapse to near-zero."""
    n = 30
    artist_plan = ["SameArtist"] * (n - 1) + ["Other"]
    eng, paths = _make_engine(n=n, artist_plan=artist_plan)
    out = eng.mix(paths[0], size=20, style=40, variety=variety, artist_spacing=0,
                  rng=np.random.default_rng(0))
    assert out is not None
    assert len(out) == 20


def test_artist_spacing_positive_still_restricts_with_heavy_repeats():
    """Sanity counterpart: with a POSITIVE artist_spacing, a library
    dominated by one artist, and the strict variety=0 walk (no "accept
    anyway when running low" escape valve), spacing should meaningfully
    restrict output. This proves artist_spacing=0 above is really "no
    restriction", not just a library that never needed restricting."""
    n = 30
    artist_plan = ["SameArtist"] * (n - 1) + ["Other"]
    eng, paths = _make_engine(n=n, artist_plan=artist_plan)
    out = eng.mix(paths[0], size=20, style=40, variety=0, artist_spacing=3,
                  rng=np.random.default_rng(0))
    assert out is not None
    assert len(out) < 20


def test_mix_no_infinite_loop_high_variety_high_spacing():
    """The pool-sampling `while` loop is bounded by `guard < size * 50`; with
    aggressive variety + spacing pressure it must still terminate quickly."""
    n = 30
    artist_plan = ["SameArtist"] * (n - 1) + ["Other"]
    eng, paths = _make_engine(n=n, artist_plan=artist_plan)
    t0 = time.time()
    out = eng.mix(paths[0], size=20, style=40, variety=9, artist_spacing=9,
                  rng=np.random.default_rng(0))
    elapsed = time.time() - t0
    assert out is not None
    assert len(out) <= 20
    assert elapsed < 5.0, "mix() took too long -- possible infinite/near-infinite loop"


def test_mix_seed_not_found_returns_none():
    eng, _ = _make_engine(n=10)
    assert eng.mix("/does/not/exist.mp3", size=5) is None
