"""Attune tag file layer: every tag a modern player edits, read and written through mutagen
with the file's own format kept (2026-10-02, TODO.md rows 52 and 54).

PRIOR ART, stated per CLAUDE.md section 7: the existing solution is mutagen, already in the
lean runtime, and this module is its format layer; nothing here parses a byte of ID3 or
Vorbis itself. The easy-key table (title, artist, ... website) is mutagen's own EasyID3 and
Vorbis mapping, used through EasyID3's public Get/Set/Delete tables on a raw ID3 object so
the file is opened and saved ONCE per edit.

Three things the easy keys do not cover, done per format:
  comment and lyrics   ID3 COMM / USLT frames; Vorbis COMMENT / LYRICS fields
  rating               ID3 POPM frames, the byte scale every Windows player shares (1, 64,
                       128, 196, 255 for one to five stars; Windows Media Player, MediaMonkey,
                       MusicBee and foobar2000 all read it, each under its own "email"), and
                       Vorbis RATING on the 0 to 100 scale (MusicBee, MediaMonkey, Winamp)
                       beside FMPS_RATING 0.0 to 1.0, so a reader of either scale sees the
                       same stars
  cover art            ID3 APIC; FLAC picture blocks

Two rules every write keeps:
  * the file's modified time is put back after the save, so Attune's own scan does not
    treat the song as changed audio and analyze it again (the same rule GenreTagger's
    write keeps, ISSUES.md row 94);
  * an ID3v2.3 file stays v2.3. mutagen saves v2.4 by default, and Windows Media Player
    reads v2.4 poorly, so the version the file came with is the version it keeps.
"""
from __future__ import annotations

import os
import re

# ------------------------------------------------------------------ the field table
# (key, label, kind). kind: text | number | multiline. Order is the order the window shows.
# Every key except comment, lyrics, rating and cover is a mutagen easy key, valid for ID3
# and Vorbis alike.
FIELDS = (
    ("title", "Title", "text"),
    ("artist", "Artist", "text"),
    ("albumartist", "Album artist", "text"),
    ("album", "Album", "text"),
    ("tracknumber", "Track #", "text"),
    ("discnumber", "Disc #", "text"),
    ("date", "Year", "text"),
    ("originaldate", "Original year", "text"),
    ("genre", "Genre", "text"),
    ("composer", "Composer", "text"),
    ("performer", "Performer", "text"),
    ("conductor", "Conductor", "text"),
    ("grouping", "Grouping", "text"),
    ("bpm", "BPM", "number"),
    ("compilation", "Compilation", "text"),
    ("organization", "Label", "text"),
    ("isrc", "ISRC", "text"),
    ("copyright", "Copyright", "text"),
    ("encodedby", "Encoded by", "text"),
    ("website", "Website", "text"),
    ("comment", "Comment", "multiline"),
    ("lyrics", "Lyrics", "multiline"),
)
EASY_KEYS = tuple(k for k, _, _ in FIELDS if k not in ("comment", "lyrics"))
SPECIAL_KEYS = ("comment", "lyrics")
MULTI_SEP = "; "

# ------------------------------------------------------------------ ratings
# Stars to the POPM byte: the convention Windows Media Player wrote and the others adopted.
STAR_BYTES = {1: 1, 2: 64, 3: 128, 4: 196, 5: 255}
# The "email" a new POPM frame carries. Windows Media Player, MediaMonkey and MusicBee all
# read this one; a frame another player already wrote (MusicBee's, say) is updated in place
# so that player keeps seeing its own.
POPM_EMAIL = "Windows Media Player 9 Series"


def stars_from_byte(b):
    """POPM byte to stars, by the midpoints between the five conventional bytes."""
    b = int(b or 0)
    if b <= 0:
        return 0
    if b < 32:
        return 1
    if b < 96:
        return 2
    if b < 162:
        return 3
    if b < 226:
        return 4
    return 5


def stars_from_vorbis(v):
    """A Vorbis RATING or FMPS_RATING value to stars. Three scales are in use: 0 to 1
    (FMPS), 0 to 5 (foobar2000 and others) and 0 to 100 (MusicBee, MediaMonkey, Winamp).
    Told apart by size; a value of exactly 1 is read as one star, not as the top of
    the 0 to 1 scale, because a one-star song is far more common than a 1.0 written by
    an FMPS reader for a five-star one (those write 1.0 as "1.0")."""
    try:
        s = str(v).strip()
        x = float(s)
    except (TypeError, ValueError):
        return 0
    if x <= 0:
        return 0
    if x <= 1 and ("." in s) and x < 1:
        return max(1, min(5, int(round(x * 5))))
    if x <= 5:
        return max(1, min(5, int(round(x))))
    if x <= 100:
        return max(1, min(5, int(round(x / 20))))
    return 5


# ------------------------------------------------------------------ opening a file

