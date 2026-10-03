"""Full tag editing and ratings in files (2026-10-02, TODO.md rows 52 and 54, ISSUES.md 96).

On two tiny silent fixtures (tests/fixtures/silence.mp3, 0.3 s at 32 kbps; silence.flac),
copied to a temp folder per test module:

  1. tagfile reads and writes every field in its table on both formats, keeps a field that
     held several values as several, and removes a field written empty;
  2. a comment on an MP3 saves (it used to raise: ISSUES.md row 96);
  3. a rating goes into the file as the shared POPM byte (MP3) and RATING 0 to 100 plus
     FMPS_RATING (FLAC), reads back as the same stars, and every player's own POPM frame
     is updated; clearing removes them;
  4. a write keeps the file's modified time and its ID3 version (v2.3 stays v2.3);
  5. a cover is written, read and removed on both formats; a non-image is refused;
  6. through the server: the tag routes carry the field table, the rating route writes
     the file and says so, the cover routes work, the ratings import reads the files'
     ratings into the library (the file wins), and the Plex hook is called with the
     path and the stars, off the request thread.
"""
from __future__ import annotations
import importlib.util
import os
import shutil
import sys
import threading
import time

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIX = os.path.join(HERE, "fixtures")
APP_PY = os.path.join(ROOT, "web", "app.py")
sys.path.insert(0, os.path.join(ROOT, "src"))

import db as dbm                     # noqa: E402
from features import FEATURE_DIM     # noqa: E402


def _load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


tagfile = _load("attune_tagfile_t", os.path.join(ROOT, "web", "tagfile.py"))

