r"""Resolve a folder of loose audio files to the tracks your Plex server already has.

The problem this exists for: a folder like ``L:\_MUSIC\MP3 CD`` is a hand-built car
roster, copied out of the library over years. It is NOT under the library root, so
``export.PathMapper`` correctly refuses to rewrite its paths (ruling (a),
MORNING_REPORT_2026-07-29 5.1 -- a root-prefix swap on a path outside the root is the
"drive letter in the middle of a UNC path" bug). Prefix maths cannot answer "which
library track is this file a copy of". So this module asks a different question and
answers it with evidence: WHICH TRACK IN PLEX IS THE SAME RECORDING?

Three tiers, tried in order, each gated by duration:

  basename      the file name is byte-identical to a file Plex has indexed.
  artist+title  ID3 artist and title, normalised, match a Plex track's own metadata.
  title-only    only the title matches; the artist must still appear as a substring
                on one side or the other, so "Non Blondes" reaches "4 Non Blondes".

DURATION IS THE ARBITER, NOT A TIEBREAKER. Every candidate carries Plex's own
``duration``; the source file's real length comes from mutagen. A candidate more than
``NEAR_SECS`` off is demoted out of the tier even when its name matches perfectly, and a
tier whose best candidate is more than ``FAR_SECS`` off does not resolve at that tier at
all. This is what stops "Life During Wartime" (studio) silently becoming "Life During
Wartime (live)": same artist, same title, ten seconds apart, and the report says so.

NOTHING IS EVER SILENTLY SUBSTITUTED. Every resolution carries a grade
(``exact`` / ``strong`` / ``probable`` / ``weak``) and the seconds of drift that earned
it, and anything that resolves at ``probable`` or worse is listed in ``flagged`` for a
human to look at. A file with no honest answer lands in ``residue`` with the artist,
title and duration that were searched for -- never a plausible near-miss.

Measured on the operator's real roster 2026-09-01: 128 files, 127 resolved
(100 basename, 27 tag-based), 1 genuine residue (a track the library does not contain),
3 flagged. Filename matching alone reached 101 of 128, which is why tags and duration
are the primary evidence here and the file name is only the fast path.

No new dependency: mutagen is already in the lean runtime venv, and the Plex side is
``export.PlexExporter``'s existing urllib calls. python-plexapi was considered and
rejected -- it would add a package to a frozen exe to make HTTP requests this repo
already makes correctly.
"""
from __future__ import annotations

import collections
import os
import re
import unicodedata

# Duration windows, in seconds. NEAR is "the same recording, allowing for tag/encoder
# rounding"; FAR is "close enough to be worth reporting, but a human should look".
NEAR_SECS = 2.0
FAR_SECS = 8.0

AUDIO_EXT = (".mp3", ".flac", ".m4a", ".wma", ".ogg", ".opus", ".wav", ".aac", ".alac")

# A leading track number, with or without a separator: "13 - x", "02 x", "10-088 - x".
_LEAD_NUM = re.compile(r"^\d{1,3}(?:[-_. ]+\d{1,3})*[-_. ]+")
_PARENS = re.compile(r"\(.*?\)|\[.*?\]")
_FEAT = re.compile(r"\b(?:feat|ft|featuring|with)\b.*$")
_NONWORD = re.compile(r"[^a-z0-9]+")
_LEADING_ARTICLE = re.compile(r"^(?:the|a|an)\s+")
_VERSIONY = re.compile(r"\b(?:live|remix|karaoke|instrumental|acoustic|demo|edit)\b")


def normalise(s):
    """Fold a title or artist to a comparable key.

    Deliberately lossy and deliberately NOT fuzzy: accents stripped, case folded,
    '&' spelled out, a trailing "feat. X" dropped, bracketed suffixes dropped,
    punctuation collapsed, a leading article dropped. Two strings either produce the
    same key or they do not. No edit-distance anywhere in this module -- a fuzzy
    matcher's near-misses are exactly the silent wrong answer ruling (a) exists to
    prevent, and duration is a far better discriminator than string distance.
    """
    s = unicodedata.normalize("NFKD", s or "")
    s = "".join(c for c in s if not unicodedata.combining(c)).casefold()
    s = s.replace("&", " and ").replace("\u2019", "'").replace("\u0060", "'")
    s = _FEAT.sub(" ", s)
    s = _PARENS.sub(" ", s)
    s = _NONWORD.sub(" ", s)
    s = _LEADING_ARTICLE.sub("", s.strip())
    return re.sub(r"\s+", " ", s).strip()


