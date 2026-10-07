"""Measured loudness (2026-10-06, TODO.md row 65): the scan tool and the `loud` ingredient.

  the parser     ffmpeg's ebur128 summary gives integrated LUFS, loudness range and true peak;
                 a file ffmpeg could not open gives nothing; a silent one gives no LUFS
  the table      an add-on, made without touching SCHEMA_VERSION; the library still opens
  freshness      a row whose src_mtime matches the library's is skipped, a changed one is
                 measured again, an error is asked again only when asked
  the term       -|LUFS gap| / LOUD_SCALE, 0.5 where a side is unmeasured, put on the z scale
                 like every other ingredient (the formula is written out below)
  the promise    a library without the table scores exactly as before, whatever the weight
  the route      /api/lib/stats reports the weight and how many songs are measured
"""
from __future__ import annotations
import importlib.util
import os
import sqlite3
import sys

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))
sys.path.insert(0, HERE)

import db as dbm                     # noqa: E402
import hybrid                        # noqa: E402
from test_collections_fit import build_db, APP_PY   # noqa: E402

_spec = importlib.util.spec_from_file_location("loudness_scan", os.path.join(ROOT, "tools", "loudness_scan.py"))
ls = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ls)

SUMMARY = """[Parsed_ebur128_0 @ 0000014ddbff3fc0] t: 228.699977 TARGET:-23 LUFS    M: -85.3 S: -68.0     I:  -7.8 LUFS       LRA:   5.7 LU  FTPK: -80.6 -80.8 dBFS  TPK:   0.9   0.5 dBFS
[Parsed_ebur128_0 @ 0000014ddbff3fc0] Summary:

  Integrated loudness:
    I:          -7.8 LUFS
    Threshold: -17.9 LUFS

  Loudness range:
    LRA:         5.7 LU
    Threshold: -27.9 LUFS
    LRA low:   -11.0 LUFS
    LRA high:   -5.2 LUFS

  True peak:
    Peak:        0.9 dBFS
[out#0/null @ 0000014dddb51500] video:0KiB audio:39405KiB subtitle:0KiB other streams:0KiB global headers:0KiB muxing overhead: unknown
size=N/A time=00:03:48.74 bitrate=N/A speed= 310x elapsed=0:00:00.73
"""

FAILED = """[in#0 @ 000002202c360d00] invalid start code ID3[3] in RIFF header
[in#0 @ 000002202c360b80] Error opening input: Invalid data found when processing input
Error opening input file song.snd.
Error opening input files: Invalid data found when processing input
"""

SILENT = SUMMARY.replace("I:          -7.8 LUFS", "I:          -inf LUFS").replace(
    "LRA:         5.7 LU", "LRA:         0.0 LU").replace("Peak:        0.9 dBFS", "Peak:       -inf dBFS")


# ------------------------------------------------------------------ the parser

def test_parser_reads_the_three_numbers():
    assert ls.parse_summary(SUMMARY) == (-7.8, 5.7, 0.9)


def test_parser_gives_nothing_for_a_file_ffmpeg_could_not_open():
    assert ls.parse_summary(FAILED) is None
    assert ls.parse_summary("") is None


def test_parser_on_a_silent_song_gives_no_lufs():
    lufs, lra, tp = ls.parse_summary(SILENT)
    assert lufs is None and lra == 0.0 and tp is None


def test_measure_asks_an_mp3_again_as_mp3(monkeypatch, tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"x")
    asked = []

    def fake_run(exe, path, as_format=()):
        asked.append(as_format)
        return (SUMMARY if as_format == ("-f", "mp3") else FAILED), None

    monkeypatch.setattr(ls, "_run", fake_run)
    assert ls.measure("ffmpeg", str(p)) == (-7.8, 5.7, 0.9, None)
    assert asked == [(), ("-f", "mp3")]


def test_measure_names_the_failure(monkeypatch, tmp_path):
    p = tmp_path / "x.snd"
    p.write_bytes(b"x")
    monkeypatch.setattr(ls, "_run", lambda exe, path, as_format=(): (FAILED, None))
    lufs, lra, tp, err = ls.measure("ffmpeg", str(p))
    assert lufs is None and err.startswith("ffmpeg could not measure it")
    lufs, lra, tp, err = ls.measure("ffmpeg", str(tmp_path / "gone.snd"))
    assert err == ls.features.TRANSIENT_ERRORS[0]


# ------------------------------------------------------------------ the table

