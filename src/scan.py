"""Scanner: populate tracks from a music folder (standalone) or a MusicIP library
dump, then analyze audio into feature vectors. Incremental + parallel + resumable.

Usage:
  python scan.py import-folder <music_dir>             # walk a folder, read tags (mutagen)
  python scan.py import-catalog <library.json>        # load MusicIP metadata dump
  python scan.py analyze [--limit N] [--workers K] [--paths-file F]
  python scan.py stats
"""
from __future__ import annotations
import sys, os, json, time, shutil, argparse, subprocess, sqlite3
import concurrent.futures as cf
import db as dbm
import features as feat

DB_DEFAULT = os.path.join(os.path.dirname(__file__), "..", "data", "mixer.db")
AUDIO_EXTS = {".mp3", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma", ".wav"}


# Tag names for the six fields we store, one row per field, listed across every tag
# dialect mutagen hands back. Lowercased at lookup time, so case never matters.
#
#   easy / Vorbis   mutagen.File(easy=True) for MP3 and MP4, and FLAC/Ogg natively,
#                   all expose the same lowercase words: artist, album, title, genre...
#   ID3 frames      containers with no Easy wrapper: RIFF WAV, AIFF.
#   ASF             Windows Media (.wma).
#   MP4 atoms       belt and braces; EasyMP4 normally covers these.
_TAG_KEYS = {
    "artist":      ("artist", "TPE1", "Author", "\xa9ART"),
    "albumartist": ("albumartist", "album_artist", "album artist", "TPE2",
                    "WM/AlbumArtist", "aART"),
    "album":       ("album", "TALB", "WM/AlbumTitle", "\xa9alb"),
    "title":       ("title", "TIT2", "\xa9nam"),
    "genre":       ("genre", "TCON", "WM/Genre", "\xa9gen"),
    "date":        ("date", "year", "originaldate", "TDRC", "TDRL", "TYER", "TDOR",
                    "WM/Year", "\xa9day"),
}

# RIFF INFO, the metadata a .wav actually carries. mutagen's WAVE class reads only ID3,
# and almost nothing writes ID3 into a wav: ffmpeg, Audacity and Windows all write an
# INFO list instead. Observed 2026-09-20 on an ffmpeg-written wav -- mutagen returned
# tags = None for a file ffprobe read five fields out of. Without this, every wav a
# stranger imports arrives with nothing but its file name.
_RIFF_INFO_KEYS = {
    b"IART": "artist", b"INAM": "title", b"IPRD": "album", b"IALB": "album",
    b"IGNR": "genre", b"ICRD": "date", b"IYER": "date",
}
_RIFF_MAX_LIST = 1 << 20      # never read more than 1 MiB of INFO


def _riff_info_tags(path):
    """The RIFF INFO chunk of a .wav as {field: value}, or {} if there is none.

    Deliberately tiny and read-only: 12-byte RIFF header, then walk top-level chunks
    looking for LIST/INFO and read its sub-chunks. Sizes are little-endian and every
    chunk is padded to an even length. Anything unexpected ends the walk rather than
    raising -- a malformed wav must import with a file name, not crash the scan."""
    out = {}
    try:
        with open(path, "rb") as fh:
            if fh.read(4) != b"RIFF":
                return out
            fh.read(4)                                   # total size, not needed
            if fh.read(4) != b"WAVE":
                return out
            while True:
                head = fh.read(8)
                if len(head) < 8:
                    break
                cid, size = head[:4], int.from_bytes(head[4:8], "little")
                if cid == b"LIST" and size <= _RIFF_MAX_LIST:
                    body = fh.read(size)
                    if body[:4] == b"INFO":
                        off = 4
                        while off + 8 <= len(body):
                            sid = body[off:off + 4]
                            slen = int.from_bytes(body[off + 4:off + 8], "little")
                            off += 8
                            val = body[off:off + slen]
                            off += slen + (slen % 2)
                            name = _RIFF_INFO_KEYS.get(sid)
                            if name and name not in out:
                                raw = val.split(b"\x00", 1)[0]
                                try:
                                    text = raw.decode("utf-8").strip()
                                except UnicodeDecodeError:
                                    # Windows tools write INFO in the local codepage.
                                    text = raw.decode("cp1252", "replace").strip()
                                if text:
                                    out[name] = text
                else:
                    fh.seek(size + (size % 2), os.SEEK_CUR)
                    continue
                if size % 2:
                    fh.read(1)
    except OSError:
        return {}
    return out


def _tag_values(raw):
    """Flatten one tag entry to a list of non-empty strings.

    mutagen hands back a different shape per dialect: a plain list of str for Vorbis
    and the Easy wrappers, an ID3 frame object carrying `.text` (whose items may be
    ID3TimeStamp, not str), and ASFUnicodeAttribute objects carrying `.value`. The
    audioinfo.py lesson applies here too: read the value, never trust the repr."""
    if raw is None:
        return []
    genres = getattr(raw, "genres", None)       # ID3 TCON: resolves "(17)" to "Rock"
    if genres:
        raw = genres
    elif hasattr(raw, "text"):                  # any other ID3 text frame
        raw = raw.text
    items = raw if isinstance(raw, (list, tuple)) else [raw]
    out = []
    for it in items:
        it = getattr(it, "value", it)           # ASF attribute
        s = str(it).strip()
        # ASF stores the same field more than once (ffmpeg writes Author twice), which
        # produced "Name;Name" before this. Order is kept; only repeats are dropped.
        if s and s not in out:
            out.append(s)
    return out


def _mutagen_tags(path):
    """Read the six stored fields with mutagen. Returns a dict, or None if mutagen
    cannot open the file at all (then the caller may try ffprobe).

    Replaces a shell-out to ffprobe, which cost 141 MB of bundled ffprobe.exe for this
    one job while mutagen was already inside both halves of the app (web/audioinfo.py,
    web/userdata.py, web/studio.py). Operator ruling B5, 2026-08-04.

    The contract is ffprobe's, kept field for field so a library imported before this
    change and one imported after hold the same rows:
      artist   tag artist, else album artist
      album    tag album
      title    tag title, else the file name without its extension
      genre    tag genre
      year     the first four digits of date or year, int, else None
      seconds  whole seconds, truncated (not rounded), else None
    Multi-value tags are joined with ";", which is what ffmpeg wrote for the rows
    already in the database."""
    try:
        import mutagen
    except ImportError:
        _warn_no_mutagen()
        return None
    try:
        f = mutagen.File(path, easy=True)
    except Exception:
        f = None
    if f is None:
        try:
            f = mutagen.File(path)              # no Easy wrapper for this container
        except Exception:
            return None
    if f is None:
        return None

    tags = f.tags or {}
    # One lowercase name can map to more than one real key (an ASF file carries both
    # `Title` and `title`), so keep every candidate in the order the container lists
    # them and take the first that actually holds a value.
    lower = {}
    try:
        for k in tags.keys():
            lower.setdefault(str(k).lower(), []).append(k)
    except Exception:
        lower = {}

    def field(name):
        for want in _TAG_KEYS[name]:
            for key in lower.get(want.lower(), ()):
                try:
                    vals = _tag_values(tags[key])
                except Exception:
                    continue
                if vals:
                    return ";".join(vals)
        return None

    got = {n: field(n) for n in _TAG_KEYS}
    # A wav whose metadata is in the RIFF INFO list, which mutagen does not read. Only
    # fills the gaps: an ID3 tag inside a wav, if there is one, still wins.
    if os.path.splitext(path)[1].lower() == ".wav" and not all(
            got[n] for n in ("artist", "album", "title", "genre", "date")):
        for n, v in _riff_info_tags(path).items():
            if not got.get(n):
                got[n] = v

    year = "".join(ch for ch in str(got["date"] or "")[:4] if ch.isdigit())
    dur = getattr(getattr(f, "info", None), "length", None)
    return {
        "artist": got["artist"] or got["albumartist"],
        "album": got["album"],
        "title": got["title"] or os.path.splitext(os.path.basename(path))[0],
        "genre": got["genre"],
        "year": int(year) if len(year) == 4 else None,
        "seconds": int(float(dur)) if dur else None,
    }


_WARNED_NO_MUTAGEN = False


def _warn_no_mutagen():
    """Say it once, loudly, if mutagen is missing from the interpreter running the scan.

    This is not hypothetical. When the user configures an ML python in Preferences,
    web/scanjob.py runs THIS script under it (scanjob.py:165-175), and mutagen is not a
    core dependency of the project. On this machine, mixer-ng\\.venv-ml has no mutagen
    (checked 2026-09-20). Without this line every track would import with nothing but a
    file name and the run would still report success."""
    global _WARNED_NO_MUTAGEN
    if _WARNED_NO_MUTAGEN:
        return
    _WARNED_NO_MUTAGEN = True
    print(f"WARNING: mutagen is not installed in {sys.executable}", file=sys.stderr)
    print("WARNING: tags cannot be read from this Python. Fix it with:", file=sys.stderr)
    print(f'WARNING:   "{sys.executable}" -m pip install mutagen', file=sys.stderr)
    sys.stderr.flush()
    print("WARNING: mutagen is missing - see stderr; tags will fall back to ffprobe "
          "or to file names", flush=True)


def _read_tags(path):
    """The import-folder tag reader. mutagen first; ffprobe only if mutagen could not
    open the file AND an ffprobe happens to be on PATH (a system install, or one the
    user dropped in their own Attune bin folder). Neither available -> the file name.

    A .wav gets one more try before giving up: mutagen can fail to open a malformed or
    truncated wav whose RIFF INFO chunk is still perfectly readable."""
    t = _mutagen_tags(path)
    if t is not None:
        return t
    fallback = {"title": os.path.splitext(os.path.basename(path))[0]}
    if os.path.splitext(path)[1].lower() == ".wav":
        info = _riff_info_tags(path)
        if info:
            year = "".join(ch for ch in str(info.get("date") or "")[:4] if ch.isdigit())
            return {
                "artist": info.get("artist"),
                "album": info.get("album"),
                "title": info.get("title") or fallback["title"],
                "genre": info.get("genre"),
                "year": int(year) if len(year) == 4 else None,
                "seconds": None,
            }
    if shutil.which("ffprobe"):
        return _ffprobe_tags(path)
    return fallback


def _ffprobe_tags(path):
    """Read basic tags + duration via ffprobe (shelled out, not linked -> no GPL
    entanglement for this codebase). Returns a dict or minimal fallback.

    No longer the default reader: kept as the fallback for a container mutagen cannot
    open, on a machine that has an ffprobe. The release build ships neither ffmpeg nor
    ffprobe (282 MB), so on most machines this never runs.

    ffprobe prints its JSON as UTF-8. `text=True` alone made Python decode it in the
    console codepage (cp1252 here), which RAISED on any tag holding a character cp1252
    does not have -- and this function swallows every exception, so the track imported
    with no artist, no album, no genre, no year and no length at all. Measured on a
    238-file sample of the operator's library, 2026-09-20: 9 files (3.8%) came back
    with nothing but a file name; the encoding fix recovers 5 of them, the other 4 are
    files ffprobe genuinely cannot parse. Same bug, and the same fix, as the stdout
    reconfigure in desktop/worker_entry.py."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams",
             "-show_entries", "format=duration:format_tags:stream_tags",
             path],
            capture_output=True, text=True, timeout=30,
            encoding="utf-8", errors="replace",
        )
        j = json.loads(out.stdout or "{}")
        fmt = j.get("format", {})
        # Ogg Vorbis and Opus carry their comments on the STREAM, not the container, so
        # a format-only probe returned NOTHING for them -- every .ogg and .opus row
        # imported by the old reader holds no tags at all. Format tags still win.
        tags = {}
        for st in (j.get("streams") or []):
            tags.update({k.lower(): v for k, v in (st.get("tags") or {}).items()})
        tags.update({k.lower(): v for k, v in (fmt.get("tags", {}) or {}).items()})
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


def import_folder(db_path, music_dir, workers=8, exclude=()):
    conn = dbm.connect(db_path)
    # exclude: folder prefixes never imported (settings `exclude_folders`, ruling
    # B2 2026-08-04). Born from _SYNCAPP\Versioning: a sync tool's version-history
    # folder seeded 199 dead rows the pool could never use. Pruned during the walk
    # so an excluded tree is never even listed.
    ex_norm = [os.path.normcase(os.path.abspath(e)).rstrip("\\/")
               for e in (exclude or ()) if str(e).strip()]

    def _excluded(d):
        dn = os.path.normcase(os.path.abspath(d))
        return any(dn == e or dn.startswith(e + os.sep) for e in ex_norm)

    files = []
    for root, _dirs, names in os.walk(music_dir):
        if _excluded(root):
            _dirs[:] = []
            continue
        _dirs[:] = [d for d in _dirs if not _excluded(os.path.join(root, d))]
        for n in names:
            if os.path.splitext(n)[1].lower() in AUDIO_EXTS:
                files.append(os.path.abspath(os.path.join(root, n)))
    print(f"found {len(files)} audio files under {music_dir}")
    n = 0
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for path, tags in zip(files, ex.map(_read_tags, files)):
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


def _read_path(path, read_map):
    """Map a stored (canonical) path to a faster local mirror to READ audio from, if that
    mirror copy exists. The DB key stays the original path; only the bytes are read locally.
    read_map is a list of (from_prefix, to_prefix) pairs, e.g.
    (r'\\\\NAS\\music', r'D:\\Music') so analysis runs off a local disk, not over the LAN."""
    for a, b in (read_map or []):
        if a and path.startswith(a):
            cand = b + path[len(a):]
            if os.path.exists(cand):
                return cand
            break
    return path


def _analyze_one(path, read_path=None):
    t0 = time.time()
    r = feat.extract(read_path or path)
    dt = time.time() - t0
    if r and "vec" in r:
        return (path, r["vec"], r["tempo"], None, dt, bool(r.get("redecoded")))
    err = (r or {}).get("error", "unknown")
    return (path, None, None, err, dt, False)


def appdata_bin():
    r"""%APPDATA%\Attune\bin - where a user may drop their own ffmpeg.exe. Returns the
    folder if it exists, else None. Attune does not create it and does not police what
    is in it; it is simply looked at."""
    base = os.environ.get("APPDATA")
    if not base:
        return None
    d = os.path.join(base, "Attune", "bin")
    return d if os.path.isdir(d) else None


def add_user_bin_to_path():
    r"""Put %APPDATA%\Attune\bin at the FRONT of PATH for this process.

    desktop/worker_entry.py does the same thing for the frozen analyzer, but that is
    not the only way analysis runs: when the user has configured an ML python,
    web/scanjob.py launches THIS script directly under it (scanjob.py:165-175) and
    worker_entry is never involved. Doing it here as well means a user-supplied ffmpeg
    is found on every route into analysis, not just the frozen one."""
    d = appdata_bin()
    if d:
        cur = os.environ.get("PATH", "")
        if d.lower() not in cur.lower().split(os.pathsep):
            os.environ["PATH"] = d + os.pathsep + cur
    return d


def _retry_paths(conn, retry_failed):
    """Failed rows worth another attempt on THIS run, as {path: why}. The reason travels
    with the path because the two kinds are retried for different reasons and printing one
    of them under the other's heading would make the run log say something untrue.

    A features row carrying an error counts as up to date (db.py up_to_date_paths), so
    without this a track that failed once is never looked at again until the file
    itself changes. That is right for a damaged file and wrong for the one case the
    user can actually fix: a format that needed a decoder the machine did not have.
    features.py marks those with the NEEDS_DECODER prefix, and they are retried by
    themselves the first time an ffmpeg is present. --retry-failed widens it to every
    failed row."""
    if retry_failed:
        q = "SELECT path FROM features WHERE error IS NOT NULL"
        return {r[0]: "--retry-failed" for r in conn.execute(q)}
    if not feat.decoder_available():
        return {}
    q = "SELECT path FROM features WHERE error LIKE ?"
    no_decoder = {r[0] for r in conn.execute(q, (feat.NEEDS_DECODER + "%",))}
    # Rows written before 2026-09-20 recorded a short decode as "too short" without ever
    # asking a second decoder, and on this library ffmpeg gets a whole song, or enough of
    # one to analyze, out of 14 of the 18 such rows.
    # Each gets exactly one more attempt now that there is a second decoder: the attempt
    # rewrites the row whichever way it goes, and no reason features.py writes today can
    # begin with either of these, so the match cannot pick the same row up twice.
    short = set()
    for prefix in feat.LEGACY_SHORT_ERROR_PREFIXES:
        short |= {r[0] for r in conn.execute(q, (prefix + "%",))}
    out = {p: "failed for want of a decoder, and an ffmpeg is now on PATH"
           for p in no_decoder}
    for p in short - no_decoder:
        out[p] = "recorded as too short before anything asked a second decoder"
    return out


def _clear_clap(conn, paths, only_failed=True):
    """Drop CLAP rows for tracks the analyze stage has just settled, so the embed stage
    looks at them again. Two callers, two scopes:

    only_failed=True, after a track that failed for want of a decoder analyzes at last.
    embed_onnx.py treats a clap row with `err` set as done and never retries it, so the
    track would gain a librosa vector, still have no embedding, and still sit outside the
    mix pool. Only rows with no vector go; a real embedding is never touched.

    only_failed=False, after a track whose audio had to be decoded a second time. Any
    embedding it already holds was computed from the same truncated read, so it describes
    a fragment rather than the song -- on this library, three such CLAP vectors were built
    from about three seconds of audio padded out with silence. Those rows go, vector and
    all, because the value in them is wrong rather than missing.

    Both callers pass a single path, one track at a time, so that the delete lands in the
    same transaction as that track's features row. The batching is kept for any caller that
    passes many at once: SQLite refuses a statement with more bound variables than its limit
    allows, and one oversized statement would raise and clear nothing."""
    if not paths:
        return 0
    paths = list(paths)
    n = 0
    where = "vec IS NULL AND " if only_failed else ""
    for i in range(0, len(paths), 400):
        chunk = paths[i:i + 400]
        try:
            cur = conn.execute(
                "DELETE FROM clap WHERE %spath IN (%s)"
                % (where, ",".join("?" * len(chunk))), tuple(chunk))
            n += cur.rowcount or 0
        except sqlite3.OperationalError as e:
            # ONLY a missing clap table is survivable, and it is the common case: embedding
            # has never run in this database. Say so rather than returning a quiet zero,
            # but once per run, because this is called per track and the same line repeated
            # a thousand times buries everything else in the log.
            #
            # Anything else -- a locked database above all -- is re-raised on purpose. The
            # features row for this track is in the same uncommitted transaction, and
            # swallowing the failure would commit a good vector beside the very CLAP row
            # this call exists to remove, with nothing left to say so.
            if "no such table" not in str(e):
                raise
            global _SAID_NO_CLAP_TABLE
            if not _SAID_NO_CLAP_TABLE:
                _SAID_NO_CLAP_TABLE = True
                print(f"note: could not clear CLAP rows ({e}); run the embed stage "
                      f"again if those tracks stay out of the mix")
            return n
    return n


_SAID_NO_CLAP_TABLE = False


def analyze(db_path, limit=None, workers=4, paths_file=None, read_map=None,
            retry_failed=False):
    conn = dbm.connect(db_path)
    # Ask once, on this thread, so the worker threads all find the answer cached rather
    # than each spawning its own `ffmpeg -version` on first use.
    feat.decoder_available()
    done = dbm.up_to_date_paths(conn)   # skip only unchanged, already-analyzed tracks
    retry = _retry_paths(conn, retry_failed)
    if retry:
        done -= retry.keys()
    mtimes = {r[0]: r[1] for r in conn.execute("SELECT path, mtime FROM tracks")}
    if paths_file:
        want = [l.strip() for l in open(paths_file, encoding="utf-8") if l.strip()]
    else:
        want = [r[0] for r in conn.execute("SELECT path FROM tracks")]
    todo = [p for p in want if p not in done]
    if limit:
        todo = todo[:limit]
    # Counted AFTER --paths-file and --limit have narrowed the list, so the number is
    # what this run will actually re-attempt rather than what was eligible.
    by_why = {}
    for p in todo:
        if p in retry:
            by_why[retry[p]] = by_why.get(retry[p], 0) + 1
    for why, n_retry in sorted(by_why.items()):
        print(f"retrying {n_retry} previously failed tracks: {why}")
    print(f"{len(want)} candidates, {len(done)} already done, {len(todo)} to analyze, workers={workers}")
    if not todo:
        print("nothing to do"); return

    t0 = time.time()
    n_ok = n_err = n_redecoded = n_held = 0
    rescued = []
    # I/O + numba release the GIL enough that threads give real speedup and avoid
    # re-importing librosa per task (process pool would be far slower to spin up).
    local = sum(1 for p in todo if _read_path(p, read_map) != p)
    if read_map:
        print(f"reading {local}/{len(todo)} from local mirror, the rest from original path")
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_analyze_one, p, _read_path(p, read_map)): p for p in todo}
        for i, fut in enumerate(cf.as_completed(futs), 1):
            path, vec, tempo, err, dt, redecoded = fut.result()
            # A row being retried that fails because the drive is offline or the file is
            # locked has learned nothing about the file. Writing that answer would replace
            # the reason that made it eligible, and neither of these is ever retried
            # automatically, so one absent drive would spend every such row's single
            # attempt for good. Leave the old row exactly where it is.
            if err in feat.TRANSIENT_ERRORS and path in retry:
                n_held += 1
            else:
                dbm.save_features(conn, path, vec, tempo, int(time.time()), err,
                                  src_mtime=mtimes.get(path))
            if err:
                n_err += 1
            else:
                n_ok += 1
                # In the SAME transaction as the features row, never at the end of the
                # run. Once this row has a vector its error is gone, so the retry query
                # will not select it again; a cancel between the commit below and a late
                # cleanup would leave the stale CLAP row behind and the track out of the
                # pool, or in it on a fragment, for good.
                if redecoded:
                    _clear_clap(conn, [path], only_failed=False)
                    n_redecoded += 1
                elif path in retry:
                    _clear_clap(conn, [path])
                if path in retry:
                    rescued.append(path)
            if i % 25 == 0 or i == len(todo):
                conn.commit()
                rate = i / (time.time() - t0)
                eta = (len(todo) - i) / rate if rate else 0
                print(f"  {i}/{len(todo)} ok={n_ok} err={n_err} "
                      f"{rate:.2f}/s eta={eta/60:.1f}m last={dt:.1f}s", flush=True)
    if rescued:
        print(f"{len(rescued)} previously failed tracks now analyze; their stale CLAP "
              f"failures were cleared as each one landed, so the embed stage retries them")
    if n_held:
        print(f"{n_held} tracks being retried could not be reached at all - the drive is "
              f"offline or the file is locked - so their rows were left as they were and "
              f"they will be retried again next time")
    if n_redecoded:
        print(f"{n_redecoded} tracks came back short from the first decoder and were "
              f"decoded again with ffmpeg; any CLAP row they held was built from the "
              f"same short read and was cleared with them")
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
    ap.add_argument("--read-map", nargs=2, metavar=("FROM", "TO"), action="append",
                    help="read audio from a local mirror: FROM path-prefix -> TO path-prefix "
                         "(repeatable). DB paths stay unchanged; only reads are redirected.")
    ap.add_argument("--retry-failed", action="store_true",
                    help="re-attempt every track whose last analysis failed. Tracks that "
                         "failed only for want of a decoder are retried automatically "
                         "once an ffmpeg is on PATH, so this is for the rest.")
    ap.add_argument("--exclude", action="append",
                    help="folder prefix to skip during import-folder (repeatable; "
                         "settings `exclude_folders` feeds this)")
    a = ap.parse_args()
    add_user_bin_to_path()
    if a.cmd == "import-folder":
        import_folder(a.db, a.arg, exclude=a.exclude or ())
    elif a.cmd == "import-catalog":
        import_catalog(a.db, a.arg)
    elif a.cmd == "analyze":
        analyze(a.db, a.limit, a.workers, a.paths_file, read_map=a.read_map,
                retry_failed=a.retry_failed)
    elif a.cmd == "stats":
        print(dbm.stats(dbm.connect(a.db)))


if __name__ == "__main__":
    main()