def _open(path):
    """(mutagen file, kind): kind is 'id3', 'vorbis' or 'other'."""
    import mutagen
    f = mutagen.File(path)
    if f is None:
        raise ValueError("unsupported or unreadable audio file")
    if f.tags is None:
        try:
            f.add_tags()
        except Exception:
            pass
    tags = f.tags
    from mutagen.id3 import ID3
    if isinstance(tags, ID3):
        return f, "id3"
    try:
        from mutagen._vorbis import VCommentDict
    except ImportError:                              # pragma: no cover - older mutagen
        VCommentDict = ()
    if isinstance(tags, VCommentDict) or type(tags).__name__ in ("VCommentDict", "VCFLACDict"):
        return f, "vorbis"
    return f, "other"


def _save(f, path, kind):
    """Save with the file's own ID3 version kept and its modified time put back."""
    st = os.stat(path)
    if kind == "id3":
        ver = getattr(f.tags, "version", (2, 4, 0))
        f.save(v2_version=3 if ver[1] == 3 else 4)
    else:
        f.save()
    os.utime(path, (st.st_atime, st.st_mtime))


# ------------------------------------------------------------------ easy keys

def _easy_get(f, kind, key):
    """List of values for an easy key, [] when absent or unsupported."""
    try:
        if kind == "id3":
            from mutagen.easyid3 import EasyID3
            fn = EasyID3.Get.get(key)
            if fn is None:
                return []
            try:
                return list(fn(f.tags, key))
            except KeyError:
                return []
        if kind == "vorbis":
            return list(f.tags.get(key, []))
        easy = getattr(f, "tags", None)
        return []
    except Exception:
        return []


def _easy_set(f, kind, key, values):
    if kind == "id3":
        from mutagen.easyid3 import EasyID3
        if values:
            fn = EasyID3.Set.get(key)
            if fn is not None:
                fn(f.tags, key, list(values))
        else:
            fn = EasyID3.Delete.get(key)
            if fn is not None:
                try:
                    fn(f.tags, key)
                except KeyError:
                    pass
    elif kind == "vorbis":
        if values:
            f.tags[key] = list(values)
        elif key in f.tags:
            del f.tags[key]


# ------------------------------------------------------------------ comment, lyrics

def _special_get(f, kind, key):
    if kind == "id3":
        frame = "COMM" if key == "comment" else "USLT"
        frames = f.tags.getall(frame)
        if not frames:
            return ""
        # the plain one first (no description), then whatever is there
        frames.sort(key=lambda fr: (fr.desc != "", fr.lang != "eng"))
        fr = frames[0]
        text = fr.text if isinstance(fr.text, str) else "\n".join(str(t) for t in fr.text)
        return text.strip("\x00")
    if kind == "vorbis":
        names = ("comment", "description") if key == "comment" else ("lyrics", "unsyncedlyrics")
        for n in names:
            if n in f.tags:
                return "\n".join(str(v) for v in f.tags[n])
        return ""
    return ""


def _special_set(f, kind, key, text):
    text = (text or "").replace("\r\n", "\n").strip()
    if kind == "id3":
        from mutagen.id3 import COMM, USLT, Encoding
        frame = "COMM" if key == "comment" else "USLT"
        # replace the plain frame(s); frames with a description belong to other programs
        for fr in list(f.tags.getall(frame)):
            if fr.desc == "":
                f.tags.delall(f"{frame}:{fr.desc}:{fr.lang}")
        if text:
            cls = COMM if key == "comment" else USLT
            f.tags.add(cls(encoding=Encoding.UTF8, lang="eng", desc="", text=text))
    elif kind == "vorbis":
        name = "comment" if key == "comment" else "lyrics"
        if text:
            f.tags[name] = [text]
        elif name in f.tags:
            del f.tags[name]


# ------------------------------------------------------------------ rating

def _rating_get(f, kind):
    if kind == "id3":
        frames = f.tags.getall("POPM")
        if not frames:
            return 0
        return max(stars_from_byte(fr.rating) for fr in frames)
    if kind == "vorbis":
        for n in ("rating", "fmps_rating"):
            if n in f.tags:
                return stars_from_vorbis(f.tags[n][0])
        return 0
    return 0


def _rating_set(f, kind, stars):
    stars = max(0, min(5, int(stars or 0)))
    if kind == "id3":
        from mutagen.id3 import POPM
        frames = f.tags.getall("POPM")
        if stars == 0:
            f.tags.delall("POPM")
            return
        b = STAR_BYTES[stars]
        seen = False
        for fr in frames:
            fr.rating = b                            # every player keeps its own frame, updated
            if fr.email == POPM_EMAIL:
                seen = True
        if not seen:
            f.tags.add(POPM(email=POPM_EMAIL, rating=b, count=0))
    elif kind == "vorbis":
        if stars == 0:
            for n in ("rating", "fmps_rating"):
                if n in f.tags:
                    del f.tags[n]
            return
        f.tags["rating"] = [str(stars * 20)]
        f.tags["fmps_rating"] = [f"{stars / 5:.1f}"]


# ------------------------------------------------------------------ cover art

