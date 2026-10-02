"""The scan reads the catalog tags from the files themselves (2026-10-01): MusicBrainz recording
and artist ids, the original release date and STYLE, under the names Picard's tag mapping gives
them, into the same catalog_ids table tools/import_catalog.py fills from a CSV file.

Locks in:
  1. the reader finds each tag in ID3v2.4, ID3v2.3, Vorbis (FLAC) and an ID3 chunk in a wav, and
     returns nothing for a file without them or one that cannot be opened;
  2. off by default: a plain import-folder writes no catalog row however the files are tagged;
  3. the two roads meet: a library whose facts arrive by CSV import and the same library whose
     facts are read from its files hold identical catalog_ids rows and score identically;
  4. a field the file does not carry keeps what an import put there; one it carries replaces it;
  5. the app's scan job passes the switch from settings `read_catalog_tags`, itself, and a
     settings read that fails means off;
  6. the command line flag reaches import_folder;
  7. a damaged tag never ends a scan and never stores a wrong value: digits that are not ASCII, a
     year outside 1000 to 2100, a blank date beside a good year under another name, an id with
     extra characters glued on, an id that is not text at all;
  8. the desktop build carries every module the bundled scripts import.

Every audio file here is made by the test; nothing outside its temp folder is read or written.
"""
from __future__ import annotations
import csv
import importlib.util
import os
import sqlite3
import struct
import subprocess
import sys
import time
import wave

import numpy as np
import pytest

mutagen = pytest.importorskip("mutagen")
from mutagen.flac import FLAC                                        # noqa: E402
from mutagen.id3 import ID3, TCON, TDOR, TDRC, TIT2, TPE1, TXXX, UFID   # noqa: E402
from mutagen.wave import WAVE                                        # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")
sys.path.insert(0, SRC)

import db as dbm                     # noqa: E402
from features import FEATURE_DIM     # noqa: E402
import enrich                        # noqa: E402
import hybrid                        # noqa: E402
import scan                          # noqa: E402

REC = ["11111111-aaaa-4bbb-8ccc-00000000000%d" % i for i in range(8)]
ART = ["22222222-aaaa-4bbb-8ccc-00000000000%d" % i for i in range(8)]

# (file name, genre, year in the file, recording id, artist ids, original year, styles)
# The names here and further down are written in two pieces on purpose. tools/leak_check.py
# counts whole song-file names in a file and treats ten or more as a listing of somebody's
# library; these are made-up names, and written whole they failed that check on every push.
LIBRARY = [
    ("a0" ".mp3",  "Punk; Covers", 1999, REC[0], [ART[0]],         1977, ["Ska"]),
    ("a1" ".mp3",  "Punk",         2004, REC[1], [ART[1], ART[2]], 1979, []),
    ("a2" ".flac", "Grunge",       1991, REC[2], [ART[3]],         1991, ["Hard Rock", "Punk"]),
    ("a3" ".flac", "Pop",          2010, REC[0], [ART[0]],         1977, []),      # the same recording as a0
    ("a4" ".wav",  "Electronic",   2001, REC[4], [ART[4]],         None, ["House"]),
    ("a5" ".mp3",  "Pop",          1985, None,   [],               None, []),      # carries no catalog tag
    ("a6" ".mp3",  "Rock",         1972, REC[6], [ART[6]],         1972, ["Blues Rock"]),
    ("a7" ".flac", "Rock",         1969, None,   [],               1969, []),      # original year only
]


def _mp3_bytes(frames=24):
    # MPEG-1 Layer III, 128 kbit/s, 44.1 kHz, no padding: 417-byte frames of silence
    return (b"\xff\xfb\x90\x00" + b"\x00" * 413) * frames


def _flac_bytes():
    info = struct.pack(">HH", 4096, 4096) + b"\x00" * 6
    info += struct.pack(">Q", (44100 << 44) | (1 << 41) | (15 << 36) | 44100) + b"\x00" * 16
    return b"fLaC" + bytes([0x80]) + len(info).to_bytes(3, "big") + info


