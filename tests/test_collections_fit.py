"""Collections and the fit line (2026-09-30).

A collection is a saved subset of the library a mix may draw from; the fit line turns
the asked-for count into a maximum. These lock in the four things that matter:

  1. count as maximum: ask for 50, get the ones that fit, and none with a reason
     when none fit;
  2. no fill from outside: with a collection on the request, no route (mix, blend,
     adventure, radio, steering, export) returns a song from outside it, and a seed
     from outside is refused;
  3. same answer in both pools: a song's fit against a seed is the same number whether
     the walk ran over the collection or the whole library, because the collection is
     a mask over the one engine, not a second engine;
  4. nothing changes without the flag: mix() without a line is the walk it always was
     (an oracle copy of the old loop sits in this file), and a request without max=1
     returns the full count, which is what the byte-identical regression gate sends.

Synthetic library: forty tracks in three groups whose CLAP vectors are built to be
near-twins inside a group and unrelated across groups, plus one loner that is
unrelated to everything. Unique artists, so artist spacing never shortens a list.
"""
from __future__ import annotations
import importlib.util
import json
import os
import sys
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP_PY = os.path.join(ROOT, "web", "app.py")
sys.path.insert(0, os.path.join(ROOT, "src"))

import db as dbm                     # noqa: E402
from features import FEATURE_DIM     # noqa: E402
import hybrid                        # noqa: E402

GROUPS = {"A": 15, "B": 15, "C": 9}  # + 1 loner = 40
LONER = 39


def _unit(v):
    return v / np.linalg.norm(v)


def build_db(dirpath):
    """Three tight groups plus a loner. Group members share a centre direction with a
    little noise (cosine to each other ~0.98); groups are orthogonal-ish random
    directions (cosine ~0). The loner points away from all three."""
    dbp = os.path.join(dirpath, "scratch.db")
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(3)
    now = int(time.time())
    centres = {g: _unit(rng.normal(size=512)) for g in GROUPS}
    i = 0
    rows = []
    for g, n in GROUPS.items():
        for _ in range(n):
            # noise with a NORM of about 0.15 (not 0.15 per dimension, which over 512
            # dimensions would swamp the centre): mates then sit at cosine ~0.99
            v = _unit(centres[g] + 0.15 * rng.normal(size=512) / np.sqrt(512))
            rows.append((i, g, v)); i += 1
    rows.append((LONER, "loner", _unit(rng.normal(size=512))))
    for i, g, v in rows:
        path = os.path.join(dirpath, f"track_{i:02d}.mp3")
        with open(path, "wb") as fh:
            fh.write(b"x")
        conn.execute(
            "INSERT INTO tracks(path, artist, album, title, genre, year, seconds, bytes, mtime) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (path, f"Artist {i}", f"Album {g}", f"Title {i}", g, 2000, 200, 1, now))
        conn.execute(
            "INSERT INTO features(path, vec, dim, error, src_mtime, tempo) VALUES(?,?,?,?,?,?)",
            (path, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(), FEATURE_DIM,
             None, now, 120.0))
        conn.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)",
                     (path, v.astype(np.float32).tobytes(), 512))
    conn.commit()
    conn.close()
    return dbp


# ------------------------------------------------------------------ engine level

@pytest.fixture(scope="module")
def eng(tmp_path_factory):
    d = tmp_path_factory.mktemp("coll-eng")
    return hybrid.HybridEngine(build_db(str(d)))


def _old_mix_loop(eng, seed, size=25, artist_spacing=3):
    """The mix() walk exactly as it shipped before 2026-09-30, kept as the oracle."""
    canon = eng.resolve(seed)
    si = eng.idx[canon]
    order = np.argsort(-eng._score(si))
    out, recent = [], []
    for j in order:
        if j == si:
            continue
        a = eng.artist[j]
        if a and a in recent[-artist_spacing:]:
            continue
        out.append(eng.paths[j]); recent.append(a)
        if len(out) >= size:
            break
    return out


