"""The genre term in two tiers (2026-10-01): full credit for the same value, partial credit for the
same broad family, nothing for a different family. A scoring switch, off by default.

Locks in:
  1. off by default: a library that HAS the family table scores exactly as one that has not;
  2. no table, no change: the switch does nothing on a library without the family table;
  3. on: a family-only match scores between no match and the same value;
  4. a shared STYLE on top of a shared family scores between family-only and the same value;
  5. explain() adds up to _score() with the switch on, under the raw sum and under rank fusion,
     and names how much of the genre credit the family tier gave;
  6. the importer loads the map, matches values whatever their case, and skips blank rows;
  7. the switch rides engine_v2.json like the others; a value out of range is refused or ignored;
  8. the two listening-test versions turn it on, and the engine "before the catalog" has no families.

Synthetic library: nine tracks, CLAP vectors close enough that every track is a candidate.
"""
from __future__ import annotations
import csv
import json
import os
import sqlite3
import sys
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "src"))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "eval"))

import db as dbm                     # noqa: E402
from features import FEATURE_DIM     # noqa: E402
import hybrid                        # noqa: E402
import enrich                        # noqa: E402

#          0       1       2         3      4             5        6         7      8
GENRES = ["Punk", "Punk", "Grunge", "Pop", "Electronic", "House", "Zydeco", "Punk", "Grunge"]
STYLES = {7: "Ska", 8: "Ska"}                     # catalog_ids.style for tracks 7 and 8
FAMILIES = [("Punk", "Rock"), ("GRUNGE", "Rock"), ("Ska", "Rock"), ("Pop", "Pop"),
            ("Electronic", "Electronic"), ("House", "Electronic"), ("", "Rock"), ("Blank", "")]


def build_db(dirpath, name="lib.db"):
    dbp = os.path.join(dirpath, name)
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(11)
    centre = rng.normal(size=512)
    now = int(time.time())
    paths = []
    for i, g in enumerate(GENRES):
        p = os.path.join(dirpath, f"t{i:02d}.mp3")
        paths.append(p)
        v = centre + 0.3 * rng.normal(size=512)
        v = v / np.linalg.norm(v)
        conn.execute("INSERT INTO tracks(path, artist, album, title, genre, year, seconds, bytes, mtime) "
                     "VALUES(?,?,?,?,?,?,?,?,?)", (p, f"Artist {i}", "Album", f"Title {i}", g, 2005, 200, 1, now))
        conn.execute("INSERT INTO features(path, vec, dim, error, src_mtime, tempo) VALUES(?,?,?,?,?,?)",
                     (p, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(), FEATURE_DIM, None, now, 120.0))
        conn.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)", (p, v.astype(np.float32).tobytes(), 512))
    conn.commit()
    conn.close()
    return dbp, paths


def add_families(dbp, dirpath, paths, styles=True):
    fam_csv = os.path.join(dirpath, "families.csv")
    with open(fam_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["value", "family"])
        w.writerows(FAMILIES)
    ids_csv = None
    if styles:
        ids_csv = os.path.join(dirpath, "ids.csv")
        with open(ids_csv, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["path", "recording_mbid", "artist_mbids", "original_year", "style"])
            for i, st in STYLES.items():
                w.writerow([paths[i], "", "", "", st])
    conn = sqlite3.connect(dbp)
    counts = enrich.import_csvs(conn, ids_csv, None, fam_csv)
    conn.close()
    return counts


@pytest.fixture()
def plain(tmp_path):
    return build_db(str(tmp_path))


@pytest.fixture()
def tiered(tmp_path):
    dbp, paths = build_db(str(tmp_path))
    add_families(dbp, str(tmp_path), paths)
    return dbp, paths


def test_off_by_default_scores_as_a_library_without_the_table(tmp_path):
    a_dir, b_dir = tmp_path / "a", tmp_path / "b"
    a_dir.mkdir(), b_dir.mkdir()
    dbp_a, paths_a = build_db(str(a_dir))
    dbp_b, paths_b = build_db(str(b_dir))
    add_families(dbp_b, str(b_dir), paths_b, styles=False)
    a, b = hybrid.HybridEngine(dbp_a), hybrid.HybridEngine(dbp_b)
    assert b.family_credit == 0.0 and b.genre_fams is not None and a.genre_fams is None
    for si in range(len(GENRES)):
        assert a._score(si).tobytes() == b._score(si).tobytes()
        assert [os.path.basename(p) for p in a.mix(paths_a[si], size=5)] == \
               [os.path.basename(p) for p in b.mix(paths_b[si], size=5)]


def test_no_table_no_change(plain):
    dbp, _ = plain
    off, on = hybrid.HybridEngine(dbp), hybrid.HybridEngine(dbp, family_credit=0.5)
    for si in range(len(GENRES)):
        assert off._score(si).tobytes() == on._score(si).tobytes()