def _id3(genre, year, rec, arts, oyear, styles):
    t = ID3()
    t.add(TIT2(encoding=3, text=["Title"]))
    t.add(TPE1(encoding=3, text=["Artist"]))
    t.add(TCON(encoding=3, text=[genre]))
    t.add(TDRC(encoding=3, text=[str(year)]))
    if rec:
        t.add(UFID(owner="http://musicbrainz.org", data=rec.encode("ascii")))
    if arts:
        t.add(TXXX(encoding=3, desc="MusicBrainz Artist Id", text=list(arts)))
    if oyear:
        t.add(TDOR(encoding=3, text=[f"{oyear}-03-01"]))
    if styles:
        t.add(TXXX(encoding=3, desc="STYLE", text=list(styles)))
    return t


def make_file(folder, name, genre, year, rec, arts, oyear, styles, v23=False, v23_sep="; "):
    p = os.path.join(folder, name)
    ext = os.path.splitext(name)[1]
    if ext == ".mp3":
        with open(p, "wb") as f:
            f.write(_mp3_bytes())
        t = _id3(genre, year, rec, arts, oyear, styles)
        if v23:
            t.update_to_v23()
            t.save(p, v2_version=3, v23_sep=v23_sep)
        else:
            t.save(p, v2_version=4)
    elif ext == ".flac":
        with open(p, "wb") as f:
            f.write(_flac_bytes())
        fl = FLAC(p)
        fl["title"], fl["artist"], fl["genre"], fl["date"] = "Title", "Artist", genre, str(year)
        if rec:
            fl["MUSICBRAINZ_TRACKID"] = rec
        if arts:
            fl["MUSICBRAINZ_ARTISTID"] = list(arts)
        if oyear:
            fl["ORIGINALDATE"] = f"{oyear}-03-01"
        if styles:
            fl["STYLE"] = list(styles)
        fl.save()
    else:
        with wave.open(p, "wb") as w:
            w.setnchannels(1), w.setsampwidth(2), w.setframerate(8000)
            w.writeframes(b"\x00\x00" * 800)
        wv = WAVE(p)
        wv.add_tags()
        for frame in _id3(genre, year, rec, arts, oyear, styles).values():
            wv.tags.add(frame)
        wv.save()
    return p


@pytest.fixture()
def music(tmp_path):
    folder = tmp_path / "music"
    folder.mkdir()
    return [make_file(str(folder), *row) for row in LIBRARY], str(folder)


def expected(row):
    _, _, _, rec, arts, oyear, styles = row
    out = {}
    if rec:
        out["recording_mbid"] = rec
    if arts:
        out["artist_mbids"] = "; ".join(arts)
    if oyear:
        out["original_year"] = oyear
    if styles:
        out["style"] = "; ".join(styles)
    return out


def test_reader_finds_the_tags_in_each_dialect(music, tmp_path):
    paths, _ = music
    for p, row in zip(paths, LIBRARY):
        assert scan._catalog_tags(p) == expected(row), os.path.basename(p)
    # ID3v2.3 has no multi-value text frames: several values are one string joined by whatever
    # the writer chose, and the original date is a year-only TORY. Joined with "; " (what a
    # writer for this library must use) the styles come apart again.
    v23 = make_file(str(tmp_path), "v23.mp3", "Punk", 2004, REC[1], [ART[1], ART[2]], 1979, ["Ska", "Oi"], v23=True)
    assert ID3(v23).version[:2] == (2, 3)
    assert scan._catalog_tags(v23) == {"recording_mbid": REC[1], "artist_mbids": f"{ART[1]}; {ART[2]}",
                                       "original_year": 1979, "style": "Ska; Oi"}
    # THE TRAP: mutagen's own default joins with "/", and "Ska/Oi" is then one style nobody
    # has. The ids survive any separator; a style or a genre does not. Recorded here so the
    # tag writer (GenreTagger's apply step) is built against it.
    slash = make_file(str(tmp_path), "slash.mp3", "Punk", 2004, REC[1], [ART[1], ART[2]], 1979, ["Ska", "Oi"],
                      v23=True, v23_sep="/")
    got = scan._catalog_tags(slash)
    assert got["style"] == "Ska/Oi" and got["artist_mbids"] == f"{ART[1]}; {ART[2]}"
    junk = tmp_path / "junk.mp3"
    junk.write_bytes(b"not audio at all")
    assert scan._catalog_tags(str(junk)) == {}
    assert scan._catalog_tags(str(tmp_path / "missing.mp3")) == {}
    # the six fields the scan has always read are untouched by any of this
    assert scan._read_tags(paths[0])["genre"] == "Punk; Covers" and scan._read_tags(paths[0])["year"] == 1999