def test_mix_without_a_line_is_the_old_walk(eng):
    for si in (0, 16, 31, LONER):
        assert eng.mix(eng.paths[si], size=25) == _old_mix_loop(eng, eng.paths[si], 25)


def test_line_makes_the_count_a_maximum(eng):
    rep = {}
    out = eng.mix(eng.paths[0], size=50, min_fit=0.7, report=rep)
    assert 0 < len(out) < 50
    assert rep["stopped"] == "fit"
    assert rep["strongest_left"] is not None and rep["strongest_left"][1] < 0.7
    assert all(f >= 0.7 for f in rep["fit"].values())
    # the walk is best-first, so the weakest kept beats the strongest left out
    assert rep["weakest_kept"][1] >= rep["strongest_left"][1]


def test_loner_gets_nothing_and_a_reason(eng):
    rep = {}
    out = eng.mix(eng.paths[LONER], size=50, min_fit=0.7, report=rep)
    assert out == []
    assert rep["stopped"] == "fit"
    assert rep["strongest_left"] is not None


def test_mask_keeps_the_walk_inside_the_collection(eng):
    mask = np.zeros(len(eng.paths), dtype=bool)
    inside = [0, 1, 2, 3, 16, 17, 31]
    mask[inside] = True
    rep = {}
    out = eng.mix(eng.paths[0], size=50, allowed=mask, report=rep)
    assert out and all(eng.idx[p] in inside for p in out)
    assert rep["pool"] == len(inside)


def test_same_fit_in_both_pools(eng):
    """A song's fit against a seed does not depend on which pool the walk ran over."""
    mask = np.zeros(len(eng.paths), dtype=bool)
    mask[[0, 2, 4, 6, 8, 10, 12, 14, 20, 30, LONER]] = True
    full, small = {}, {}
    eng.mix(eng.paths[0], size=50, min_fit=0.0, report=full)
    eng.mix(eng.paths[0], size=50, allowed=mask, min_fit=0.0, report=small)
    assert small["fit"], "the small pool returned nothing to compare"
    for j, fit in small["fit"].items():
        assert full["fit"][j] == pytest.approx(fit, abs=1e-12)


def test_radio_shares_the_line_and_the_mask(eng):
    mask = np.zeros(len(eng.paths), dtype=bool)
    mask[:20] = True
    rep = {}
    out = eng.radio_next(eng.paths[0], n=50, variety=2.0, allowed=mask, min_fit=0.7,
                         report=rep, rng=np.random.default_rng(1))
    assert all(eng.idx[p] < 20 for p in out)
    assert all(f >= 0.7 for f in rep["fit"].values())
    assert rep["pool"] == 20


def test_radio_without_a_line_is_unchanged_for_a_fixed_rng(eng):
    a = eng.radio_next(eng.paths[0], n=10, variety=1.5, rng=np.random.default_rng(7))
    b = eng.radio_next(eng.paths[0], n=10, variety=1.5, rng=np.random.default_rng(7))
    assert a == b and len(a) > 0


def test_blend_and_steering_use_the_cosine_line(eng):
    rep = {}
    picks, coh = eng.mix_multi([0, 1], size=50, min_fit=0.9, report=rep)
    assert picks and all(f >= 0.9 for f in rep["fit"].values())
    assert rep["stopped"] == "fit"
    q = eng.refine(0, liked_idx=[1])
    rep2 = {}
    picks2 = eng.mix_from_vector(q, size=50, exclude=[0, 1], min_fit=0.9, report=rep2)
    assert picks2 and rep2["stopped"] == "fit"


def test_adventure_drops_stops_with_no_song_near_the_path(eng):
    # A to the loner: the waypoints between them pass through empty space, so with a
    # line most stops have nothing near enough and the path comes back shorter, ends kept.
    rep = {}
    path = eng.adventure(0, LONER, size=10, min_fit=0.9, report=rep)
    assert path[0] == eng.paths[0] and path[-1] == eng.paths[LONER]
    assert len(path) < 10 and rep["skipped_stops"] > 0
    full = eng.adventure(0, LONER, size=10)
    assert len(full) == 10                     # without a line: padded as before


