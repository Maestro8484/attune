"""The scoring versions the blind listening test and the influence report compare.

One table, read by eval/abtest.py (which exports each version as a blinded playlist) and
eval/influence.py (which reports how much say each ingredient has in each version). A
version is the shipped engine with some of hybrid.py's scoring switches turned on; every
switch is off in the shipped default, and none becomes the default on a number (LAW 1).

Two numbers here are PROVISIONAL starting points, not tuned values. Whether either is
right is a listening decision.

FUSED_CLAP_WEIGHT  Once every ingredient is ranked onto one scale, a weight is a share of
    say, and the shipped fingerprint weight of 1.0 (written for a raw scale on which the
    fingerprint barely counted) would hand it most of the sorting: 70 songs of 100 change
    when it is removed. The target for this work is 30 to 60. Measured with
    eval/influence.py on the reference library 2026-10-01 (pool 21,215): 0.3 gives 49,
    the middle of that band, and level with the descriptor, genre and tempo ingredients.
    MOVED TO 0.5 the same day for the corrected fingerprints (the makers' trained weights,
    ISSUES.md row 81): there 0.3 gives 29, just under the band, 0.4 gives 33 at its edge
    and 0.5 gives 39 (pool 21,098, audit-sonic/influence_sweep_fixed.txt). Moved only to
    stay inside the band, not tuned; on the old fingerprints 0.5 would give about 60.
EARFEEL_WEIGHT  The same weight the sound-descriptor term carries, so the two "what it
    sounds like" ingredients beside the fingerprint start level.
FAMILY_CREDIT  The two-tier genre term's credit for a family-only match (Punk against
    Grunge, both Rock): half of what the same value gets. Half is the plain midpoint
    between "a different genre" and "the same genre", not a tuned value.
"""
from __future__ import annotations

import copy

FUSED_CLAP_WEIGHT = 0.5
EARFEEL_WEIGHT = 0.4
# Provisional, like the two above: the genre ingredient's own weight, so listener-based
# artist closeness starts level with tag-based genre closeness.
ARTIST_WEIGHT = 0.3
FAMILY_CREDIT = 0.5

_FUSED = {"fusion": "rank", "clap_space": "centered"}

VARIANTS = {
    # A: the shipped default, exactly
    "v2":        {},
    # B: every ingredient ranked before its weight applies, fingerprint with the
    #    library's average removed
    "v2-fused":  {**_FUSED, "weights": {"clap": FUSED_CLAP_WEIGHT}},
    # C: B plus closeness in the five Earfeel scores
    "v2-earfeel":   {**_FUSED, "weights": {"clap": FUSED_CLAP_WEIGHT, "earfeel": EARFEEL_WEIGHT}},
    # D: the shipped default with the fingerprint given no say at all
    "v2-noclap": {"weights": {"clap": 0.0}},
    # C with the Earfeel scores read from a file named on the command line (--earfeel-file)
    # instead of the shipped one; for trying a different set of scores without shipping it
    "v2-earfeel-file": {**_FUSED, "weights": {"clap": FUSED_CLAP_WEIGHT, "earfeel": EARFEEL_WEIGHT},
                     "earfeel_file": True},
    # The catalog test (2026-10-01). "v2" reads the catalog tables when a library has them;
    # this is the engine as it was before them: no catalog, "covers" counted as a genre.
    "v2-before-catalog": {"catalog": False},
    # v2 plus how often listeners play the two artists together
    "v2-catalog-artist": {"weights": {"artist": ARTIST_WEIGHT}},
    # The follow-up to the sitting of 2026-10-01: each of its two finalists with the genre
    # term in two tiers (needs a library with the genre_family table; without one these
    # score exactly as v2 and v2-earfeel-file).
    "v2-tiers": {"family_credit": FAMILY_CREDIT},
    "v2-earfeel-file-tiers": {**_FUSED, "weights": {"clap": FUSED_CLAP_WEIGHT, "earfeel": EARFEEL_WEIGHT},
                           "earfeel_file": True, "family_credit": FAMILY_CREDIT},
}


_OVERRIDES = frozenset({"fusion", "clap_space", "weights", "family_credit", "catalog"})


def variant_engine(base, name, earfeel_file=None, **override):
    """A HybridEngine that scores as the named version, sharing `base`'s loaded library.
    `base` is not changed. `override` replaces or adds settings (fusion=, clap_space=,
    weights={...}) on top of the named version, for the influence report's grid."""
    unknown = set(override) - _OVERRIDES
    if unknown:
        # a misspelt or renamed setting must fail loudly: ignored, the report would carry
        # the default's numbers under the other version's name
        raise TypeError(f"variant_engine() does not know {sorted(unknown)}; it takes {sorted(_OVERRIDES)}")
    spec = dict(VARIANTS[name])
    weights = dict(spec.get("weights", {}))
    weights.update(override.pop("weights", {}))
    spec.update(override)
    e = base.without_catalog() if spec.get("catalog") is False else copy.copy(base)
    e.w = dict(base.w)
    e.w.update(weights)
    e.fusion = spec.get("fusion", "raw")
    e.clap_space = spec.get("clap_space", "raw")
    e.family_credit = float(spec.get("family_credit", 0.0))
    if spec.get("earfeel_file"):
        if not earfeel_file:
            raise SystemExit(f"version '{name}' needs --earfeel-file")
        e.earfeel, e.earfeel_names, e.earfeel_labels = None, [], []
        e._load_earfeel(earfeel_file)
        if e.earfeel is None:
            raise SystemExit(f"no Earfeel scores could be read from {earfeel_file}")
    return e
