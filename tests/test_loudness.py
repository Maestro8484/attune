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
  the scan       a fourth stage measures the new and changed songs, through the analyzer
                 program when packaged, and never fails a scan
  Radio          steers by measured loudness where the library has it, RMS exactly where not
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
import loudness as lm                # noqa: E402
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
    assert lm.parse_summary(SUMMARY) == (-7.8, 5.7, 0.9)


def test_parser_gives_nothing_for_a_file_ffmpeg_could_not_open():
    assert lm.parse_summary(FAILED) is None
    assert lm.parse_summary("") is None


def test_parser_on_a_silent_song_gives_no_lufs():
    lufs, lra, tp = lm.parse_summary(SILENT)
    assert lufs is None and lra == 0.0 and tp is None


def test_measure_asks_an_mp3_again_as_mp3(monkeypatch, tmp_path):
    p = tmp_path / "x.mp3"
    p.write_bytes(b"x")
    asked = []

    def fake_run(exe, path, as_format=()):
        asked.append(as_format)
        return (SUMMARY if as_format == ("-f", "mp3") else FAILED), None

    monkeypatch.setattr(lm, "_run", fake_run)
    assert lm.measure("ffmpeg", str(p)) == (-7.8, 5.7, 0.9, None)
    assert asked == [(), ("-f", "mp3")]


def test_measure_names_the_failure(monkeypatch, tmp_path):
    p = tmp_path / "x.snd"
    p.write_bytes(b"x")
    monkeypatch.setattr(lm, "_run", lambda exe, path, as_format=(): (FAILED, None))
    lufs, lra, tp, err = lm.measure("ffmpeg", str(p))
    assert lufs is None and err.startswith("ffmpeg could not measure it")
    lufs, lra, tp, err = lm.measure("ffmpeg", str(tmp_path / "gone.snd"))
    assert err == lm.features.TRANSIENT_ERRORS[0]


# ------------------------------------------------------------------ the table

def _schema_version(dbp):
    c = sqlite3.connect(dbp)
    try:
        return c.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0]
    finally:
        c.close()


def _write(dbp, rows):
    """rows: {path: (lufs, mtime, error)}"""
    lm.ensure_schema(dbp)
    lm.write_batch(dbp, [(p, l, 6.0, -1.0, mt, 0.0, e) for p, (l, mt, e) in rows.items()])


def test_table_is_added_without_touching_the_schema_version(tmp_path):
    dbp = build_db(str(tmp_path))
    before = _schema_version(dbp)
    lm.ensure_schema(dbp)
    lm.ensure_schema(dbp)                       # twice is harmless
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
    todo = {p for p, _ in lm.todo(dbp)}
    assert p0 not in todo and p1 in todo and p2 not in todo
    assert len(todo) == len(tracks) - 2
    assert p2 in {p for p, _ in lm.todo(dbp, retry_errors=True)}


def test_a_run_backs_up_then_writes_every_song(tmp_path, monkeypatch):
    dbp = build_db(str(tmp_path))
    monkeypatch.setattr(lm.features, "ffmpeg_state", lambda: "ok")
    monkeypatch.setattr(lm, "measure", lambda exe, path: (-10.0, 5.0, -1.0, None))
    assert ls.main(["--db", dbp, "--workers", "2"]) == 0
    c = sqlite3.connect(dbp)
    n_tracks = c.execute("SELECT count(*) FROM tracks").fetchone()[0]
    assert c.execute("SELECT count(*) FROM loudness WHERE lufs = -10.0").fetchone()[0] == n_tracks
    c.close()
    backups = [f for f in os.listdir(tmp_path) if "before-loudness" in f]
    assert len(backups) == 1
    assert lm.todo(dbp) == []                   # a second run has nothing to do


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


# ------------------------------------------------------------------ Radio's arc axis

def _old_rms_axis(dbp, paths):
    """The arc axis as the engine built it before 2026-10-06: every analyzed song's RMS
    (descriptor 75), z-scored over all of them, NaN for a pool song with none."""
    c = sqlite3.connect(dbp)
    rms = {}
    for p, blob, dim in c.execute("SELECT path, vec, dim FROM features WHERE vec IS NOT NULL"):
        v = np.frombuffer(blob, np.float32).astype(np.float64)
        if dim == 79 and np.isfinite(v).all():
            rms[p] = v[75]
    c.close()
    r = np.array(list(rms.values()))
    return np.array([(rms[p] - r.mean()) / r.std() if p in rms else np.nan for p in paths])


def test_without_the_table_radio_keeps_the_rms_axis_exactly(tmp_path):
    dbp = build_db(str(tmp_path))
    e = hybrid.HybridEngine(dbp)
    assert e.energy_axis == "rms"
    assert np.array_equal(e.energy, _old_rms_axis(dbp, e.paths))


def test_with_the_table_radio_steers_by_lufs(lib_with_loudness):
    dbp, lufs = lib_with_loudness
    e = hybrid.HybridEngine(dbp)
    assert e.energy_axis == "lufs"
    measured = ~np.isnan(e.lufs)
    est = e.lufs.copy()
    rms = e._lib_raw[:, 75].astype(np.float64)
    need = np.isnan(est) & (rms > 0)
    if need.any():                              # the one unmeasured song, if its RMS is usable
        a, b = np.polyfit(20 * np.log10(rms[measured & (rms > 0)]), est[measured & (rms > 0)], 1)
        est[need] = a * 20 * np.log10(rms[need]) + b
    z = (est - np.nanmean(est)) / np.nanstd(est)
    assert np.allclose(e.energy, z, equal_nan=True)
    lo, hi = np.nanpercentile(z, [10, 90])
    assert (e._energy_lo, e._energy_hi) == pytest.approx((lo, hi))