PNG = (b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00\x00\x00\x01\x00\x00\x00\x01"
       b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89" + b"\x00\x00\x00\x00IEND\xaeB`\x82")


@pytest.fixture
def files(tmp_path):
    out = {}
    for n in ("silence.mp3", "silence.flac"):
        dst = tmp_path / n
        shutil.copy(os.path.join(FIX, n), dst)
        out[n.split(".")[1]] = str(dst)
    return out


# ------------------------------------------------------------------ the file layer

@pytest.mark.parametrize("fmt", ["mp3", "flac"])
def test_every_field_round_trips(files, fmt):
    p = files[fmt]
    want = {k: f"{k} value" for k, _l, _k in tagfile.FIELDS}
    want.update({"tracknumber": "7", "discnumber": "2", "date": "1999", "bpm": "128",
                 "originaldate": "1998", "compilation": "1", "lyrics": "line one\nline two"})
    r = tagfile.write(p, want)
    got = r["tags"]
    for k, v in want.items():
        assert got[k] == v, (fmt, k, got[k])
    # read again from disk, not from the write's own answer
    again = tagfile.read(p)["tags"]
    assert again == got
    assert [f["key"] for f in r["fields"]] == [k for k, _l, _k in tagfile.FIELDS]


def test_a_comment_saves_on_an_mp3(files):
    r = tagfile.write(files["mp3"], {"comment": "worth keeping"})
    assert r["tags"]["comment"] == "worth keeping"
    import mutagen
    f = mutagen.File(files["mp3"])
    assert [c.text for c in f.tags.getall("COMM")] == [["worth keeping"]]


@pytest.mark.parametrize("fmt", ["mp3", "flac"])
def test_several_values_stay_several_and_empty_removes(files, fmt):
    import mutagen
    p = files[fmt]
    f = mutagen.File(p, easy=True)
    f.tags["genre"] = ["Punk", "Covers"]
    f.save()
    r = tagfile.read(p)
    g = next(x for x in r["fields"] if x["key"] == "genre")
    assert g["value"] == "Punk; Covers" and g["multi"] is True
    r = tagfile.write(p, {"genre": "Punk; Ska"})
    assert mutagen.File(p, easy=True).tags["genre"] == ["Punk", "Ska"]
    # a single value is never split, whatever it contains
    r = tagfile.write(p, {"artist": "Simon; Garfunkel"})
    assert mutagen.File(p, easy=True).tags["artist"] == ["Simon; Garfunkel"]
    r = tagfile.write(p, {"genre": ""})
    assert "genre" not in mutagen.File(p, easy=True).tags


@pytest.mark.parametrize("fmt,stars,expect", [
    ("mp3", 1, 1), ("mp3", 3, 128), ("mp3", 5, 255), ("flac", 4, "80"), ("flac", 2, "40")])
def test_a_rating_is_written_the_way_other_players_read_it(files, fmt, stars, expect):
    import mutagen
    p = files[fmt]
    tagfile.write_rating(p, stars)
    assert tagfile.read_rating(p) == stars
    f = mutagen.File(p)
    if fmt == "mp3":
        frames = f.tags.getall("POPM")
        assert [(x.email, x.rating) for x in frames] == [(tagfile.POPM_EMAIL, expect)]
    else:
        assert f.tags["rating"] == [expect]
        assert f.tags["fmps_rating"] == [f"{stars / 5:.1f}"]
    tagfile.write_rating(p, 0)
    assert tagfile.read_rating(p) == 0
    f = mutagen.File(p)
    assert not (f.tags.getall("POPM") if fmt == "mp3" else ("rating" in f.tags))


def test_another_players_popm_frame_is_updated_not_duplicated(files):
    import mutagen
    from mutagen.id3 import POPM
    p = files["mp3"]
    f = mutagen.File(p)
    f.tags.add(POPM(email="MusicBee", rating=255, count=3))
    f.save()
    assert tagfile.read_rating(p) == 5
    tagfile.write_rating(p, 2)
    frames = sorted((x.email, x.rating, x.count) for x in mutagen.File(p).tags.getall("POPM"))
    assert frames == [("MusicBee", 64, 3), (tagfile.POPM_EMAIL, 64, 0)]


def test_stars_from_the_bytes_and_scales_other_players_use():
    assert [tagfile.stars_from_byte(b) for b in (0, 1, 13, 64, 118, 128, 186, 196, 242, 255)] == \
        [0, 1, 1, 2, 3, 3, 4, 4, 5, 5]
    assert [tagfile.stars_from_vorbis(v) for v in ("100", "80", "20", "5", "3", "0.6", "1", "0")] == \
        [5, 4, 1, 5, 3, 3, 1, 0]


@pytest.mark.parametrize("fmt", ["mp3", "flac"])
def test_a_write_keeps_the_modified_time(files, fmt):
    p = files[fmt]
    old = time.time() - 86400 * 30
    os.utime(p, (old, old))
    tagfile.write(p, {"title": "kept"})
    tagfile.write_rating(p, 3)
    assert abs(os.path.getmtime(p) - old) < 2


def test_an_id3v23_file_stays_v23(files):
    import mutagen
    p = files["mp3"]
    f = mutagen.File(p)
    f.tags.update_to_v23()
    f.save(v2_version=3)
    assert mutagen.File(p).tags.version[1] == 3
    tagfile.write(p, {"title": "still 2.3", "comment": "c", "date": "2001"})
    f = mutagen.File(p)
    assert f.tags.version[1] == 3
    assert tagfile.read(p)["tag_format"] == "ID3v2.3"


@pytest.mark.parametrize("fmt", ["mp3", "flac"])
def test_a_cover_is_written_read_and_removed(files, fmt):
    p = files[fmt]
    assert tagfile.read_cover(p) is None
    tagfile.write_cover(p, PNG)
    data, mime = tagfile.read_cover(p)
    assert data == PNG and mime == "image/png"
    assert tagfile.read(p)["cover"] == {"mime": "image/png", "bytes": len(PNG)}
    with pytest.raises(ValueError):
        tagfile.write_cover(p, b"not an image")
    tagfile.write_cover(p, b"")
    assert tagfile.read_cover(p) is None


# ------------------------------------------------------------------ through the server

@pytest.fixture(scope="module")
def client(tmp_path_factory):
    if not os.path.exists(APP_PY):
        pytest.skip(f"web/app.py not found at {APP_PY}")
    mp = pytest.MonkeyPatch()
    tmp = tmp_path_factory.mktemp("tags-routes")
    cfg_home = tmp / "config"
    cfg_home.mkdir()
    empty_env = tmp / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    mp.setenv("APPDATA", str(cfg_home))
    mp.setenv("XDG_CONFIG_HOME", str(cfg_home))
    mp.setenv("ATTUNE_ENV", str(empty_env))
    lib = tmp / "library"
    lib.mkdir()
    paths = []
    for n in ("silence.mp3", "silence.flac"):
        dst = lib / n
        shutil.copy(os.path.join(FIX, n), dst)
        paths.append(str(dst))
    dbp = str(lib / "scratch.db")
    conn = dbm.connect(dbp)
    rng = np.random.default_rng(1)
    now = int(time.time())
    for k, path in enumerate(paths):
        v = rng.normal(size=512); v = v / np.linalg.norm(v)
        conn.execute(
            "INSERT INTO tracks(path, artist, album, title, genre, year, seconds, bytes, mtime) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (path, f"Artist {k}", "Album", f"Title {k}", "Rock", 2000, 1, 1, now))
        conn.execute(
            "INSERT INTO features(path, vec, dim, error, src_mtime, tempo) VALUES(?,?,?,?,?,?)",
            (path, rng.normal(size=FEATURE_DIM).astype(np.float32).tobytes(), FEATURE_DIM,
             None, now, 120.0))
        conn.execute("INSERT INTO clap(path, vec, dim) VALUES(?,?,?)",
                     (path, v.astype(np.float32).tobytes(), 512))
    conn.commit()
    conn.close()
    web_dir = os.path.dirname(APP_PY)
    added = web_dir not in sys.path
    if added:
        sys.path.insert(0, web_dir)
    try:
        appmod = _load("attune_app_tags", APP_PY)
        flask_app = appmod.create_app(dbp, engine_name="v2", playlist_dir=str(tmp))
    except Exception as e:          # pragma: no cover - environment-dependent
        mp.undo()
        pytest.skip(f"the app could not be stood up: {type(e).__name__}: {e}")
    finally:
        if added and web_dir in sys.path:
            sys.path.remove(web_dir)
    flask_app.config["TESTING"] = True
    c = flask_app.test_client()
    c._paths = paths
    yield c
    mp.undo()


def test_tags_route_carries_the_field_table_and_the_file_facts(client):
    j = client.get("/api/track/tags?i=0").get_json()
    assert [f["key"] for f in j["fields"]] == [k for k, _l, _k in tagfile.FIELDS]
    assert j["tag_format"].startswith("ID3") and j["info"]["codec"] == "MP3"
    assert j["rating"] == 0 and j["file_rating"] == 0 and j["cover"] is None
    assert j["file"] == "silence.mp3" and j["bytes"] > 0 and j["modified"] > 0


def test_tags_route_writes_and_mirrors_the_library_row(client):
    r = client.post("/api/track/tags", json={"i": 1, "tags": {
        "title": "Storms", "artist": "Tom", "comment": "ok", "lyrics": "la", "date": "2013-05-01"}})
    assert r.status_code == 200, r.get_json()
    row = r.get_json()["row"]
    assert row["title"] == "Storms" and row["artist"] == "Tom" and row["year"] == 2013
    j = client.get("/api/track/tags?i=1").get_json()
    assert j["tags"]["comment"] == "ok" and j["tags"]["lyrics"] == "la"


def test_rating_route_writes_the_file_and_calls_plex(client):
    import mutagen
    calls = []
    ud = client.application.extensions.get("attune_userdata")
    assert ud is not None
    done = threading.Event()
    def fake(path, stars):
        calls.append((os.path.basename(path), stars)); done.set()
    old = ud.plex_rate
    ud.plex_rate = fake
    try:
        r = client.post("/api/track/rate", json={"i": 0, "rating": 4})
        assert r.status_code == 200
        j = r.get_json()
        assert j["rating"] == 4 and j["file"] == "written" and j["plex"] == "sending"
        assert done.wait(5)
        assert calls == [("silence.mp3", 4)]
    finally:
        ud.plex_rate = old
    f = mutagen.File(client._paths[0])
    assert [(x.email, x.rating) for x in f.tags.getall("POPM")] == [(tagfile.POPM_EMAIL, 196)]
    j = client.get("/api/track/tags?i=0").get_json()
    assert j["rating"] == 4 and j["file_rating"] == 4


def test_cover_routes(client):
    import io as _io
    assert client.get("/api/track/cover?i=1").status_code == 404
    r = client.post("/api/track/cover", data={"i": "1", "file": (_io.BytesIO(PNG), "c.png")},
                    content_type="multipart/form-data")
    assert r.status_code == 200, r.get_json()
    r = client.get("/api/track/cover?i=1")
    assert r.status_code == 200 and r.data == PNG and r.mimetype == "image/png"
    r = client.post("/api/track/cover", data={"i": "1", "file": (_io.BytesIO(b"nope"), "c.txt")},
                    content_type="multipart/form-data")
    assert r.status_code == 400
    r = client.delete("/api/track/cover", json={"i": 1})
    assert r.status_code == 200
    assert client.get("/api/track/cover?i=1").status_code == 404


def test_ratings_import_reads_the_files_into_the_library(client):
    # the library (and the file) say 2 stars; then another player writes 5 into the file
    import mutagen
    ud = client.application.extensions["attune_userdata"]
    old = ud.plex_rate
    ud.plex_rate = None
    try:
        client.post("/api/track/rate", json={"i": 1, "rating": 2})
        f = mutagen.File(client._paths[1])
        f.tags["rating"] = ["100"]
        f.save()
        r = client.post("/api/ratings/import", json={})
        assert r.status_code == 200
        for _ in range(100):
            st = client.get("/api/ratings/import/status").get_json()
            if not st["running"]:
                break
            time.sleep(0.05)
        assert not st["running"] and st["done"] == 2 and st["with_rating"] >= 1 and st["changed"] >= 1
        j = client.get("/api/track/tags?i=1").get_json()
        assert j["rating"] == 5 and j["file_rating"] == 5
    finally:
        ud.plex_rate = old