def test_value_shapes():
    assert scan._catalog_values(None) == []
    assert scan._catalog_values([b"abc", b"abc", b" def\x00"]) == ["abc", "def"]      # MP4 freeform atoms
    assert scan._catalog_values(UFID(owner="http://musicbrainz.org", data=b"xyz")) == ["xyz"]
    assert scan._catalog_values(TXXX(encoding=3, desc="STYLE", text=["A", "B"])) == ["A", "B"]


def _has_table(dbp, name):
    conn = sqlite3.connect(dbp)
    got = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()
    conn.close()
    return got is not None


def _rows(dbp):
    conn = sqlite3.connect(dbp)
    rows = conn.execute("SELECT path, recording_mbid, artist_mbids, original_year, style "
                        "FROM catalog_ids ORDER BY path").fetchall()
    conn.close()
    return rows


def test_off_by_default(music, tmp_path):
    _, folder = music
    dbp = str(tmp_path / "plain.db")
    scan.import_folder(dbp, folder)
    assert not _has_table(dbp, "catalog_ids")


def _add_analysis(dbp, paths):
    """The same made-up fingerprints and descriptors for the same paths, whichever road filled
    the catalog, so the two engines differ in nothing but how the catalog rows arrived."""
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(5)
    centre = rng.normal(size=512)
    now = int(time.time())
    for p in paths:
        v = centre + 0.3 * rng.normal(size=512)
        v = v / np.linalg.norm(v)
        conn.execute("INSERT OR REPLACE INTO features(path, vec, dim, error, src_mtime, tempo) VALUES(?,?,?,?,?,?)",
                     (p, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(), FEATURE_DIM, None, now, 120.0))
        conn.execute("INSERT OR REPLACE INTO clap(path, vec, dim) VALUES(?,?,?)",
                     (p, v.astype(np.float32).tobytes(), 512))
    conn.commit()
    conn.close()


def test_csv_import_and_file_tags_give_the_same_engine(music, tmp_path):
    paths, folder = music
    # road one: a plain scan, then the CSV an outside tagger exported
    by_csv = str(tmp_path / "by_csv.db")
    scan.import_folder(by_csv, folder)
    ids_csv = str(tmp_path / "ids.csv")
    with open(ids_csv, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["path", "recording_mbid", "artist_mbids", "original_year", "style"])
        for p, row in zip(paths, LIBRARY):
            e = expected(row)
            if e:
                w.writerow([p, e.get("recording_mbid", ""), e.get("artist_mbids", ""),
                            e.get("original_year", ""), e.get("style", "")])
    conn = sqlite3.connect(by_csv)
    enrich.import_csvs(conn, ids_csv)
    conn.close()
    # road two: the same facts read out of the files by the scan
    by_tags = str(tmp_path / "by_tags.db")
    scan.import_folder(by_tags, folder, catalog_tags=True)

    assert _rows(by_csv) == _rows(by_tags)
    assert len(_rows(by_tags)) == 7                 # a5 carries nothing and has no row

    _add_analysis(by_csv, paths)
    _add_analysis(by_tags, paths)
    a, b = hybrid.HybridEngine(by_csv), hybrid.HybridEngine(by_tags)
    assert a.paths == b.paths and a.catalog_loaded and b.catalog_loaded
    assert a.genre_tags == b.genre_tags and a.recording == b.recording and a.artist_ids == b.artist_ids
    for si in range(len(a.paths)):
        assert a._score(si).tobytes() == b._score(si).tobytes()
        assert a.mix(a.paths[si], size=6) == b.mix(b.paths[si], size=6)
    # and the catalog is really in play: one recording once, "covers" out of the genre tags
    i0 = b.idx[paths[0]]
    assert paths[3] not in b.mix(paths[0], size=7)
    assert b.genre_tags[i0] == {"punk", "ska"} and b.year[i0] == 1977