def read_source(path):
    """(artist, title, album, seconds) for one audio file. Tags first, name as backup.

    Tags win because the file NAME is the thing that turned out to be unreliable: on the
    real roster, ``zAll I Need.mp3`` carries a correct AWOLNATION tag and a name that
    matches nothing, and ``10-088 - Rammstein - Du Hast (1998).mp3`` has a double track
    number no filename splitter parses correctly. The name is parsed only to fill a
    field the tags left empty.

    Returns seconds = 0.0 when the file is unreadable, which makes every duration gate
    fail closed: an unreadable file resolves at most to ``weak`` and gets flagged.
    """
    artist = title = album = ""
    secs = 0.0
    try:
        from mutagen import File as MutagenFile
        mf = MutagenFile(path, easy=True)
        if mf is not None:
            artist = (mf.get("artist") or [""])[0]
            title = (mf.get("title") or [""])[0]
            album = (mf.get("album") or [""])[0]
            if getattr(mf, "info", None) is not None:
                secs = float(getattr(mf.info, "length", 0.0) or 0.0)
    except Exception:
        pass
    if not (artist and title):
        stem = _LEAD_NUM.sub("", os.path.splitext(os.path.basename(path))[0])
        if " - " in stem:
            name_artist, name_title = stem.split(" - ", 1)
        elif "-" in stem:
            name_artist, name_title = stem.split("-", 1)
        else:
            name_artist, name_title = "", stem
        artist = artist or name_artist.strip()
        title = title or name_title.strip()
    return artist, title, album, secs


def list_folder(folder, recursive=True):
    """Every audio file at or under `folder`, sorted. Subfolders ARE descended.

    OPERATOR RULING, 2026-09-01, his words: "the rule is if it's in there it's on the
    playlist - including folders that could be dropped in there now or later." The
    roster folder is the roster, whatever shape it is in. An earlier version of this
    function defaulted to non-recursive because that folder happened to hold four
    unrelated sub-projects on the day it was written; he deleted them and ruled the
    other way, so that rationale is dead and this is not a preference to toggle back.

    `recursive=False` survives as a caller-side escape hatch, unused by Attune itself.
    """
    out = []
    if recursive:
        for root, _dirs, names in os.walk(folder):
            out += [os.path.join(root, n) for n in names if n.lower().endswith(AUDIO_EXT)]
    else:
        for n in os.listdir(folder):
            p = os.path.join(folder, n)
            if n.lower().endswith(AUDIO_EXT) and os.path.isfile(p):
                out.append(p)
    return sorted(out)


class PlexCatalog:
    """The Plex music section, indexed three ways, built once per run.

    `tracks` is whatever ``PlexExporter.build_meta_index()`` returned: dicts carrying
    rk / title / artist / album / dur (ms) / files. Paging the operator's 21,421-track
    section takes about 6 seconds, so this is rebuilt per run rather than cached -- a
    cached index that misses a newly-added album is a residue entry nobody can explain.
    """

    def __init__(self, tracks):
        self.tracks = tracks
        self.by_base = collections.defaultdict(list)
        self.by_artist_title = collections.defaultdict(list)
        self.by_title = collections.defaultdict(list)
        for t in tracks:
            for f in t.get("files") or []:
                self.by_base[os.path.basename(f).casefold()].append(t)
            nt = normalise(t.get("title"))
            if not nt:
                continue
            self.by_title[nt].append(t)
            # originalTitle carries the track artist on compilations, where
            # grandparentTitle is the useless "Various Artists".
            for a in filter(None, (t.get("artist"), t.get("orig"))):
                self.by_artist_title[(normalise(a), nt)].append(t)


def _drift(track, secs):
    """|seconds of difference| between a Plex track and a source file. 999 if unknown."""
    d = track.get("dur")
    if not d or not secs:
        return 999.0
    return abs(d / 1000.0 - secs)


def _rank(cands, secs, album, prefer_zone):
    """Order candidates so the choice is deterministic and explainable.

    Sorted by, in order: duration drift bucket (inside NEAR, inside FAR, outside),
    then a small penalty stack, then raw drift, then path -- so the same inputs always
    give the same answer and two equally-good candidates are reported as a tie rather
    than resolved by list order.
    """
    ranked = []
    for c in cands:
        d = _drift(c, secs)
        bucket = 0 if d <= NEAR_SECS else (1 if d <= FAR_SECS else 2)
        penalty = 0.0
        if album and normalise(c.get("album")) == normalise(album):
            penalty -= 2.0
        path = (c.get("files") or [""])[0]
        if prefer_zone and path.startswith(prefer_zone):
            penalty -= 1.0
        if normalise(c.get("artist")) == "various artists":
            penalty += 1.5
        if _VERSIONY.search(normalise(c.get("title"))):
            penalty += 1.0
        ranked.append((bucket, penalty, d, path, c))
    ranked.sort(key=lambda r: (r[0], r[1], r[2], r[3]))
    return ranked


