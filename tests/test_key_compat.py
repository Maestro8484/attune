"""Locks in the key-compatibility scoring used by the hybrid engine (and, where
importable, the mixer-ng coherence harness's independent re-implementation).

hybrid._key_compat lives in attune/src/hybrid.py and is dependency-light (numpy
only) so it is imported directly and exercised thoroughly.

coherence.key_compat lives in mixer-ng/src/coherence.py (a sibling project, not
part of attune/src) and imports heavy DB state at module load time (it opens
mixer-ng/data/mixer.db and clap.db and computes artist centroids). We load it
by file path via importlib so we never add mixer-ng/src to sys.path (that
would risk shadowing attune/src's identically-named db.py/features.py/mixer.py
modules for OTHER tests). If that module-load ever fails or hangs in some
other environment, the coherence tests are skipped with a clear reason instead
of failing the whole suite -- per the task's own escape hatch.
"""
from __future__ import annotations
import importlib.util
import math
import os

import pytest

import hybrid


# ---------------------------------------------------------------------------
# hybrid._key_compat  (dependency-light; always run)
# ---------------------------------------------------------------------------

MAJOR_MINOR_RELATIVE_PAIRS = [
    # (minor_tonic, major_tonic) such that major = minor + 3 (mod 12)
    (m, (m + 3) % 12) for m in range(12)
]


@pytest.mark.parametrize("minor_tonic,major_tonic", MAJOR_MINOR_RELATIVE_PAIRS)
def test_hybrid_relative_major_minor_pairs(minor_tonic, major_tonic):
    """All 12 relative major/minor pairs score 0.8, in both call orders."""
    minor_key = (minor_tonic, False)
    major_key = (major_tonic, True)
    assert hybrid._key_compat(minor_key, major_key) == pytest.approx(0.8)
    assert hybrid._key_compat(major_key, minor_key) == pytest.approx(0.8)


@pytest.mark.parametrize("tonic", range(12))
@pytest.mark.parametrize("is_major", [True, False])
def test_hybrid_same_key_is_1(tonic, is_major):
    k = (tonic, is_major)
    assert hybrid._key_compat(k, k) == pytest.approx(1.0)


@pytest.mark.parametrize("is_major", [True, False])
@pytest.mark.parametrize("step", [7, 5])
def test_hybrid_fifth_neighbor_is_0_8(is_major, step):
    """A fifth away (+7 or -7, i.e. step 5) in the SAME mode scores 0.8."""
    k1 = (0, is_major)
    k2 = (step, is_major)
    assert hybrid._key_compat(k1, k2) == pytest.approx(0.8)
    assert hybrid._key_compat(k2, k1) == pytest.approx(0.8)


@pytest.mark.parametrize("is_major", [True, False])
@pytest.mark.parametrize("step", [2, 10])
def test_hybrid_two_fifths_is_0_4(is_major, step):
    """Two fifths away (+2 or -2, i.e. step 10) in the SAME mode scores 0.4."""
    k1 = (0, is_major)
    k2 = (step, is_major)
    assert hybrid._key_compat(k1, k2) == pytest.approx(0.4)
    assert hybrid._key_compat(k2, k1) == pytest.approx(0.4)


@pytest.mark.parametrize("is_major", [True, False])
@pytest.mark.parametrize("step", [1, 3, 4, 6, 8, 9, 11])
def test_hybrid_unrelated_same_mode_is_0(is_major, step):
    k1 = (0, is_major)
    k2 = (step, is_major)
    assert hybrid._key_compat(k1, k2) == 0.0


@pytest.mark.parametrize("major_tonic", [t for t in range(12)])
def test_hybrid_unrelated_cross_mode_is_0(major_tonic):
    """A major/minor pair that is NOT the relative pair scores 0."""
    minor_tonic = 0
    if major_tonic == (minor_tonic + 3) % 12:
        pytest.skip("this is the relative pair, covered elsewhere")
    assert hybrid._key_compat((minor_tonic, False), (major_tonic, True)) == 0.0


