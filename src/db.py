"""SQLite storage for the modern mixer. One file, no server.

tracks:   catalog (path is the primary key; mirrors MusicIP's file paths)
features: acoustic descriptor blob + tempo, keyed by path
Meta table records feature schema version so a dimension change invalidates cleanly.
"""
from __future__ import annotations
import sqlite3, os, numpy as np
from features import FEATURE_DIM

SCHEMA_VERSION = 1

DDL = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS tracks (
    path      TEXT PRIMARY KEY,
    artist    TEXT, album TEXT, title TEXT, genre TEXT,
    year      INTEGER, seconds INTEGER, bytes INTEGER, mtime INTEGER
);
CREATE TABLE IF NOT EXISTS features (
    path        TEXT PRIMARY KEY,
    dim         INTEGER,
    vec         BLOB,          -- float32 little-endian, length dim
    tempo       REAL,
    analyzed_at INTEGER,
    error       TEXT,          -- non-null if analysis failed (so we don't retry forever)
    src_mtime   INTEGER,       -- source file mtime when analyzed; change => re-analyze
    FOREIGN KEY(path) REFERENCES tracks(path)
);
CREATE INDEX IF NOT EXISTS idx_tracks_artist ON tracks(artist);
CREATE INDEX IF NOT EXISTS idx_tracks_genre  ON tracks(genre);
"""


def connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.executescript(DDL)

    def _meta_get(k):
        r = conn.execute("SELECT value FROM meta WHERE key=?", (k,)).fetchone()
        return r[0] if r else None

    # additive migration for DBs created before src_mtime existed (safe: adds a
    # nullable column, no data loss, no SCHEMA_VERSION bump / rebuild needed).
    cols = {r[1] for r in conn.execute("PRAGMA table_info(features)")}
    if "src_mtime" not in cols:
        conn.execute("ALTER TABLE features ADD COLUMN src_mtime INTEGER")
        conn.commit()
    # One-time backfill (idempotent via meta flag; self-heals a DB whose column was added
    # in an earlier run before this backfill existed). Baselines already-analyzed rows to
    # the current tracks.mtime — otherwise they stay src_mtime=NULL forever and a later
    # change to those files is never re-detected (the stale-vector bug this fixes). Error
    # rows (vec NULL) intentionally stay NULL. Assumes current vectors match current files,
    # the best we can do since the old schema didn't record it.
    if _meta_get("src_mtime_backfilled") is None:
        conn.execute("""UPDATE features
                           SET src_mtime = (SELECT t.mtime FROM tracks t WHERE t.path = features.path)
                         WHERE src_mtime IS NULL AND vec IS NOT NULL""")
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('src_mtime_backfilled','1')")
        conn.commit()

    sv = _meta_get("schema_version")
    if sv is None:
        # fresh DB: stamp version + feature dim
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('schema_version',?)", (str(SCHEMA_VERSION),))
        conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feature_dim',?)", (str(FEATURE_DIM),))
        conn.commit()
    else:
        # existing DB: VALIDATE — the docs promise a schema/dim bump invalidates old
        # data. Refuse a mismatch (fail-closed) instead of silently mixing dims.
        fd = _meta_get("feature_dim")
        if str(sv) != str(SCHEMA_VERSION) or (fd is not None and str(fd) != str(FEATURE_DIM)):
            conn.close()
            raise SystemExit(
                f"DB {db_path} is schema v{sv}/dim {fd}, but this code expects "
                f"v{SCHEMA_VERSION}/dim {FEATURE_DIM}. Delete the DB (or its features "
                f"table) and re-run scan to rebuild.")
        if fd is None:   # older DB predating the dim stamp
            # don't blindly stamp the current dim over vectors of a different size —
            # verify first, else they'd pass validation and blow up later in the engine.
            bad = conn.execute(
                "SELECT dim FROM features WHERE vec IS NOT NULL AND dim IS NOT NULL "
                "AND dim != ? LIMIT 1", (FEATURE_DIM,)).fetchone()
            if bad is not None:
                conn.close()
                raise SystemExit(
                    f"DB {db_path} holds feature vectors of dim {bad[0]} but this code "
                    f"expects {FEATURE_DIM}. Delete the features table and re-run scan.")
            conn.execute("INSERT OR REPLACE INTO meta(key,value) VALUES('feature_dim',?)", (str(FEATURE_DIM),))
            conn.commit()
    return conn


def upsert_track(conn, rec: dict):
    conn.execute(
        """INSERT INTO tracks(path,artist,album,title,genre,year,seconds,bytes,mtime)
           VALUES(:path,:artist,:album,:title,:genre,:year,:seconds,:bytes,:mtime)
           ON CONFLICT(path) DO UPDATE SET
             artist=excluded.artist, album=excluded.album, title=excluded.title,
             genre=excluded.genre, year=excluded.year, seconds=excluded.seconds,
             bytes=excluded.bytes, mtime=excluded.mtime""",
        {k: rec.get(k) for k in
         ("path", "artist", "album", "title", "genre", "year", "seconds", "bytes", "mtime")},
    )


def save_features(conn, path: str, vec, tempo, analyzed_at, error=None, src_mtime=None):
    blob = None if vec is None else np.asarray(vec, dtype=np.float32).tobytes()
    dim = None if vec is None else int(np.asarray(vec).shape[0])
    conn.execute(
        """INSERT INTO features(path,dim,vec,tempo,analyzed_at,error,src_mtime)
           VALUES(?,?,?,?,?,?,?)
           ON CONFLICT(path) DO UPDATE SET
             dim=excluded.dim, vec=excluded.vec, tempo=excluded.tempo,
             analyzed_at=excluded.analyzed_at, error=excluded.error,
             src_mtime=excluded.src_mtime""",
        (path, dim, blob, tempo, analyzed_at, error, src_mtime),
    )


def load_matrix(conn, only_analyzed=True):
    """Return (paths list, float32 matrix [N,dim], meta dict path->row)."""
    q = """SELECT t.path,t.artist,t.album,t.title,t.genre,t.year,t.seconds,
                  f.vec,f.dim,f.tempo
           FROM tracks t JOIN features f ON f.path=t.path
           WHERE f.vec IS NOT NULL"""
    paths, vecs, meta = [], [], {}
    for row in conn.execute(q):
        path, artist, album, title, genre, year, seconds, blob, dim, tempo = row
        v = np.frombuffer(blob, dtype=np.float32)
        if dim and v.shape[0] != dim:
            continue
        paths.append(path)
        vecs.append(v)
        meta[path] = {"artist": artist, "album": album, "title": title,
                      "genre": genre, "year": year, "seconds": seconds, "tempo": tempo}
    mat = np.vstack(vecs) if vecs else np.zeros((0, FEATURE_DIM), np.float32)
    return paths, mat, meta


def analyzed_paths(conn) -> set:
    return {r[0] for r in conn.execute("SELECT path FROM features WHERE vec IS NOT NULL OR error IS NOT NULL")}


def up_to_date_paths(conn) -> set:
    """Paths already analyzed AND unchanged since: features.src_mtime matches the
    current tracks.mtime. Rows predating mtime tracking (src_mtime IS NULL) or tracks
    with no known mtime are grandfathered in (not re-analyzed). A file that changed —
    re-imported so tracks.mtime advanced past features.src_mtime — drops out of this
    set and gets re-analyzed, fixing the old 'stale vector kept forever' bug."""
    q = """SELECT f.path FROM features f JOIN tracks t ON t.path = f.path
           WHERE (f.vec IS NOT NULL OR f.error IS NOT NULL)
             AND (f.src_mtime IS NULL OR t.mtime IS NULL OR f.src_mtime = t.mtime)"""
    return {r[0] for r in conn.execute(q)}


def stats(conn) -> dict:
    t = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
    ok = conn.execute("SELECT COUNT(*) FROM features WHERE vec IS NOT NULL").fetchone()[0]
    err = conn.execute("SELECT COUNT(*) FROM features WHERE error IS NOT NULL").fetchone()[0]
    return {"tracks": t, "analyzed": ok, "errors": err}
