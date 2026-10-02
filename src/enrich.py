"""Catalog enrichment: what an outside tagger learned about each song, kept beside the analysis.

Three additive tables, outside db.py's SCHEMA_VERSION guard for the same reason as usermeta and
filestate: this is not analysis data, and a features rebuild must never wipe it.

  catalog_ids        path, recording_mbid, artist_mbids ("; " between several), original_year, style
  artist_similarity  artist_mbid, similar_mbid, source, score (0 to 1)
  genre_family       value, family: the broad family each genre or style value belongs to
                     ("Punk" and "Grunge" are both "Rock"). One row per value, not per song: it
                     is a fact about the vocabulary, so it holds for a song scanned tomorrow too.

Filled by tools/import_catalog.py from CSV files (GenreTagger's data/attune_export.csv,
data/artist_similarity.csv and data/genre_families.csv). catalog_ids has a second road: when a
tagger has written the same facts into the files as standard tags, the scan reads them from
there (scan.py import-folder --catalog-tags, upsert_from_tags below), so they survive a rescan
with no CSV step. Both roads give the same rows. The engine reads them itself at load
(hybrid._load_catalog; the app loads hybrid.py by file path, so hybrid imports nothing from here)
for:

  original_year   the era term uses the year the recording first came out, not the year of the
                  compilation or remaster the file sits on
  style           the genre term compares GENRE plus STYLE, so two songs that share a style get
                  partial credit instead of all-or-nothing on one GENRE value
  recording_mbid  a walk never takes the same recording twice (two files, two albums, one song)
  artist_mbids    the optional `artist` weight: how often listeners play the two artists together
  genre_family    the optional two-tier genre term (hybrid's family_credit switch, off by default):
                  partial credit for two songs in the same family whose tags differ

No table, no change: a library that never imported anything mixes exactly as before.
"""
from __future__ import annotations

import csv
import sqlite3

CATALOG_DDL = """CREATE TABLE IF NOT EXISTS catalog_ids (
    path           TEXT PRIMARY KEY,
    recording_mbid TEXT,
    artist_mbids   TEXT,
    original_year  INTEGER,
    style          TEXT
)"""
SIMILARITY_DDL = """CREATE TABLE IF NOT EXISTS artist_similarity (
    artist_mbid  TEXT NOT NULL,
    similar_mbid TEXT NOT NULL,
    source       TEXT NOT NULL,
    score        REAL NOT NULL,
    PRIMARY KEY (artist_mbid, similar_mbid, source)
)"""


FAMILY_DDL = """CREATE TABLE IF NOT EXISTS genre_family (
    value  TEXT PRIMARY KEY,
    family TEXT NOT NULL
)"""


def _has_table(conn, name) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone() is not None


def ensure_catalog_table(conn) -> None:
    conn.execute(CATALOG_DDL)


def upsert_from_tags(conn, path, tags) -> int:
    """One file's catalog tags (scan._catalog_tags) into catalog_ids. Field by field: a value
    the file carries replaces the stored one; a field the file does not carry keeps what an
    import put there. A file with none of them writes nothing. Returns 1 if a row was written."""
    row = {k: tags.get(k) or None for k in ("recording_mbid", "artist_mbids", "original_year", "style")}
    if not any(v is not None for v in row.values()):
        return 0
    row["path"] = path
    conn.execute(
        """INSERT INTO catalog_ids(path, recording_mbid, artist_mbids, original_year, style)
           VALUES(:path, :recording_mbid, :artist_mbids, :original_year, :style)
           ON CONFLICT(path) DO UPDATE SET
             recording_mbid=COALESCE(excluded.recording_mbid, catalog_ids.recording_mbid),
             artist_mbids=COALESCE(excluded.artist_mbids, catalog_ids.artist_mbids),
             original_year=COALESCE(excluded.original_year, catalog_ids.original_year),
             style=COALESCE(excluded.style, catalog_ids.style)""", row)
    return 1


def _int(v):
    s = str(v or "").strip()
    return int(s) if s.isdigit() else None


def import_csvs(conn, ids_csv=None, similarity_csv=None, families_csv=None) -> dict:
    """Replace each table's contents from the CSV file given for it. Returns row counts."""
    out = {}
    if families_csv:
        conn.execute(FAMILY_DDL)
        conn.execute("DELETE FROM genre_family")
        with open(families_csv, encoding="utf-8", newline="") as f:
            rows = [(r["value"].strip(), r["family"].strip()) for r in csv.DictReader(f)
                    if (r.get("value") or "").strip() and (r.get("family") or "").strip()]
        conn.executemany("INSERT OR REPLACE INTO genre_family VALUES (?,?)", rows)
        out["genre_family"] = len(rows)
    if ids_csv:
        conn.execute(CATALOG_DDL)
        conn.execute("DELETE FROM catalog_ids")
        with open(ids_csv, encoding="utf-8", newline="") as f:
            rows = [(r["path"], r.get("recording_mbid") or None, r.get("artist_mbids") or None,
                     _int(r.get("original_year")), r.get("style") or None)
                    for r in csv.DictReader(f) if r.get("path")]
        conn.executemany("INSERT OR REPLACE INTO catalog_ids VALUES (?,?,?,?,?)", rows)
        out["catalog_ids"] = len(rows)
    if similarity_csv:
        conn.execute(SIMILARITY_DDL)
        conn.execute("DELETE FROM artist_similarity")
        with open(similarity_csv, encoding="utf-8", newline="") as f:
            rows = [(r["artist_mbid"], r["similar_mbid"], r["source"], float(r["score"]))
                    for r in csv.DictReader(f) if r.get("artist_mbid") and r.get("similar_mbid")]
        conn.executemany("INSERT OR REPLACE INTO artist_similarity VALUES (?,?,?,?)", rows)
        out["artist_similarity"] = len(rows)
    conn.commit()
    return out


def import_main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Load catalog ids and artist similarity into an Attune database")
    ap.add_argument("--db", required=True)
    ap.add_argument("--ids", help="CSV: path, recording_mbid, artist_mbids, original_year, style")
    ap.add_argument("--similar", help="CSV: artist_mbid, similar_mbid, source, score")
    ap.add_argument("--families", help="CSV: value, family (the broad family of each genre or style value)")
    ap.add_argument("--no-backup", action="store_true", help="skip the copy taken before writing (scratch copies only)")
    a = ap.parse_args(argv)
    conn = sqlite3.connect(a.db, timeout=30)
    conn.execute("PRAGMA busy_timeout=30000")
    if not a.no_backup:
        # The library database is production data: a consistent copy first, through sqlite's own
        # backup API so a running app's WAL is included, then a check that it opens and matches.
        import datetime, os
        dest = os.path.splitext(a.db)[0] + datetime.datetime.now().strftime(".backup-%Y%m%d-%H%M%S-catalog.db")
        with sqlite3.connect(dest) as b:
            conn.backup(b)
            n_src = conn.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
            n_dst = b.execute("SELECT COUNT(*) FROM tracks").fetchone()[0]
        if n_src != n_dst:
            raise SystemExit(f"backup check failed: {n_dst} tracks in {dest}, {n_src} in {a.db}; nothing written")
        print(f"backup: {dest} ({os.path.getsize(dest)} bytes, {n_dst} tracks)")
    print(import_csvs(conn, a.ids, a.similar, a.families))
    conn.close()
    return 0