@pytest.mark.parametrize("k1,k2", [
    (None, None),
    (None, (0, True)),
    ((0, True), None),
    (None, (5, False)),
])
def test_hybrid_key_compat_none_safe(k1, k2):
    """None inputs must not raise; current behavior is a neutral 0.5."""
    assert hybrid._key_compat(k1, k2) == 0.5


# ---------------------------------------------------------------------------
# coherence.key_compat  (mixer-ng/src/coherence.py) -- best-effort
# ---------------------------------------------------------------------------

def _load_coherence_module():
    """Load mixer-ng/src/coherence.py by file path (not by adding its dir to
    sys.path) so its `import db` / `from features import ...` statements
    resolve against attune/src's byte-identical copies already on sys.path,
    instead of risking a second, differently-ordered copy of those modules."""
    coherence_path = os.path.normpath(os.path.join(
        os.path.dirname(os.path.abspath(__file__)),
        "..", "..", "mixer-ng", "src", "coherence.py"))
    if not os.path.exists(coherence_path):
        raise RuntimeError(f"coherence.py not found at {coherence_path}")
    spec = importlib.util.spec_from_file_location("coherence_under_test", coherence_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def coherence_mod():
    try:
        return _load_coherence_module()
    except Exception as e:  # pragma: no cover - environment-dependent escape hatch
        pytest.skip(
            "mixer-ng/src/coherence.py could not be imported (it loads heavy DB "
            f"state -- mixer-ng/data/mixer.db and clap.db -- at module load): "
            f"{type(e).__name__}: {e}"
        )


@pytest.mark.parametrize("minor_tonic,major_tonic", MAJOR_MINOR_RELATIVE_PAIRS)
def test_coherence_relative_major_minor_pairs(coherence_mod, minor_tonic, major_tonic):
    minor_key = (minor_tonic, False)
    major_key = (major_tonic, True)
    assert coherence_mod.key_compat(minor_key, major_key) == pytest.approx(0.8)
    assert coherence_mod.key_compat(major_key, minor_key) == pytest.approx(0.8)


@pytest.mark.parametrize("is_major", [True, False])
def test_coherence_same_key_is_1(coherence_mod, is_major):
    k = (3, is_major)
    assert coherence_mod.key_compat(k, k) == pytest.approx(1.0)


@pytest.mark.parametrize("is_major", [True, False])
@pytest.mark.parametrize("step", [7, 5])
def test_coherence_fifth_neighbor_is_0_8(coherence_mod, is_major, step):
    k1 = (0, is_major)
    k2 = (step, is_major)
    assert coherence_mod.key_compat(k1, k2) == pytest.approx(0.8)


@pytest.mark.parametrize("is_major", [True, False])
@pytest.mark.parametrize("step", [2, 10])
def test_coherence_two_fifths_is_0_4(coherence_mod, is_major, step):
    k1 = (0, is_major)
    k2 = (step, is_major)
    assert coherence_mod.key_compat(k1, k2) == pytest.approx(0.4)


@pytest.mark.parametrize("is_major", [True, False])
@pytest.mark.parametrize("step", [1, 3, 4, 6, 8, 9, 11])
def test_coherence_unrelated_is_0(coherence_mod, is_major, step):
    k1 = (0, is_major)
    k2 = (step, is_major)
    assert coherence_mod.key_compat(k1, k2) == 0.0


@pytest.mark.parametrize("k1,k2", [
    (None, None),
    (None, (0, True)),
    ((0, True), None),
])
def test_coherence_key_compat_none_safe(coherence_mod, k1, k2):
    """None-safe: must not raise. coherence.key_compat's documented/actual
    behavior for missing key info is NaN (unlike hybrid's neutral 0.5)."""
    result = coherence_mod.key_compat(k1, k2)
    assert math.isnan(result)