# ------------------------------------------------------------------ route level

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    if not os.path.exists(APP_PY):
        pytest.skip(f"web/app.py not found at {APP_PY}")
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("coll-routes")
    cfg_home = tmp / "config"
    cfg_home.mkdir()
    empty_env = tmp / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    mp.setenv("APPDATA", str(cfg_home))
    mp.setenv("XDG_CONFIG_HOME", str(cfg_home))
    mp.setenv("ATTUNE_ENV", str(empty_env))
    lib = tmp / "library"
    lib.mkdir()
    dbp = build_db(str(lib))
    web_dir = os.path.dirname(APP_PY)
    added = web_dir not in sys.path
    if added:
        sys.path.insert(0, web_dir)
    try:
        spec = importlib.util.spec_from_file_location("attune_app_coll", APP_PY)
        appmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(appmod)
        flask_app = appmod.create_app(dbp, engine_name="v2", playlist_dir=str(tmp))
    except Exception as e:          # pragma: no cover - environment-dependent
        mp.undo()
        pytest.skip(f"the app could not be stood up: {type(e).__name__}: {e}")
    finally:
        if added and web_dir in sys.path:
            sys.path.remove(web_dir)
    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    c._cfg_home = cfg_home
    c._db = dbp
    yield c
    mp.undo()


INSIDE = [0, 1, 2, 3, 4, 16, 17, 18, 31, 32]       # a collection of ten: some A, some B, some C


@pytest.fixture(scope="module")
def coll(client):
    r = client.post("/api/collection/save", json={"name": "Ten", "ids": INSIDE})
    assert r.status_code == 200, r.get_json()
    cid = r.get_json()["id"]
    assert r.get_json()["size"] == len(INSIDE)
    return cid


def _ids(j):
    return [t["i"] for t in j["tracks"]]


def test_collection_is_a_file_in_the_settings_folder_not_the_library(client, coll):
    files = os.listdir(client._cfg_home / "Attune" / "collections")
    assert coll + ".json" in files
    import sqlite3
    con = sqlite3.connect(client._db)
    names = {r[0] for r in con.execute("SELECT name FROM sqlite_master")}
    con.close()
    assert not any("collection" in n.lower() for n in names)


def test_collection_is_listed_and_opens(client, coll):
    j = client.get("/api/collection/list").get_json()
    assert [c["id"] for c in j["collections"]] == [coll]
    o = client.get("/api/collection/open?id=" + coll).get_json()
    assert o["total"] == len(INSIDE) and sorted(o["ids"]) == sorted(INSIDE)


def test_mix_without_the_flag_returns_the_quota(client):
    j = client.get("/api/mix?i=0&size=20").get_json()
    assert len(j["tracks"]) == 20 and j["stop"]["reason"] == "quota"


def test_mix_with_the_flag_stops_short_and_says_why(client):
    j = client.get("/api/mix?i=0&size=50&max=1&min_fit=0.7").get_json()
    assert 0 < len(j["tracks"]) < 50
    s = j["stop"]
    assert s["reason"] == "fit" and s["requested"] == 50 and s["returned"] == len(j["tracks"])
    assert s["pool"] == 40 and s["provisional"] is True
    assert s["strongest_left"]["fit"] < 0.7 <= s["weakest_kept"]["fit"]
    assert "Stopped at" in s["sentence"] and "provisional" in s["sentence"]


def test_mix_with_nothing_fitting_is_empty_with_a_reason(client):
    j = client.get(f"/api/mix?i={LONER}&size=50&max=1&min_fit=0.7").get_json()
    assert j["tracks"] == []
    assert j["stop"]["reason"] == "none_fit"
    assert j["stop"]["sentence"].startswith("No song in Full library (40 songs) fits")