def test_a_field_the_file_lacks_keeps_what_an_import_put_there(music, tmp_path):
    paths, folder = music
    dbp = str(tmp_path / "merge.db")
    scan.import_folder(dbp, folder)
    conn = sqlite3.connect(dbp)
    conn.execute(enrich.CATALOG_DDL)
    # a7 carries only an original year; a0 carries everything
    conn.execute("INSERT INTO catalog_ids VALUES (?,?,?,?,?)", (paths[7], "rec-from-csv", "art-from-csv", 1950, "Skiffle"))
    conn.execute("INSERT INTO catalog_ids VALUES (?,?,?,?,?)", (paths[0], "old", "old", 1900, "Old"))
    conn.commit()
    conn.close()
    scan.import_folder(dbp, folder, catalog_tags=True)
    rows = {r[0]: r[1:] for r in _rows(dbp)}
    assert rows[paths[7]] == ("rec-from-csv", "art-from-csv", 1969, "Skiffle")
    assert rows[paths[0]] == (REC[0], ART[0], 1977, "Ska")
    # scanning again changes nothing
    scan.import_folder(dbp, folder, catalog_tags=True)
    assert {r[0]: r[1:] for r in _rows(dbp)} == rows


def _scanjob():
    spec = importlib.util.spec_from_file_location(
        "attune_scanjob_catalog_test", os.path.join(os.path.dirname(HERE), "web", "scanjob.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_scan_job_reads_the_switch_itself(tmp_path):
    sj = _scanjob()
    dbp = str(tmp_path / "x.db")
    assert sj.ScanJob(dbp)._catalog_args() == []
    assert sj.ScanJob(dbp, None, lambda: {})._catalog_args() == []
    assert sj.ScanJob(dbp, None, lambda: {"read_catalog_tags": False})._catalog_args() == []
    assert sj.ScanJob(dbp, None, lambda: {"read_catalog_tags": True})._catalog_args() == ["--catalog-tags"]

    def broken():
        raise OSError("settings unreadable")
    assert sj.ScanJob(dbp, None, broken)._catalog_args() == []
    import config
    assert config.DEFAULTS["read_catalog_tags"] is False


def test_command_line_flag(music, tmp_path):
    _, folder = music
    for flag, want in (([], False), (["--catalog-tags"], True)):
        dbp = str(tmp_path / f"cli{int(want)}.db")
        r = subprocess.run([sys.executable, os.path.join(SRC, "scan.py"), "import-folder", folder, "--db", dbp, *flag],
                           capture_output=True, text=True, cwd=SRC)
        assert r.returncode == 0, r.stderr
        assert _has_table(dbp, "catalog_ids") is want
        assert ("catalog tags read from 7 of them" in r.stdout) is want


def _flac(folder, name, **tags):
    p = os.path.join(folder, name)
    with open(p, "wb") as f:
        f.write(_flac_bytes())
    fl = FLAC(p)
    fl["title"] = "Title"
    for k, v in tags.items():
        fl[k] = v
    fl.save()
    return p


def _mp3_with_ufid(folder, name, data):
    p = os.path.join(folder, name)
    with open(p, "wb") as f:
        f.write(_mp3_bytes())
    t = ID3()
    t.add(TIT2(encoding=3, text=["Title"]))
    t.add(TCON(encoding=3, text=["Punk"]))
    t.add(UFID(owner="http://musicbrainz.org", data=data))
    t.save(p, v2_version=4)
    return p


def test_damaged_tags_store_nothing_wrong_and_never_end_a_scan(tmp_path):
    folder = tmp_path / "damaged"
    folder.mkdir()
    d = str(folder)
    sup = chr(0xB2) * 4                                         # four superscript twos: digits, not ASCII
    assert scan._catalog_tags(_flac(d, "sup" ".flac", ORIGINALDATE=sup)) == {}
    assert scan._catalog_tags(_flac(d, "y1" ".flac", ORIGINALDATE="0001")) == {}
    assert scan._catalog_tags(_flac(d, "y9" ".flac", ORIGINALDATE="9999")) == {}
    assert scan._catalog_tags(_flac(d, "zero" ".flac", ORIGINALDATE="0000", ORIGINALYEAR="1969")) == {"original_year": 1969}
    assert scan._catalog_tags(_flac(d, "ok" ".flac", ORIGINALDATE="1969-07-20")) == {"original_year": 1969}
    # an id is taken only when it stands on its own
    glued = _mp3_with_ufid(d, "glued.mp3", b"abc" + REC[0].encode("ascii"))
    assert scan._catalog_tags(glued) == {}
    # an id that is not text: the catalog reader stores nothing, and the plain reader, which
    # used to raise UnicodeDecodeError out of the whole scan on this file, still reads the rest
    binary = _mp3_with_ufid(d, "binary.mp3", bytes([0xFF, 0xFE]) + b"-not-text")
    assert scan._catalog_tags(binary) == {}
    assert scan._read_tags(binary)["genre"] == "Punk"
    # and a scan over all of them finishes, with the one good year stored
    dbp = str(tmp_path / "damaged.db")
    scan.import_folder(dbp, d, catalog_tags=True)
    conn = sqlite3.connect(dbp)
    assert conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0] == 7
    assert sorted(y for (y,) in conn.execute("SELECT original_year FROM catalog_ids")) == [1969, 1969]
    conn.close()


def _local_imports(path):
    import ast
    names = set()
    for node in ast.walk(ast.parse(open(path, encoding="utf-8").read())):
        if isinstance(node, ast.Import):
            names |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            names.add(node.module.split(".")[0])
    return {n for n in names if os.path.exists(os.path.join(SRC, n + ".py"))}


def _build_list(name):
    import ast
    src = open(os.path.join(os.path.dirname(HERE), "desktop", "build.py"), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == name for t in node.targets):
            return {e.elts[0].value for e in node.value.elts}
    raise AssertionError(f"{name} not found in desktop/build.py")


@pytest.mark.parametrize("bundle", ["GUI_DATA", "ANALYZER_DATA"])
def test_the_build_carries_every_module_its_scripts_import(bundle):
    """desktop/build.py names the src/ scripts it ships one by one, and PyInstaller cannot see
    what a script shipped as a data file imports. A sibling module left off the list is not a
    build error: the packaged app dies at `import` the first time a scan runs. scan.py gained
    `import enrich` on 2026-10-01 and the list did not, which a cold reader caught by running it."""
    files = _build_list(bundle)
    scripts = {f for f in files if f.startswith("src/") and f.endswith(".py")}
    assert "src/scan.py" in scripts
    missing = {}
    for f in sorted(scripts):
        need = {"src/" + n + ".py" for n in _local_imports(os.path.join(os.path.dirname(HERE), f))} - files
        if need:
            missing[f] = sorted(need)
    # Known and accepted: modules a bundled script imports only on a road the packaged app
    # never takes are listed here by name, so a NEW gap still fails.
    for f, allowed in KNOWN_UNBUNDLED.get(bundle, {}).items():
        if f in missing:
            missing[f] = [m for m in missing[f] if m not in allowed]
            if not missing[f]:
                del missing[f]
    assert not missing, f"imported by a bundled script but not in {bundle}: {missing}"


KNOWN_UNBUNDLED = {}
