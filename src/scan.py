"""Scanner: populate tracks from a music folder (standalone) or a MusicIP library
dump, then analyze audio into feature vectors. Incremental + parallel + resumable.

Usage:
  python scan.py import-folder <music_dir>             # walk a folder, read tags (ffprobe)
  python scan.py import-catalog <library.json>        # load MusicIP metadata dump
  python scan.py analyze [--limit N] [--workers K] [--paths-file F]
  python scan.py stats
"""
from __future__ import annotations
import sys, os, json, time, argparse, subprocess, concurrent.futures as cf
import db as dbm
import features as feat

DB_DEFAULT = os.path.join(os.path.dirname(__file__), "..", "data", "mixer.db")
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".wav"}


def _ffprobe_tags(path):
    """Read basic tags + duration via ffprobe (shelled out, not linked -> no GPL
    entanglement for this codebase). Returns a dict or minimal fallback."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_entries", "format=duration:format_tags",
             path],
            capture_output=True, text=True, timeout=30,
        )
        j = json.loads(out.stdout or "{}")
        fmt = j.get("format", {})
        tags = {k.lower(): v for k, v in (fmt.get("tags", {}) or {}).items()}
        dur = fmt.get("duration")
        year = tags.get("date") or tags.get("year") or ""
        year = "".join(ch for ch in str(year)[:4] if ch.isdigit()) or None
        return {
            "artist": tags.get("artist") or tags.get("album_artist"),
            "album": tags.get("album"),
            "title": tags.get("title") or os.path.splitext(os.path.basename(path))[0],
            "genre": tags.get("genre"),
            "year": int(year) if year else None,
            "seconds": int(float(dur)) if dur else None,
        }
    except Exception:
        return {"title": os.path.splitext(os.path.basename(path))[0]}


def import_folder(db_path, music_dir, workers=8):
    conn = dbm.connect(db_path)
    files = []
    for root, _dirs, names in os.walk(music_dir):
        for n in names:
            if os.path.splitext(n)[1].lower() in AUDIO_EXTS:
                files.append(os.path.abspath(os.path.join(root, n)))
    print(f"found {len(files)} audio files under {music_dir}")
    n = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for path, tags in zip(files, ex.map(_ffprobe_tags, files)):
            try:
                st = os.stat(path)
                mtime, size = int(st.st_mtime), st.st_size
            except OSError:
                mtime, size = None, None
            rec = {"path": path, "bytes": size, "mtime": mtime}
            rec.update(tags)
            dbm.upsert_track(conn, rec)
            n += 1
            if n % 500 == 0:
                conn.commit(); print(f"  {n}/{len(files)} tagged", flush=True)
    conn.commit()
    print(f"imported {n} tracks -> {os.path.abspath(db_path)}")
    print("stats:", dbm.stats(conn))


def import_catalog(db_path, library_json):
    conn = dbm.connect(db_path)
    lib = json.load(open(library_json, encoding="utf-8"))
    n = 0
    for r in lib:
        f = r.get("file")
        if not f:
            continue
        dbm.upsert_track(conn, {
            "path": f,
            "artist": r.get("artist"), "album": r.get("album"),
            "title": r.get("name"), "genre": r.get("genre"),
            "year": r.get("year"), "seconds": r.get("seconds"),
            "bytes": r.get("bytes"), "mtime": r.get("modified"),
        })
        n += 1
        if n % 2000 == 0:
            conn.commit()
    conn.commit()
    print(f"imported {n} tracks -> {os.path.abspath(db_path)}")
    print("stats:", dbm.stats(conn))


def _analyze_one(path):
    t0 = time.time()
    r = feat.extract(path)
    dt = time.time() - t0
    if r and "vec" in r:
        return (path, r["vec"], r["tempo"], None, dt)
    err = (r or {}).get("error", "unknown")
    return (path, None, None, err, dt)


def analyze(db_path, limit=None, workers=4, paths_file=None):
    conn = dbm.connect(db_path)
    done = dbm.up_to_date_paths(conn)   # skip only unchanged, already-analyzed tracks
    mtimes = {r[0]: r[1] for r in conn.execute("SELECT path, mtime FROM tracks")}
    if paths_file:
        want = [l.strip() for l in open(paths_file, encoding="utf-8") if l.strip()]
    else:
        want = [r[0] for r in conn.execute("SELECT path FROM tracks")]
    todo = [p for p in want if p not in done]
    if limit:
        todo = todo[:limit]
    print(f"{len(want)} candidates, {len(done)} already done, {len(todo)} to analyze, workers={workers}")
    if not todo:
        print("nothing to do"); return

    t0 = time.time()
    n_ok = n_err = 0
    # I/O + numba release the GIL enough that threads give real speedup and avoid
    # re-importing librosa per task (process pool would be far slower to spin up).
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_analyze_one, p): p for p in todo}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            path, vec, tempo, err, dt = fut.result()
            dbm.save_features(conn, path, vec, tempo, int(time.time()), err,
                              src_mtime=mtimes.get(path))
            if err:
                n_err += 1
            else:
                n_ok += 1
            if i % 25 == 0 or i == len(todo):
                conn.commit()
                rate = i / (time.time() - t0)
                eta = (len(todo) - i) / rate if rate else 0
                print(f"  {i}/{len(todo)} ok={n_ok} err={n_err} "
                      f"{rate:.2f}/s eta={eta/60:.1f}m last={dt:.1f}s", flush=True)
    conn.commit()
    print(f"DONE ok={n_ok} err={n_err} in {(time.time()-t0)/60:.1f}m")
    print("stats:", dbm.stats(conn))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("cmd", choices=["import-folder", "import-catalog", "analyze", "stats"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--db", default=DB_DEFAULT)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--paths-file")
    a = ap.parse_args()
    if a.cmd == "import-folder":
        import_folder(a.db, a.arg)
    elif a.cmd == "import-catalog":
        import_catalog(a.db, a.arg)
    elif a.cmd == "analyze":
        analyze(a.db, a.limit, a.workers, a.paths_file)
    elif a.cmd == "stats":
        print(dbm.stats(dbm.connect(a.db)))


if __name__ == "__main__":
    main()