def _schema_version(dbp):
    c = sqlite3.connect(dbp)
    try:
        return c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    finally:
        c.close()


def _write(dbp, rows):
    """rows: {path: (lufs, mtime, error)}"""
    ls.ensure_schema(dbp)
    ls.write_batch(dbp, [(p, l, 6.0, -1.0, mt, 0.0, e) for p, (l, mt, e) in rows.items()])


def test_table_is_added_without_touching_the_schema_version(tmp_path):
    dbp = build_db(str(tmp_path))
    before = _schema_version(dbp)
    ls.ensure_schema(dbp)
    ls.ensure_schema(dbp)                       # twice is harmless
    assert _schema_version(dbp) == before == str(dbm.SCHEMA_VERSION)
    dbm.connect(dbp).close()                    # the guard still accepts the library
    assert hybrid.HybridEngine(dbp).paths       # and the engine still opens it


def test_fresh_rows_are_skipped_and_stale_ones_measured_again(tmp_path):
    dbp = build_db(str(tmp_path))
    c = sqlite3.connect(dbp)
    tracks = c.execute("SELECT path, mtime FROM tracks").fetchall()
    c.close()
    (p0, m0), (p1, m1), (p2, m2) = tracks[:3]
    _write(dbp, {p0: (-9.0, m0, None),           # fresh
                 p1: (-9.0, m1 - 100, None),     # the file changed since
                 p2: (None, m2, "ffmpeg could not measure it")})
    todo = {p for p, _ in ls.todo(dbp)}
    assert p0 not in todo and p1 in todo and p2 not in todo
    assert len(todo) == len(tracks) - 2
    assert p2 in {p for p, _ in ls.todo(dbp, retry_errors=True)}


def test_a_run_backs_up_then_writes_every_song(tmp_path, monkeypatch):
    dbp = build_db(str(tmp_path))
    monkeypatch.setattr(ls.features, "ffmpeg_state", lambda: "ok")
    monkeypatch.setattr(ls, "measure", lambda exe, path: (-10.0, 5.0, -1.0, None))
    assert ls.main(["--db", dbp, "--workers", "2"]) == 0
    c = sqlite3.connect(dbp)
    n_tracks = c.execute("SELECT count(*) FROM tracks").fetchone()[0]
    assert c.execute("SELECT count(*) FROM loudness WHERE lufs = -10.0").fetchone()[0] == n_tracks
    c.close()
    backups = [f for f in os.listdir(tmp_path) if "before-loudness" in f]
    assert len(backups) == 1
    assert ls.todo(dbp) == []                   # a second run has nothing to do


# ------------------------------------------------------------------ the term

@pytest.fixture(scope="module")
def lib_with_loudness(tmp_path_factory):
    d = str(tmp_path_factory.mktemp("loud"))
    dbp = build_db(d)
    c = sqlite3.connect(dbp)
    tracks = c.execute("SELECT path, mtime FROM tracks ORDER BY path").fetchall()
    c.close()
    rows = {p: (-6.0 - (i % 9) * 1.5, mt, None) for i, (p, mt) in enumerate(tracks)}
    rows[tracks[-1][0]] = (None, tracks[-1][1], "ffmpeg could not measure it")   # one unmeasured
    _write(dbp, rows)
    return dbp, {p: l for p, (l, _m, _e) in rows.items()}


def test_engine_reads_the_table(lib_with_loudness):
    dbp, lufs = lib_with_loudness
    e = hybrid.HybridEngine(dbp)
    for p, i in e.idx.items():
        if lufs[p] is None:
            assert np.isnan(e.lufs[i])
        else:
            assert e.lufs[i] == lufs[p]
    assert np.all(np.nan_to_num(e.lra, nan=6.0) == 6.0)


def test_the_loud_term_is_the_z_scaled_negated_gap(lib_with_loudness):
    dbp, lufs = lib_with_loudness
    e = hybrid.HybridEngine(dbp, fusion="z", weights={"loud": 0.7})
    si = 0
    L = np.array([np.nan if lufs[p] is None else lufs[p] for p in e.paths])
    # written out: the gap in LU over 10, 0.5 where either side is unmeasured, negated,
    # then minus its own mean over its own spread
    g = -np.where(np.isnan(L), 0.5, np.abs(L - L[si]) / 10.0)
    zg = (g - g.mean()) / g.std()
    terms = dict((n, (w, a)) for n, w, a in e._terms(si))
    assert terms["loud"][0] == 0.7
    assert np.allclose(terms["loud"][1], g)
    for ci in (1, 5, len(e.paths) - 1):
        comp = e.explain(e.paths[si], e.paths[ci])
        assert comp["loud"] == pytest.approx(0.7 * zg[ci])
        assert comp["total"] == e._score(si)[ci]