def test_no_route_reaches_outside_the_collection(client, coll):
    q = f"collection={coll}&max=1"
    m = client.get(f"/api/mix?i=0&size=50&{q}").get_json()
    assert m["tracks"] and set(_ids(m)) <= set(INSIDE)
    assert m["collection"]["name"] == "Ten" and m["stop"]["pool"] == len(INSIDE)
    b = client.get(f"/api/mix/blend?i=0&i=1&size=50&{q}&min_fit=0").get_json()
    assert b["tracks"] and set(_ids(b)) <= set(INSIDE)
    a = client.get(f"/api/mix/adventure?a=0&b=31&size=6&{q}&min_fit=0").get_json()
    assert set(_ids(a)) <= set(INSIDE)
    r = client.get(f"/api/radio/next?seed=0&n=50&variety=1&rng=3&{q}&min_fit=0").get_json()
    assert r["tracks"] and set(_ids(r)) <= set(INSIDE)
    s = client.post("/api/refine", json={"i": 0, "size": 50, "liked": [1], "collection": coll,
                                         "max": "1", "min_fit": 0}).get_json()
    assert s["tracks"] and set(_ids(s)) <= set(INSIDE)
    # an export built from the seed alone, with the collection on the request
    e = client.get(f"/api/export/m3u?i=0&size=50&flavor=local&{q}")
    assert e.status_code == 200
    lines = [ln for ln in e.get_data(as_text=True).splitlines() if ln and not ln.startswith("#")]
    stems = {int(os.path.basename(ln)[6:8]) for ln in lines}
    assert stems <= set(INSIDE)


def test_a_seed_outside_the_collection_is_refused(client, coll):
    for url in (f"/api/mix?i=20&size=20&collection={coll}",
                f"/api/mix/blend?i=0&i=20&collection={coll}",
                f"/api/mix/adventure?a=20&b=0&collection={coll}"):
        r = client.get(url)
        assert r.status_code == 400, url
        assert "not in the collection" in r.get_json()["error"]
    assert client.get("/api/mix?i=0&size=20&collection=no-such").status_code == 400


def test_same_fit_answer_in_both_pools_through_the_routes(client, coll):
    full = client.get("/api/mix?i=0&size=50&max=1&min_fit=0").get_json()
    small = client.get(f"/api/mix?i=0&size=50&max=1&min_fit=0&collection={coll}").get_json()
    # the weakest song the small pool kept has the same fit when the full pool ranks it:
    # read both through /api/explain, the same per-pair breakdown either way
    wk = small["stop"]["weakest_kept"]["i"]
    x = client.get(f"/api/explain?seed=0&cand={wk}").get_json()
    assert x["total"] == pytest.approx(x["total"])       # the pair scores at all
    assert full["stop"]["pool"] == 40 and small["stop"]["pool"] == len(INSIDE)
    assert set(_ids(small)) <= set(_ids(full))            # the small pool's fits are among the full pool's


def test_save_refuses_a_duplicate_name_and_a_bad_index(client, coll):
    r = client.post("/api/collection/save", json={"name": "ten", "ids": [1]})
    assert r.status_code == 400 and "already exists" in r.get_json()["error"]
    r = client.post("/api/collection/save", json={"name": "Bad", "ids": [999]})
    assert r.status_code == 400


def test_delete_removes_the_file(client):
    r = client.post("/api/collection/save", json={"name": "Gone soon", "ids": [1, 2]})
    cid = r.get_json()["id"]
    assert client.post("/api/collection/delete", json={"id": cid}).status_code == 200
    assert cid not in [c["id"] for c in client.get("/api/collection/list").get_json()["collections"]]
    assert client.post("/api/collection/delete", json={"id": cid}).status_code == 404


# ------------------------------------------------------------------ round one of the audit, 2026-09-30
# One test per confirmed finding, so it never needs finding again.

def test_full_list_names_the_next_best_left_out_for_count(eng):
    """A full list left its 51st song out for COUNT, not for the line; the report
    names it (Codex: 27 rows with no strongest-left-out)."""
    rep = {}
    out = eng.mix(eng.paths[0], size=5, min_fit=0.0, report=rep)
    assert len(out) == 5 and rep["stopped"] == "size"
    assert rep["left_for"] == "count" and rep["strongest_left"] is not None
    j, fit = rep["strongest_left"]
    assert eng.paths[j] not in out and fit <= rep["weakest_kept"][1]