def test_family_only_match_sits_between_none_and_full(tiered):
    dbp, _ = tiered
    eng = hybrid.HybridEngine(dbp, family_credit=0.5)
    g = eng._genre_closeness(0)                    # seed: Punk
    assert g[1] == 1.0                             # Punk: the same value
    assert g[2] == 0.5                             # Grunge: same family, different value
    assert g[3] == 0.0 and g[4] == 0.0             # Pop, Electronic: another family
    assert g[6] == 0.0                             # Zydeco: a value the map does not know
    assert 0.0 < g[2] < g[1]
    h = eng._genre_closeness(4)                    # seed: Electronic
    assert h[5] == 0.5 and h[0] == 0.0             # House shares the family, Punk does not
    # a value the map does not know keeps plain tag matching and nothing more
    z = eng._genre_closeness(6)
    assert z[6] == 1.0 and not z[[0, 1, 2, 3, 4, 5, 7, 8]].any()


def test_shared_style_on_top_of_family_sits_between_family_only_and_full(tiered):
    dbp, _ = tiered
    eng = hybrid.HybridEngine(dbp, family_credit=0.5)
    g = eng._genre_closeness(7)                    # seed: Punk + Ska
    #   8 is Grunge + Ska: one tag of three shared, same family
    #   2 is Grunge: no tag shared, same family
    #   0 is Punk: one tag of two shared, same family
    assert g[8] == pytest.approx(1 / 3 + 0.5 * (2 / 3))
    assert g[2] == 0.5
    assert g[2] < g[8] < 1.0
    assert g[2] < g[0] < 1.0
    assert g[7] == 1.0
    # more credit never reorders exact against family: raising it lifts family-only matches only
    low = hybrid.HybridEngine(dbp, family_credit=0.25)._genre_closeness(7)
    assert low[2] == 0.25 and low[7] == 1.0 and low[2] < low[8] < g[8]


@pytest.mark.parametrize("fusion", ["raw", "rank"])
def test_explain_adds_up_with_the_switch_on(tiered, fusion):
    dbp, _ = tiered
    eng = hybrid.HybridEngine(dbp, family_credit=0.5, fusion=fusion)
    for si in (0, 4, 7):
        s = eng._score(si)
        for ci in range(len(GENRES)):
            comp = eng.explain(si, ci)
            parts = sum(v for k, v in comp.items() if k not in ("total", "genre_family"))
            assert comp["total"] == pytest.approx(s[ci], abs=1e-6)   # the raw sum is float32
            assert parts == pytest.approx(comp["total"], abs=1e-9)
    if fusion == "raw":
        comp = eng.explain(0, 2)                   # Punk seed, Grunge candidate: family only
        assert comp["genre"] == pytest.approx(eng.w["genre"] * 0.5)
        assert comp["genre_family"] == pytest.approx(comp["genre"])
        assert "genre_family" not in eng.explain(0, 1)      # same value: no family part
        assert "genre_family" not in eng.explain(0, 3)      # another family: none either


def test_importer_loads_the_map(tmp_path):
    dbp, paths = build_db(str(tmp_path))
    counts = add_families(dbp, str(tmp_path), paths)
    assert counts["genre_family"] == 6             # the two blank rows are skipped
    conn = sqlite3.connect(dbp)
    _, _, fams = hybrid._load_catalog(conn)
    conn.close()
    assert fams == {"punk": "rock", "grunge": "rock", "ska": "rock", "pop": "pop",
                    "electronic": "electronic", "house": "electronic"}
    # importing again replaces, never doubles
    assert add_families(dbp, str(tmp_path), paths)["genre_family"] == 6


def test_switch_rides_engine_v2_json(tiered):
    dbp, _ = tiered
    cfg = os.path.join(os.path.dirname(dbp), "engine_v2.json")
    with open(cfg, "w", encoding="utf-8") as f:
        json.dump({"scoring": {"family_credit": 0.4}}, f)
    assert hybrid.HybridEngine(dbp).family_credit == 0.4
    assert hybrid.HybridEngine(dbp, family_credit=0.0).family_credit == 0.0
    for bad in (1.5, -0.1, "0.5", True, None):
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"scoring": {"family_credit": bad}}, f)
        assert hybrid.HybridEngine(dbp).family_credit == 0.0
    os.remove(cfg)
    with pytest.raises(ValueError):
        hybrid.HybridEngine(dbp, family_credit=1.5)


def test_listening_versions(tiered):
    import variants as V
    dbp, _ = tiered
    base = hybrid.HybridEngine(dbp)
    assert V.variant_engine(base, "v2").family_credit == 0.0
    t = V.variant_engine(base, "v2-tiers")
    assert t.family_credit == V.FAMILY_CREDIT and t.fusion == "raw" and t.w == base.w
    assert base.family_credit == 0.0               # the shared engine is not changed
    assert t._genre_closeness(0)[2] == V.FAMILY_CREDIT
    assert "v2-feel-file-tiers" in V.VARIANTS
    # the engine as it was before the catalog has no families, whatever the switch says
    before = base.without_catalog()
    before.family_credit = 0.5
    assert before.genre_fams is None and before._genre_closeness(0)[2] == 0.0