def resolve_one(catalog, path, prefer_zone=None):
    """Resolve one source file. Returns a dict; ``grade`` says how much to trust it.

    Grades:
      exact     name-identical AND within NEAR_SECS -- no judgement involved.
      strong    tag artist+title match AND within NEAR_SECS.
      probable  matched, but only within FAR_SECS, or matched on title alone.
      weak      matched with no usable duration on one side, or outside FAR_SECS.
      (none)    ``track`` is None; the caller puts this in residue.
    """
    artist, title, album, secs = read_source(path)
    base = os.path.basename(path)
    na, nt = normalise(artist), normalise(title)
    # prefer_zone may be a fixed server-path prefix, or a callable that picks one per
    # file -- the operator's roster encodes it in the name ("13 - x" came out of an
    # album, "Artist - x" out of the singles folder). It is only ever a tiebreak
    # nudge worth one point; duration still decides.
    if callable(prefer_zone):
        prefer_zone = prefer_zone(base)

    attempts = [
        ("basename", catalog.by_base.get(base.casefold(), [])),
        ("artist+title", catalog.by_artist_title.get((na, nt), []) if na and nt else []),
        ("title-only", [c for c in catalog.by_title.get(nt, [])
                        if na and (na in normalise(c.get("artist"))
                                   or normalise(c.get("artist")) in na)] if nt else []),
    ]

    best = None
    for tier, cands in attempts:
        if not cands:
            continue
        ranked = _rank(cands, secs, album, prefer_zone)
        head = ranked[0]
        bucket, _pen, drift, _p, track = head
        if bucket == 0:
            grade = ("exact" if tier == "basename"
                     else "strong" if tier == "artist+title" else "probable")
        elif bucket == 1:
            grade = "probable"
        else:
            grade = "weak"
        # A tie is a candidate the ranking CANNOT separate from the head: same bucket,
        # same penalty, within half a second. Listed without the head itself, so an
        # empty list honestly means "nothing else came close".
        tied = [r[3] for r in ranked[1:] if r[0] == head[0] and abs(r[1] - head[1]) < 1e-9
                and abs(r[2] - head[2]) < 0.5]
        cand = {"tier": tier, "grade": grade, "track": track, "drift": round(drift, 2),
                "_drift": drift, "tied": tied}
        # Take the first tier that lands inside NEAR; otherwise keep the best seen so
        # far and let a later tier beat it. A perfect-duration tag match is worth more
        # than a same-name file that is forty seconds longer.
        #
        # Compared on the RAW drift, not the rounded one. Comparing raw-against-rounded
        # made 2.0753 < 2.08 true, so an identical-drift later tier displaced an equally
        # good earlier one and the report named the weaker tier as the reason -- a
        # cosmetic bug in a report whose only job is to be believable.
        if bucket == 0:
            best = cand
            break
        if best is None or drift < best["_drift"]:
            best = cand

    if best is not None:
        best.pop("_drift", None)
    return {"file": base, "src": path, "artist": artist, "title": title,
            "album": album, "secs": round(secs, 1),
            **(best or {"tier": None, "grade": None, "track": None,
                        "drift": None, "tied": []})}


def resolve_folder(catalog, folder, recursive=True, prefer_zone=None):
    """Resolve every audio file in `folder`. Returns a report dict.

    Report keys:
      items     one entry per source file, in folder order, resolved or not
      resolved  the subset that found a track, deduplicated by ratingKey
      residue   the subset that found nothing -- the honest "not in your library" list
      flagged   resolved entries a human should eyeball (probable/weak, or a tie)
      dupes     source files that resolved to a track another file already claimed
      counts    a one-glance tally
    """
    items = [resolve_one(catalog, p, prefer_zone) for p in list_folder(folder, recursive)]
    resolved, residue, flagged, dupes = [], [], [], []
    seen = {}
    for it in items:
        if not it.get("track"):
            residue.append(it)
            continue
        rk = it["track"]["rk"]
        if rk in seen:
            dupes.append({"file": it["file"], "same_as": seen[rk], "rk": rk})
            continue
        seen[rk] = it["file"]
        resolved.append(it)
        if it["grade"] in ("probable", "weak") or it["tied"]:
            flagged.append(it)
    counts = {"source": len(items), "resolved": len(resolved), "residue": len(residue),
              "flagged": len(flagged), "dupes": len(dupes),
              "by_grade": dict(collections.Counter(i["grade"] for i in resolved))}
    return {"folder": folder, "items": items, "resolved": resolved, "residue": residue,
            "flagged": flagged, "dupes": dupes, "counts": counts}