def test_the_v2_path_subtracts_the_gap(lib_with_loudness):
    dbp, lufs = lib_with_loudness
    base = hybrid.HybridEngine(dbp, profile="v2")
    loud = hybrid.HybridEngine(dbp, profile="v2", weights={"loud": 0.4})
    assert not loud._switched()
    si = 2
    gap = loud._loud_gap(si)
    assert np.array_equal(loud._score(si), base._score(si) - 0.4 * gap)
    ci = 7
    comp = loud.explain(loud.paths[si], loud.paths[ci])
    assert comp["loud"] == pytest.approx(-0.4 * gap[ci])
    assert comp["total"] == pytest.approx(loud._score(si)[ci])   # V2 path: float32 fingerprint sum


def test_off_by_default_in_every_profile():
    assert hybrid.DEFAULT_WEIGHTS["loud"] == 0.0
    assert hybrid.SOUND_PROFILE["weights"]["loud"] == 0.0
    assert hybrid.MUSICIP_PROFILE["weights"]["loud"] == 0.0


def test_a_library_without_the_table_scores_exactly_as_before(tmp_path):
    dbp = build_db(str(tmp_path))
    base = hybrid.HybridEngine(dbp)
    asked = hybrid.HybridEngine(dbp, weights={"loud": 2.0})
    assert asked.lufs is None
    for si in (0, 3, 11):
        assert np.array_equal(asked._score(si), base._score(si))
        assert "loud" not in asked.explain(asked.paths[si], asked.paths[1])
    zb = hybrid.HybridEngine(dbp, fusion="z")
    za = hybrid.HybridEngine(dbp, fusion="z", weights={"loud": 2.0})
    assert np.array_equal(za._score(4), zb._score(4))


def test_the_table_at_weight_zero_changes_nothing(lib_with_loudness, tmp_path):
    dbp, _ = lib_with_loudness
    plain = build_db(str(tmp_path))
    a, b = hybrid.HybridEngine(dbp), hybrid.HybridEngine(plain)
    assert a.lufs is not None and b.lufs is None
    for si in (0, 9):
        assert np.array_equal(a._score(si), b._score(si))
        assert ([os.path.basename(p) for p in a.mix(a.paths[si], size=10)]
                == [os.path.basename(p) for p in b.mix(b.paths[si], size=10)])


# ------------------------------------------------------------------ the route

def _client(tmp, dbp):
    mp = pytest.MonkeyPatch()
    cfg_home = tmp / "config"
    cfg_home.mkdir()
    empty_env = tmp / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    mp.setenv("APPDATA", str(cfg_home))
    mp.setenv("XDG_CONFIG_HOME", str(cfg_home))
    mp.setenv("ATTUNE_ENV", str(empty_env))
    web_dir = os.path.dirname(APP_PY)
    added = web_dir not in sys.path
    if added:
        sys.path.insert(0, web_dir)
    try:
        spec = importlib.util.spec_from_file_location("attune_app_loud", APP_PY)
        appmod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(appmod)
        app = appmod.create_app(dbp, engine_name="v2", playlist_dir=str(tmp))
    except Exception as e:          # pragma: no cover - environment-dependent
        mp.undo()
        pytest.skip(f"the app could not be stood up: {type(e).__name__}: {e}")
    finally:
        if added and web_dir in sys.path:
            sys.path.remove(web_dir)
    app.config["TESTING"] = True
    return app.test_client(), mp


def test_stats_reports_the_weight_and_the_count(tmp_path):
    lib = tmp_path / "library"
    lib.mkdir()
    dbp = build_db(str(lib))
    c = sqlite3.connect(dbp)
    tracks = c.execute("SELECT path, mtime FROM tracks").fetchall()
    c.close()
    _write(dbp, {p: (-8.0, mt, None) for p, mt in tracks[:5]})
    client, mp = _client(tmp_path, dbp)
    try:
        st = client.get("/api/lib/stats").get_json()
        assert st["weights"]["loud"] == 0.0
        assert st["loudness"] == 5
        r = client.get("/api/mix?i=0&size=20&loud=1.5").get_json()
        assert r["weights"]["loud"] == 1.5
    finally:
        mp.undo()