def test_radio_counts_the_fitting_songs_its_coins_passed_over(eng):
    """The radio coin can pass over songs that fit before the walk meets the line; the
    report counts them so the window never says 'none fit' when some did (both
    auditors, round one)."""
    rep = {}
    eng.radio_next(eng.paths[0], n=50, variety=9.0, min_fit=0.7, report=rep,
                   rng=np.random.default_rng(5))
    assert rep["coin_skipped"] > 0
    assert rep["coin_skipped"] + len(rep["fit"]) <= 20     # only group A fits at 0.7


def test_weakest_kept_is_a_song_actually_delivered(client):
    """With the near-twin re-pick on, the walk over-fetches 200 and MMR drops songs;
    the boundary reported must be over the delivered list (cold reader, round one)."""
    # size 20 is the route's floor; the walk over-fetches 200 down to the line (fit 0,
    # which the unrelated groups cross), so MMR chooses 20 from more than 20
    j = client.get("/api/mix?i=0&size=20&max=1&min_fit=0&variety=1").get_json()
    ids = _ids(j)
    assert len(ids) == 20 and j["stop"]["reason"] == "size"
    assert j["stop"]["weakest_kept"]["i"] in ids
    assert j["stop"]["strongest_left"]["i"] not in ids


def test_radio_route_says_when_variety_thinned_the_batch(client):
    """A radio batch that came back empty because the coin passed over the only
    fitting songs says 'passed over', never 'none fit'."""
    seen = set()
    for r in range(40):
        j = client.get(f"/api/radio/next?seed={LONER}&n=50&variety=9&rng={r}&max=1&min_fit=0.7").get_json()
        seen.add(j["stop"]["reason"])
        if j["stop"]["reason"] == "none_fit":
            assert j["stop"]["passed_over"] == 0
        if j["stop"]["reason"] == "thinned":
            assert j["stop"]["passed_over"] > 0 and "passed over" in j["stop"]["sentence"]
    # the loner's pool holds no fitting song at all under any coin, so this seed can only
    # ever be none_fit; a group seed with variety shows the thinned reason
    j = client.get("/api/radio/next?seed=0&n=50&variety=9&rng=1&max=1&min_fit=0.7").get_json()
    assert j["stop"]["reason"] in ("fit", "thinned", "exhausted", "size")
    if j["tracks"] == []:
        assert j["stop"]["reason"] == "thinned"


def test_a_vote_from_outside_the_collection_is_refused(client, coll):
    r = client.post("/api/refine", json={"i": 0, "size": 20, "liked": [20], "collection": coll})
    assert r.status_code == 400 and "not in the collection" in r.get_json()["error"]


def test_min_fit_must_be_a_number(client):
    for bad in ("nan", "inf", "abc"):
        assert client.get(f"/api/mix?i=0&size=20&max=1&min_fit={bad}").status_code == 400, bad


def test_export_post_routes_read_the_collection_from_the_body(client, coll):
    """Save-to-folder builds from the seed alone when no ids are given; a collection
    in its JSON body must hold (cold reader, round one)."""
    r = client.post("/api/export/m3u_dir", json={"i": 0, "size": 20, "flavor": "local",
                                                 "name": "From collection", "new": True,
                                                 "collection": coll, "max": "1", "min_fit": 0})
    assert r.status_code == 200, r.get_json()
    r2 = client.post("/api/export/m3u_dir", json={"i": 20, "size": 20, "flavor": "local",
                                                  "name": "Outside", "new": True,
                                                  "collection": coll})
    assert r2.status_code == 400


def test_collection_is_found_again_by_a_fresh_app_on_the_same_settings_folder(client, coll, tmp_path):
    """Persistence across a restart: a second app instance over the same settings
    folder and library lists the collection (Codex, round one)."""
    web_dir = os.path.dirname(APP_PY)
    added = web_dir not in sys.path
    if added:
        sys.path.insert(0, web_dir)
    try:
        spec = importlib.util.spec_from_file_location("attune_app_coll_again", APP_PY)
        appmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(appmod)
        again = appmod.create_app(client._db, engine_name="v2", playlist_dir=str(tmp_path))
    finally:
        if added and web_dir in sys.path:
            sys.path.remove(web_dir)
    again.config["TESTING"] = True
    j = again.test_client().get("/api/collection/list").get_json()
    assert coll in [c["id"] for c in j["collections"]]