def test_an_unmeasured_song_gets_lufs_from_its_rms_in_decibels(tmp_path):
    """Measured songs whose LUFS is an exact line in their RMS decibels: the one left
    unmeasured is placed on that same line."""
    dbp = build_db(str(tmp_path))
    c = sqlite3.connect(dbp)
    feats = {p: np.frombuffer(b, np.float32)[75] for p, b in c.execute("SELECT path, vec FROM features")}
    tracks = dict(c.execute("SELECT path, mtime FROM tracks").fetchall())
    c.close()
    loud = [p for p, r in feats.items() if r > 0]
    assert len(loud) > 12
    line = lambda r: 0.8 * 20 * np.log10(float(r)) - 3.0        # noqa: E731
    left_out = loud[0]
    _write(dbp, {p: (line(feats[p]), tracks[p], None) for p in loud[1:]})
    e = hybrid.HybridEngine(dbp)
    i = e.idx[left_out]
    assert np.isnan(e.lufs[i])                   # the ingredient still sees it as unmeasured
    have = ~np.isnan(e.lufs)
    vals = np.append(e.lufs[have], line(feats[left_out]))
    others = ~have
    others[i] = False
    vals = np.concatenate([vals, np.full(others.sum(), np.nan)])  # songs with no usable RMS
    assert e.energy[i] == pytest.approx((line(feats[left_out]) - np.nanmean(vals)) / np.nanstd(vals))
    assert np.isnan(e.energy[others]).all()


# ------------------------------------------------------------------ the scan's stage

def test_the_stage_measures_only_what_is_new_or_changed(tmp_path, monkeypatch):
    dbp = build_db(str(tmp_path))
    c = sqlite3.connect(dbp)
    tracks = c.execute("SELECT path, mtime FROM tracks ORDER BY path").fetchall()
    c.close()
    _write(dbp, {p: (-9.0, mt, None) for p, mt in tracks[:-3]})          # three never measured
    asked = []
    monkeypatch.setattr(lm.features, "ffmpeg_state", lambda: "ok")
    monkeypatch.setattr(lm, "measure", lambda exe, path: (asked.append(path), (-12.0, 4.0, -0.5, None))[1])
    assert lm.main(["--db", dbp]) == 0
    assert sorted(asked) == sorted(p for p, _ in tracks[-3:])
    assert lm.todo(dbp) == []
    assert not [f for f in os.listdir(tmp_path) if "backup" in f]    # the stage makes no copy


def test_the_stage_without_ffmpeg_says_so_and_does_not_fail(tmp_path, monkeypatch, capsys):
    dbp = build_db(str(tmp_path))
    monkeypatch.setattr(lm.features, "ffmpeg_state", lambda: "none")
    assert lm.main(["--db", dbp]) == 0
    assert "skipped, no working ffmpeg" in capsys.readouterr().out
    c = sqlite3.connect(dbp)
    assert not c.execute("SELECT 1 FROM sqlite_master WHERE name='loudness'").fetchone()
    c.close()


def test_the_analyzer_program_runs_the_stage():
    spec = importlib.util.spec_from_file_location("worker_entry_loud", os.path.join(ROOT, "desktop", "worker_entry.py"))
    we = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(we)
    assert "loudness" in we.TOOLS
    src = open(os.path.join(ROOT, "desktop", "build.py"), encoding="utf-8").read()
    import ast
    lists = {}
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and getattr(node.targets[0], "id", "") in ("GUI_DATA", "ANALYZER_DATA"):
            lists[node.targets[0].id] = {e.elts[0].value for e in node.value.elts}
    assert "src/loudness.py" in lists["ANALYZER_DATA"] and "src/loudness.py" in lists["GUI_DATA"]


def _scanjob_module():
    spec = importlib.util.spec_from_file_location("attune_scanjob_loud", os.path.join(ROOT, "web", "scanjob.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("loud_rc", [0, 1])
def test_the_scan_runs_loudness_last_and_never_fails_for_it(tmp_path, loud_rc):
    sj = _scanjob_module()
    dbp = build_db(str(tmp_path))
    job = sj.ScanJob(dbp)
    ran = []

    def fake_exec(name, argv):
        ran.append((name, argv))
        return loud_rc if name == "loudness" else 0

    job._exec = fake_exec
    job.running = True
    job._run([str(tmp_path)], None)
    names = [n for n, _ in ran]
    assert names[-3:] == ["analyze", "embed (onnx)", "loudness"]
    argv = dict(ran)["loudness"]
    assert argv[1].endswith(os.path.join("src", "loudness.py")) and argv[2:] == ["--db", dbp]
    assert job.error in (None, "")


def test_a_failed_fingerprint_stage_skips_loudness(tmp_path):
    sj = _scanjob_module()
    dbp = build_db(str(tmp_path))
    job = sj.ScanJob(dbp)
    ran = []
    job._exec = lambda name, argv: (ran.append(name), 1 if name.startswith("embed") else 0)[1]
    job.running = True
    job._run([str(tmp_path)], None)
    assert "loudness" not in ran and job.error