def format_report(rep, limit=0):
    """The report as plain lines a person can read. Residue and flags come FIRST.

    Deliberately leads with what is wrong: a report that opens with 127 successes
    trains the reader to stop before the one failure.
    """
    c = rep["counts"]
    out = [f"folder: {rep['folder']}",
           f"{c['source']} audio files -> {c['resolved']} matched in Plex, "
           f"{c['residue']} not found, {c['flagged']} to eyeball, {c['dupes']} duplicate",
           f"confidence: {c['by_grade']}", ""]
    if rep["residue"]:
        out.append("NOT IN YOUR LIBRARY (these cannot go in the playlist):")
        for r in rep["residue"]:
            out.append(f"  {r['file']}")
            out.append(f"      searched for: {r['artist']!r} / {r['title']!r}  {r['secs']}s")
        out.append("")
    if rep["dupes"]:
        out.append("SAME TRACK TWICE IN THE FOLDER (kept once):")
        for d in rep["dupes"]:
            out.append(f"  {d['file']}  ==  {d['same_as']}")
        out.append("")
    if rep["flagged"]:
        out.append("MATCHED, BUT WORTH A LOOK:")
        for f in rep["flagged"]:
            t = f["track"]
            out.append(f"  [{f['grade']}/{f['tier']}] {f['file']}")
            out.append(f"      -> {t.get('artist')} / {t.get('title')}   "
                       f"({f['drift']}s different)")
            out.append(f"      {(t.get('files') or [''])[0]}")
            for tie in f["tied"]:
                out.append(f"      tied with: {tie}")
        out.append("")
    out.append("MATCHED:")
    rows = rep["resolved"] if not limit else rep["resolved"][:limit]
    for r in rows:
        t = r["track"]
        out.append(f"  [{r['grade']}] {r['file']}  ->  {(t.get('files') or [''])[0]}")
    if limit and len(rep["resolved"]) > limit:
        out.append(f"  ... and {len(rep['resolved']) - limit} more")
    return "\n".join(out)


# ---------------------------------------------------------------- naming + order
# Operator ruling 2026-09-01: a playlist is named from a TEMPLATE, and the default
# stamps the date it was made -- "DrivingTunesUSB (26-09-01)". A name he types himself
# ("DrivingTunesUSB Fall 2026") is left exactly as typed.
#
# The consequence is worth stating out loud because it changes what the sync DOES: the
# name is the identity. Same name -> the existing playlist is updated in place. A name
# with today's date in it -> a new playlist every day, and yesterday's is left standing.
# Neither is more correct; the template decides, and the panel shows the resolved name
# before anything is written so the choice is visible rather than inferred.
DEFAULT_TITLE_TEMPLATE = "DrivingTunesUSB ({date})"

# Kept deliberately small. Every extra token is another thing to look up, and he can
# type any literal text he wants around them.
TITLE_TOKENS = {
    "date": "%y-%m-%d",      # 26-09-01
    "ymd": "%Y-%m-%d",       # 2026-09-01
    "year": "%Y",            # 2026
    "month": "%B",           # September
}

_TOKEN_RE = re.compile(r"\{(\w+)\}")


def resolve_title(template, when=None):
    """Expand {date}/{ymd}/{year}/{month} in a playlist name template.

    `when` is a datetime the CALLER supplies, so the resolved name is stamped once and
    then carried: expanding lazily at write time would let a run started at 23:59 preview
    one name and create another, which is precisely the kind of "the thing I approved is
    not the thing that happened" the preview/apply split exists to prevent.

    An unknown token is left standing as literal text rather than blanked -- a name that
    still visibly says {foo} tells the operator he mistyped something; a silently empty
    one does not.
    """
    import datetime
    when = when or datetime.datetime.now()

    def sub(m):
        fmt = TITLE_TOKENS.get(m.group(1))
        return when.strftime(fmt) if fmt else m.group(0)

    return _TOKEN_RE.sub(sub, template or DEFAULT_TITLE_TEMPLATE).strip()


# Running order. "folder" is the default because the folder IS the roster -- a stick
# plays in filename order and this matches it, so the car and Plex agree.
ORDERS = ("folder", "shuffle", "artist")


def order_resolved(resolved, how="folder", seed=0):
    """Return `resolved` (plexmatch's matched entries) in the running order asked for.

    shuffle uses a deterministic Fisher-Yates driven by `seed`, NOT random.shuffle: the
    same seed gives the same order, so the list previewed is the list written. A shuffle
    that re-rolled between looking and applying would break the preview contract for the
    one option most likely to be used.
    """
    rows = list(resolved)
    if how == "artist":
        def key(r):
            t = r.get("track") or {}
            return (normalise(t.get("artist")), normalise(t.get("album")),
                    normalise(t.get("title")))
        rows.sort(key=key)
    elif how == "shuffle":
        state = (int(seed) or 1) & 0x7FFFFFFF
        for i in range(len(rows) - 1, 0, -1):
            # Lehmer / MINSTD: tiny, dependency-free, and reproducible from the seed.
            state = (state * 48271) % 2147483647
            j = state % (i + 1)
            rows[i], rows[j] = rows[j], rows[i]
    # "folder" is already the order resolve_folder produced (sorted paths); leave it.
    return rows