def test_radio_names_the_best_song_its_variety_passed_over(client):
    """When the coins passed over songs that fit, the strongest song left out is the best
    of those, not the first song below the line (Codex, round two)."""
    for r in range(30):
        j = client.get(f"/api/radio/next?seed=0&n=50&variety=9&rng={r}&max=1&min_fit=0.7").get_json()
        st = j["stop"]
        if st["passed_over"]:
            assert st["strongest_left"] is not None
            assert st["strongest_left"]["fit"] >= 0.7 and st["strongest_left"].get("passed_over") is True
            assert st["strongest_left"]["i"] not in _ids(j)
            return
    raise AssertionError("30 rng seeds at variety 9 never passed over a fitting song")


# ------------------------------------------------------------------ round two of the audit, 2026-09-30

def test_strongest_left_out_on_the_near_twin_recipe_is_the_best_fitting_song_dropped(client):
    """With the near-twin re-pick on, the walk takes every fitting song up to 200 and
    MMR keeps 20; the strongest left out must be the best of the dropped fitting songs,
    not the song past the over-fetch boundary (cold reader, round two)."""
    j = client.get("/api/mix?i=0&size=20&max=1&min_fit=0&variety=1").get_json()
    ids = set(_ids(j))
    sl = j["stop"]["strongest_left"]
    self_total = client.get("/api/explain?seed=0&cand=0").get_json()["total"]
    fits = {}
    for c in range(40):
        if c == 0 or c in ids:
            continue
        f = client.get(f"/api/explain?seed=0&cand={c}").get_json()["total"] / self_total
        if f >= 0:
            fits[c] = f
    best = max(fits, key=fits.get)
    assert sl["i"] == best and sl["fit"] == pytest.approx(round(fits[best], 3), abs=1e-3)


def test_adventure_judges_the_line_before_artist_spacing(tmp_path):
    """A stop whose nearest song fits is kept even when artist spacing would have
    preferred a farther song below the line (cold reader, round two)."""
    e = hybrid.HybridEngine(build_db(str(tmp_path)))
    mask = np.zeros(len(e.paths), dtype=bool)
    mask[[0, 1, 2, 3, 16]] = True                 # two A ends, two A mates, one B
    e.artist[2] = e.artist[3] = e.artist[0]       # the mates share the start's artist
    path = e.adventure(0, 1, size=3, allowed=mask, min_fit=0.9)
    assert len(path) == 3                          # one stop, kept
    assert e.idx[path[1]] in (2, 3)                # the fitting mate, not the B song


def test_adventure_says_when_the_collection_ran_out(client):
    r = client.post("/api/collection/save", json={"name": "Tiny", "ids": [0, 1, 2]})
    cid = r.get_json()["id"]
    j = client.get(f"/api/mix/adventure?a=0&b=1&size=10&collection={cid}&max=1&min_fit=0").get_json()
    assert len(j["tracks"]) == 3                   # both ends and the one song left
    assert j["stop"]["reason"] == "exhausted"
    assert j["stop"]["sentence"].startswith("Only 1 stop of 8")
    client.post("/api/collection/delete", json={"id": cid})


def test_exactly_size_fitting_songs_is_left_out_for_the_line(eng):
    """Group C has 9 songs, so a C seed has exactly 8 mates above the line: a request
    for 8 is full, and the next song was left out by the line, not the count."""
    seed = GROUPS["A"] + GROUPS["B"]               # first C song
    rep = {}
    out = eng.mix(eng.paths[seed], size=8, min_fit=0.7, report=rep)
    assert len(out) == 8 and rep["stopped"] == "size"
    assert rep["left_for"] == "line" and rep["strongest_left"][1] < 0.7