def _cover_get(f, kind):
    """(bytes, mime) of the front cover, or None."""
    if kind == "id3":
        frames = f.tags.getall("APIC")
        if not frames:
            return None
        frames.sort(key=lambda fr: int(fr.type) != 3)   # front cover first
        return frames[0].data, frames[0].mime
    pics = getattr(f, "pictures", None)
    if pics:
        pics = sorted(pics, key=lambda p: int(p.type) != 3)
        return pics[0].data, pics[0].mime
    return None


def _cover_set(f, kind, data, mime):
    if kind == "id3":
        from mutagen.id3 import APIC, Encoding, PictureType
        f.tags.delall("APIC")
        if data:
            f.tags.add(APIC(encoding=Encoding.LATIN1, mime=mime, type=PictureType.COVER_FRONT,
                            desc="", data=data))
    elif hasattr(f, "clear_pictures"):
        f.clear_pictures()
        if data:
            from mutagen.flac import Picture
            p = Picture()
            p.data, p.mime, p.type, p.desc = data, mime, 3, ""
            f.add_picture(p)
    else:
        raise ValueError("this file format cannot hold a cover here")


def image_mime(data):
    """The MIME type of a JPEG or PNG by its first bytes, else None."""
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


# ------------------------------------------------------------------ the two public calls

def read(path):
    """Every field, the rating, the cover's presence, and what the file is."""
    f, kind = _open(path)
    fields = []
    for key, label, fkind in FIELDS:
        if key in SPECIAL_KEYS:
            vals = [_special_get(f, kind, key)] if _special_get(f, kind, key) else []
            multi = False
        else:
            vals = [str(v) for v in _easy_get(f, kind, key)]
            multi = len(vals) > 1
        fields.append({"key": key, "label": label, "kind": fkind,
                       "value": MULTI_SEP.join(vals) if key not in SPECIAL_KEYS else (vals[0] if vals else ""),
                       "multi": multi, "supported": kind != "other" or key not in SPECIAL_KEYS})
    cover = _cover_get(f, kind) if kind != "other" else None
    info = {}
    if f.info:
        info = {"length": round(getattr(f.info, "length", 0) or 0, 1),
                "bitrate": getattr(f.info, "bitrate", 0) or 0,
                "sample_rate": getattr(f.info, "sample_rate", 0) or 0,
                "channels": getattr(f.info, "channels", 0) or 0,
                "codec": type(f).__name__}
    tagfmt = ""
    if kind == "id3":
        v = getattr(f.tags, "version", None)
        tagfmt = f"ID3v{v[0]}.{v[1]}" if v else "ID3"
    elif kind == "vorbis":
        tagfmt = "Vorbis comments"
    else:
        tagfmt = type(f.tags).__name__ if f.tags is not None else "none"
    return {"fields": fields, "rating": _rating_get(f, kind) if kind != "other" else 0,
            "cover": ({"mime": cover[1], "bytes": len(cover[0])} if cover else None),
            "tag_format": tagfmt, "kind": kind, "info": info,
            "tags": {fl["key"]: fl["value"] for fl in fields}}


def write(path, new_fields):
    """Write the fields given (a dict key -> string). A key missing from the dict is left
    alone; an empty string removes the field. A field that held several values keeps
    holding several when the string carries '; '. Returns the fields as read back."""
    f, kind = _open(path)
    for key, _label, _k in FIELDS:
        if key not in new_fields:
            continue
        v = str(new_fields[key] if new_fields[key] is not None else "").strip()
        if key in SPECIAL_KEYS:
            if kind == "other":
                continue
            _special_set(f, kind, key, v)
            continue
        cur = _easy_get(f, kind, key)
        if not v:
            _easy_set(f, kind, key, [])
        elif len(cur) > 1 and MULTI_SEP in v:
            _easy_set(f, kind, key, [p.strip() for p in v.split(MULTI_SEP) if p.strip()])
        else:
            _easy_set(f, kind, key, [v])
    _save(f, path, kind)
    return read(path)


def read_rating(path):
    try:
        f, kind = _open(path)
    except ValueError:
        return None
    if kind == "other":
        return None
    return _rating_get(f, kind)


def write_rating(path, stars):
    f, kind = _open(path)
    if kind == "other":
        raise ValueError("this file format cannot hold a rating")
    _rating_set(f, kind, stars)
    _save(f, path, kind)


def read_cover(path):
    f, kind = _open(path)
    return _cover_get(f, kind) if kind != "other" else None


def write_cover(path, data, mime=None):
    """Replace the cover with the JPEG or PNG given; data None or b'' removes it."""
    if data:
        mime = mime or image_mime(data)
        if mime not in ("image/jpeg", "image/png"):
            raise ValueError("the cover must be a JPEG or PNG image")
    f, kind = _open(path)
    _cover_set(f, kind, data or b"", mime or "image/jpeg")
    _save(f, path, kind)


_YEAR = re.compile(r"\s*(\d{4})")


def year_of(date_text):
    m = _YEAR.match(str(date_text or ""))
    return int(m.group(1)) if m else None
