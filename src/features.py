"""Acoustic feature extraction — the modern replacement for MusicIP's proprietary
analysis. Produces a fixed-length descriptor per track capturing timbre, harmony,
rhythm, brightness and dynamics: the dimensions a "sounds-like" mix depends on.

Design notes:
- 22.05 kHz mono, analyze a central window (skip intro/outro) for speed + stability.
- Feature groups are kept separate + labelled so the mixer can weight them
  (MusicIP's "style" slider is essentially a re-weighting of timbre vs. everything).
- Output is a 1-D float32 vector plus a small dict of scalar summaries (tempo, etc.).
"""
from __future__ import annotations
import os
import shutil
import subprocess
import numpy as np

SR = 22050
CENTER_SECONDS = 90.0     # analyze up to this many seconds from the middle
MIN_SECONDS = 5.0         # below this there is too little audio to describe
N_MFCC = 20

# Feature layout (kept in sync with FEATURE_NAMES for weighting):
#   mfcc_mean[20], mfcc_std[20]        -> timbre (40)
#   chroma_mean[12], chroma_std[12]    -> harmony/key (24)
#   contrast_mean[7]                   -> spectral contrast (7)
#   centroid, bandwidth, rolloff, zcr, rms_mean, rms_std, flatness (7)
#   tempo (1)  [stored but usually down-weighted / used as scalar]
# total dim = 40 + 24 + 7 + 7 + 1 = 79

FEATURE_GROUPS = {
    "timbre":    (0, 40),    # MFCC mean+std
    "harmony":   (40, 64),   # chroma mean+std
    "contrast":  (64, 71),   # spectral contrast
    "texture":   (71, 78),   # centroid/bandwidth/rolloff/zcr/rms/flatness
    "tempo":     (78, 79),   # bpm (scaled)
}
FEATURE_DIM = 79


# The stored `error` string is read by two audiences. A person reads it in the Not
# Mixable view (web/studio.py renders it after "librosa analysis failed: "), so it has
# to be a sentence they can act on. scan.py reads the first word to decide whether the
# row is worth another attempt, so the prefix is a contract, not decoration:
#
#   NEEDS_DECODER   nothing on this machine can read the format. Retry automatically
#                   once an ffmpeg appears (scan.py does this).
#   anything else   retrying changes nothing until the file itself changes.
#
# Formats libsndfile reads on its own: mp3, flac, ogg, opus, wav, aiff. Formats that
# always need ffmpeg: m4a, aac, wma. A DAMAGED file of any format may need it too --
# measured 2026-09-20, 22 of a random 300 mp3 files from a real library (7.33%) are
# rejected by libsndfile and read fine by ffmpeg, which is why the shipped build still
# carries ffmpeg.exe even though ffprobe is gone.
#
# Before 2026-09-20 this branch stored "load: NoBackendError: " -- an exception name
# with an empty message, which told nobody anything.
NEEDS_DECODER = "no decoder:"

_FFMPEG_FORMATS = {".m4a", ".aac", ".wma", ".m4b", ".m4p", ".alac", ".ape", ".wv"}


# A decoder that SUCCEEDS and hands back a fragment is worse than one that fails, because
# nothing downstream can tell the difference. librosa.load asks libsndfile first and only
# falls back to ffmpeg when libsndfile RAISES; on a damaged mp3 libsndfile often does not
# raise, it returns a second or two and reports success. Measured 2026-09-20 on the 18
# tracks of this library that are in no mix: libsndfile raised on 2 of them and returned a
# fragment of 0.0 to 3.8 seconds on the other 16, without error, against headers saying 76
# to 417 seconds. ffmpeg gets enough audio to analyze on 14 of the 18: 13 of the fragment
# cases and 1 of the two that raised. Every one of the 18 was stored as "too short", which
# is not what happened and sends whoever reads it to look at the wrong thing.
#
# So a decode that came back with too little to analyze is not the end of the story: ffmpeg
# is asked as well, and its answer is used when it returns more. The trigger is deliberately
# the ANALYSIS FLOOR rather than the container's stated duration, which makes the change
# provably additive - it can only touch a file that produces no vector as things stand.
#
# The stated duration was the trigger in the first draft of this, and both cold auditors
# killed it for the same reason on 2026-09-20. mutagen falls back to
# `8 * content_size / first_frame_bitrate` for an mp3 with no Xing or VBRI header (read from
# the installed mutagen/mp3/__init__.py), so a variable-bitrate file whose first frame is
# quiet overstates its own length several times over. That would have fired on a perfectly
# healthy file, and since ffmpeg and libsndfile disagree by an encoder delay of a few hundred
# samples, the longer answer would have won and replaced a good vector. The stated duration
# is still read, but only to word the failure, where being wrong costs nothing.
#
# Amended 2026-09-21, on Joe's ruling to repair the songs judged on a fragment. The floor
# trigger rescued reads under five seconds and nothing else; the full-library sweep that
# evening found 77 tracks read to between 5.7 and 137 seconds of songs three to seventy
# minutes long, and none of them could ever be rescued, --force or not, because 5.8 seconds
# clears the floor. So the stated duration now NOMINATES a second decode as well (see
# load_audio and _claim_says_short), and the auditors' objection is answered by the
# acceptance bar rather than the trigger: ffmpeg's answer replaces the first read only when
# it holds CONFIRM_GAIN times more audio, the same bar short_read_check already used before
# calling anything short. An overstated header on a healthy file fails that bar by design.
TRUNCATED_FRACTION = 0.5      # decoded below this share of the claim is worth a second look
MIN_CLAIMED_SECONDS = 20.0    # below this, "half of it" is not a meaningful gap
CONFIRM_GAIN = 1.5            # a second decoder must beat the first by this to be believed
# A safety valve on how much audio one rescued file may produce, because the whole decode
# is held in memory: 1200 s is about 106 MB per worker at 22.05 kHz float32, against 317 MB
# at an hour. Nothing that needs rescuing is a 20-minute song, and a longer one would be
# analyzed from its first 20 minutes rather than refused.
FFMPEG_CAP_SECONDS = 1200
FFMPEG_TIMEOUT_S = 300

# What _load_error says when the failure is about the machine rather than the file. Neither
# is ever retried automatically, so scan.py leaves the row it came from alone instead of
# spending that row's one attempt on a drive that happened to be offline.
TRANSIENT_ERRORS = (
    "the file is not there any more - it was moved, renamed or deleted",
    "Windows would not let Attune open the file - it may be in use by another program",
)

# The reasons written for a short decode BEFORE 2026-09-20, when no second decoder was
# ever asked. scan.py matches these as PREFIXES to give each such row exactly one more
# attempt, which also catches a trailing space or an older wording of the same sentence.
# No reason written today can begin with either, so a rewritten row is never picked up
# again and no row can be retried run after run.
LEGACY_SHORT_ERROR_PREFIXES = (
    "too short",
    "the file holds under",
)


_FFMPEG_STATE: str | None = None


def ffmpeg_state() -> str:
    """"ok", "broken" or "none". The analyzer puts the user's own folder and any bundled
    bin on PATH before this runs (desktop/worker_entry.py), and scan.py does the same for
    the ML-venv route.

    It asks ffmpeg for its version rather than only looking for the file, because the
    difference decides three things: whether a failure is permanent or worth retrying
    later, whether the reason shown to a person may say ffmpeg was tried, and whether
    telling them to put an ffmpeg.exe in a folder is useful or insulting when a broken
    one is already sitting there. A wrong-architecture, zero-byte or antivirus-blocked
    drop-in passes a name check and then cannot be run.

    Probed once per process, because PATH does not change under a running analyzer."""
    global _FFMPEG_STATE
    if _FFMPEG_STATE is None:
        exe = shutil.which("ffmpeg")
        if not exe:
            _FFMPEG_STATE = "none"
        else:
            try:
                r = subprocess.run(
                    [exe, "-version"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                _FFMPEG_STATE = "ok" if r.returncode == 0 else "broken"
            except Exception:
                _FFMPEG_STATE = "broken"
    return _FFMPEG_STATE


def decoder_available() -> bool:
    """True when there is an ffmpeg that actually starts, for the decoders to shell out
    to. Call this once at the top of a run rather than racing several threads into the
    probe; they all get the same answer, but only one of them needs to ask."""
    return ffmpeg_state() == "ok"


def _decoder_hint() -> str:
    """What to tell a person to do about a missing decoder, which is a different sentence
    when the ffmpeg.exe they already put there is the problem."""
    if ffmpeg_state() == "broken":
        return (f"the ffmpeg.exe Attune found does not run on this PC - replace it in "
                f"{user_bin_hint()}, or install ffmpeg, then rescan")
    return (f"put ffmpeg.exe in {user_bin_hint()}, or install ffmpeg on this PC, then "
            f"rescan - Attune retries these by itself")


def user_bin_hint() -> str:
    r"""The folder to tell a person to drop ffmpeg.exe into, as a real path they can
    paste into Explorer rather than the literal text %APPDATA%."""
    base = os.environ.get("APPDATA")
    return os.path.join(base, "Attune", "bin") if base else r"%APPDATA%\Attune\bin"


def container_seconds(path: str) -> float | None:
    """How long the file says it is, read from its header without decoding anything.

    mutagen is already the tag reader for the whole import (scan.py `_mutagen_tags`), and
    its `info.length` is the same number that ends up in the `seconds` column, so this
    adds no dependency and no second opinion. Returns None when mutagen cannot open the
    file, when the container carries no length, or when the length is absurd -- a wav
    with a 0xFFFFFFFF data-chunk size reports 48,695 seconds, and a number like that is
    not evidence of anything."""
    try:
        import mutagen
    except ImportError:
        return None
    try:
        f = mutagen.File(path)
    except Exception:
        return None
    try:
        dur = float(getattr(getattr(f, "info", None), "length", None))
    except (TypeError, ValueError):
        return None
    if not 0.0 < dur <= 6 * 3600:
        return None
    return dur


def _ffmpeg_decode(path: str, sr: int = SR) -> np.ndarray | None:
    """Decode with ffmpeg itself, straight to mono float32 at `sr`. None if it produced
    nothing or there is no ffmpeg to run.

    Why this and not librosa's own audioread fallback, which is the in-repo and in-library
    precedent: measured on the 18 damaged files on 2026-09-20, this returned at least as
    much audio as audioread on every one of them and more on two -- 416.6 seconds against
    208.3 on one, and 6.7 seconds where audioread raised instead. librosa has also
    deprecated its audioread support (0.10, removal in 1.0), so owning the call is what
    keeps this working later. Flags were measured too: -err_detect ignore_err,
    -fflags +discardcorrupt, a forced mp3 decoder and a bigger probe size each changed
    nothing on all six stubborn files, so none of them are here.

    ffmpeg's exit code is deliberately ignored. On a damaged file it writes the whole song
    to stdout and THEN exits 69; reading the code rather than the bytes would throw the
    song away.

    `aresample=rematrix_maxval=1.0` is not decoration. ffmpeg's default stereo-to-mono
    matrix is 0.707*(L+R) where librosa's to_mono is the plain mean 0.5*(L+R), so without
    it every rescued vector would carry 3 dB more loudness than the rows it is compared
    against. Measured on this PC, 2026-09-20: ratio 1.4142 without it on stereo and on
    stereo with identical channels, 1.0000 with it, 1.0000 either way on mono. Found by
    the Fable auditor. Do not replace it with a `pan=` filter, which was measured wrong
    on mono."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    cmd = [exe, "-nostdin", "-v", "quiet", "-i", path,
           "-t", str(FFMPEG_CAP_SECONDS),
           "-af", "aresample=rematrix_maxval=1.0",
           "-f", "f32le", "-ac", "1", "-ar", str(int(sr)), "-"]
    try:
        r = subprocess.run(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=FFMPEG_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception:
        # Including a timeout, whose partial output is deliberately NOT harvested: half a
        # song from a decoder that hung is the same fragment this whole guard exists to
        # refuse, and it would arrive with nothing to mark it as partial.
        return None
    raw = r.stdout or b""
    raw = raw[:len(raw) - len(raw) % 4]      # stdout can end mid-sample; drop the tail
    if not raw:
        return None
    y = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    if not np.all(np.isfinite(y)):
        y = np.nan_to_num(y, nan=0.0, posinf=0.0, neginf=0.0)
    return y


def _ffmpeg_seconds(path: str, sr: int = SR) -> float | None:
    """How much audio ffmpeg can actually get out of this file, in seconds, without
    keeping any of it. Same decode as _ffmpeg_decode, but the samples are counted and
    thrown away as they arrive, so asking the question costs no memory."""
    exe = shutil.which("ffmpeg")
    if not exe:
        return None
    cmd = [exe, "-nostdin", "-v", "quiet", "-i", path,
           "-t", str(FFMPEG_CAP_SECONDS),
           "-af", "aresample=rematrix_maxval=1.0",
           "-f", "f32le", "-ac", "1", "-ar", str(int(sr)), "-"]
    try:
        pr = subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
    except Exception:
        return None
    n = 0
    try:
        while True:
            chunk = pr.stdout.read(1 << 20)
            if not chunk:
                break
            n += len(chunk)
    except Exception:
        return None
    finally:
        try:
            pr.stdout.close()
            pr.wait(timeout=FFMPEG_TIMEOUT_S)
        except Exception:
            pr.kill()
    return (n // 4) / sr


def short_read_check(path: str, got: float, sr: int = SR) -> dict | None:
    """Was this track analyzed from only a fraction of the song?

    This is the quiet cousin of the failure this module now catches. A decode that stops
    early but still clears the analysis floor produces a perfectly ordinary looking vector
    built from part of the song, and nothing downstream can tell. Measured 2026-09-20 on a
    60-track sample of a 21,237-track library: one confirmed case, a 175-second track that
    Attune read 80 seconds of.

    Two steps, because one is not enough. The container's stated duration NOMINATES a
    suspect and nothing more: mutagen estimates an mp3's length from its first frame when
    there is no Xing or VBRI header, and on 238 real files it nominated two tracks whose
    audio really was that short. ffmpeg then DECIDES, by having to return materially more
    audio than the first reader did.

    Returns None when there is nothing to report, else what was claimed, what was read and
    what is actually there. It only reports. Acting on it would rewrite a vector that
    already exists, which is a judgement about taste rather than a bug fix; the count is
    here so that judgement can be made with a number in hand."""
    claimed = container_seconds(path)
    if (claimed is None or claimed < MIN_CLAIMED_SECONDS
            or got >= claimed * TRUNCATED_FRACTION):
        return None
    available = _ffmpeg_seconds(path, sr)
    if available is None or available <= max(got, 1e-9) * CONFIRM_GAIN:
        return None
    return {"claimed": claimed, "analyzed": got, "available": available}


def _worth_another_decoder(got: float, exc: Exception | None) -> bool:
    """Should ffmpeg be asked as well? Only when this file is a failure as things stand,
    which is what makes the whole change additive: there is no vector here to lose.

    A missing file and a locked file are excluded because a second decoder cannot help
    with either, and on a --retry-failed run with the source drive offline that would be
    one wasted process per track over thousands of tracks."""
    if isinstance(exc, (FileNotFoundError, PermissionError)):
        return False
    if not (exc is not None or got < MIN_SECONDS):
        return False
    return decoder_available()


def load_audio(path: str, sr: int = SR) -> tuple[np.ndarray, bool]:
    """Decode `path` to mono float32 at `sr`, and when that comes back with too little to
    work with, ask ffmpeg directly rather than accepting it.

    Returns (samples, redecoded). `redecoded` is True when the first decoder came back
    short and ffmpeg returned more, which also tells the caller that anything else
    computed from that first read describes a fragment rather than the song. Raises
    whatever the first decoder raised, but only when ffmpeg could not do better, so a
    caller's existing error handling is unchanged.

    The bar is MIN_SECONDS, this module's analysis floor, not the caller's. The CLAP
    embedder needs only 2 seconds, so calling this gives it a second decoder on files it
    would otherwise have embedded as 3 seconds of audio padded out with silence.

    Public because that embedder decodes the same files through the same librosa call and
    has the same hole; see the patch and the request in the DIET2 report."""
    import librosa
    exc = None
    try:
        y, _ = librosa.load(path, sr=sr, mono=True)
    except Exception as e:                   # noqa: BLE001  re-raised below if nothing better
        y, exc = None, e
    got = 0.0 if y is None else len(y) / sr
    if _worth_another_decoder(got, exc):
        # The floor case, unchanged: nothing usable came back, so anything more wins.
        y2 = _ffmpeg_decode(path, sr)
        if y2 is not None and len(y2) > (0 if y is None else len(y)):
            return y2, True
    elif exc is None and _claim_says_short(path, got):
        # The fragment case, added 2026-09-21: the first decoder returned a usable-looking
        # fragment (5.8 seconds of a 504-second song was the worst measured) and the
        # container says most of the song is missing. ffmpeg is asked, and its answer is
        # used ONLY when it beats the first decoder by CONFIRM_GAIN, the same bar
        # short_read_check applies before it will call a read short. That is what keeps
        # the 2026-09-20 decision above intact: a healthy variable-bitrate file whose
        # header overstates its length gets the same audio back from ffmpeg, a few
        # hundred samples of encoder delay apart, and keeps its vector. Only a file that
        # really holds materially more audio than the first decoder found is rewritten.
        y2 = _ffmpeg_decode(path, sr)
        if y2 is not None and len(y2) > len(y) * CONFIRM_GAIN:
            return y2, True
    if exc is not None:
        raise exc
    return (np.empty(0, dtype=np.float32) if y is None else y), False


def _claim_says_short(path: str, got: float) -> bool:
    """The container's stated length nominates a fragment; it never decides one.

    True when the file claims at least MIN_CLAIMED_SECONDS and the first decoder returned
    under TRUNCATED_FRACTION of that. The caller then asks ffmpeg and believes it only if
    it beats the first read by CONFIRM_GAIN. Without a decoder on the machine the answer
    is False, because there is nobody to ask."""
    if not decoder_available():
        return False
    claimed = container_seconds(path)
    return (claimed is not None and claimed >= MIN_CLAIMED_SECONDS
            and got < claimed * TRUNCATED_FRACTION)


def _short_error(path: str, got: float) -> str:
    """The reason stored when there is too little audio to analyze, saying which of the
    two very different things happened: a file that really is a few seconds long, or a
    whole song that nothing on this PC could decode. The container is read here rather
    than on the way in, because this is the only place its answer is used and a failure
    is one file in twenty rather than every file.

    Whatever the wording, a short decode with no ffmpeg on the machine carries the
    NEEDS_DECODER prefix, so installing one retries it. Leaving the prefix off any branch
    would strand exactly the files ffmpeg is best at."""
    claimed = container_seconds(path)
    try:
        mb = os.path.getsize(path) / (1024 * 1024)
    except OSError:
        mb = None
    if (claimed is not None and claimed >= MIN_CLAIMED_SECONDS
            and got < claimed * TRUNCATED_FRACTION):
        what = (f"the file says it holds {claimed:.0f} seconds of audio but only "
                f"{got:.1f} of them could be decoded, so it is damaged")
    elif claimed is not None:
        # The header and the decoder agree, so the file really is this short. A 4-second
        # hi-res flac is a couple of megabytes, which is why size alone cannot be trusted
        # to call a file damaged.
        what = f"only {got:.1f} seconds of audio could be decoded, too little to analyze"
    elif mb is not None and mb >= 1.0:
        what = (f"only {got:.1f} seconds of audio could be decoded from a {mb:.1f} MB "
                f"file, so it is damaged")
    else:
        what = f"only {got:.1f} seconds of audio could be decoded, too little to analyze"
    if not decoder_available():
        return (f"{NEEDS_DECODER} {what}. ffmpeg can usually read a damaged file: "
                f"{_decoder_hint()}.")
    return f"{what}. ffmpeg was tried as well and could get no further."


def _load_error(path: str, exc: Exception) -> str:
    """Turn a librosa.load failure into a sentence a person can act on."""
    name = exc.__class__.__name__
    ext = os.path.splitext(path)[1].lower() or "this"
    if isinstance(exc, FileNotFoundError):
        return "the file is not there any more - it was moved, renamed or deleted"
    if isinstance(exc, PermissionError):
        return "Windows would not let Attune open the file - it may be in use by another program"
    # audioread raises NoBackendError when every decoder it has refused the file, and
    # DecodeError when one accepted it and then failed. soundfile's own error surfaces
    # when librosa never got as far as a fallback.
    if name in ("NoBackendError", "DecodeError", "SoundFileRuntimeError", "LibsndfileError"):
        if not decoder_available():
            # Two different situations, and saying the wrong one is worse than saying
            # nothing. m4a/aac/wma genuinely need ffmpeg. An mp3 or flac reaching here
            # is a file Attune normally reads perfectly well, so blaming the format
            # would be false and would make a person doubt their whole library.
            if ext in _FFMPEG_FORMATS:
                return (f"{NEEDS_DECODER} {ext} files need ffmpeg, which Attune does not "
                        f"carry. {_decoder_hint().capitalize()}.")
            return (f"{NEEDS_DECODER} Attune could not read this {ext} file on its own; "
                    f"it is probably damaged. ffmpeg can usually read a damaged file: "
                    f"{_decoder_hint()}.")
        if ext in _FFMPEG_FORMATS:
            return (f"ffmpeg is installed but could not read this {ext} file. "
                    f"It is probably damaged or copy-protected.")
        return (f"nothing on this PC could read this {ext} file, ffmpeg included. "
                f"It is probably damaged.")
    # FABLE: an EOFError from a truncated file carries no message, so the old line
    # rendered as "could not read the file (EOFError: )" - the exact empty-detail
    # failure this function exists to remove.
    if isinstance(exc, EOFError) or not str(exc).strip():
        return "the file is empty or cut short - copy it again from wherever it came from"
    # An exception librosa does not recognise as a decode failure, so it never reached a
    # fallback by itself. load_audio has already given ffmpeg a turn whenever there is one
    # to give, which is what lets this say plainly that everything was tried. The class
    # and message stay on the end because they are the only clue to a new failure mode.
    if decoder_available():
        return (f"nothing on this PC could read this file, ffmpeg included - it is "
                f"probably damaged ({name}: {exc})").strip()
    return f"could not read the file ({name}: {exc})".strip()


def extract(path: str) -> dict | None:
    """Return {'vec': float32[79], 'tempo': float, 'seconds': float, 'redecoded': bool}
    or {'error': str}. `redecoded` tells the caller the first decoder had to be overruled,
    which also invalidates anything else computed from that decode."""
    import librosa
    sr = SR
    try:
        y, redecoded = load_audio(path, sr)
    except Exception as e:
        return {"error": _load_error(path, e)}
    if len(y) < sr * MIN_SECONDS:
        return {"error": _short_error(path, len(y) / sr)}

    dur = len(y) / sr
    # central window
    if dur > CENTER_SECONDS:
        start = int((dur - CENTER_SECONDS) / 2 * sr)
        y = y[start:start + int(CENTER_SECONDS * sr)]

    try:
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=N_MFCC)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        contrast = librosa.feature.spectral_contrast(y=y, sr=sr)
        centroid = librosa.feature.spectral_centroid(y=y, sr=sr)
        bandwidth = librosa.feature.spectral_bandwidth(y=y, sr=sr)
        rolloff = librosa.feature.spectral_rolloff(y=y, sr=sr)
        zcr = librosa.feature.zero_crossing_rate(y)
        rms = librosa.feature.rms(y=y)
        flatness = librosa.feature.spectral_flatness(y=y)
        tempo = float(np.atleast_1d(librosa.feature.tempo(y=y, sr=sr))[0])
    except Exception as e:
        return {"error": f"the audio decoded but the analysis failed ({e.__class__.__name__}: {e})"}

    vec = np.concatenate([
        mfcc.mean(axis=1), mfcc.std(axis=1),                       # 40
        chroma.mean(axis=1), chroma.std(axis=1),                   # 24
        contrast.mean(axis=1),                                     # 7
        np.array([
            float(centroid.mean()),
            float(bandwidth.mean()),
            float(rolloff.mean()),
            float(zcr.mean()),
            float(rms.mean()),
            float(rms.std()),
            float(flatness.mean()),
        ]),                                                        # 7
        np.array([tempo]),                                         # 1
    ]).astype(np.float32)

    if vec.shape[0] != FEATURE_DIM:
        return {"error": f"the analysis produced {vec.shape[0]} numbers, not the {FEATURE_DIM} the engine expects"}
    if not np.all(np.isfinite(vec)):
        vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)
    out = {"vec": vec, "tempo": tempo, "seconds": dur, "redecoded": redecoded}
    if not redecoded:
        # Skipped when the audio was redecoded, because that path already took the best
        # of both readers and there is nothing left to warn about.
        short = short_read_check(path, dur, sr)
        if short:
            out["short_read"] = short
    return out


if __name__ == "__main__":
    import sys
    r = extract(sys.argv[1])
    if r and "vec" in r:
        print(f"dim={r['vec'].shape[0]} tempo={r['tempo']:.1f} dur={r['seconds']:.0f}s"
              + (" (first decoder returned a fragment; ffmpeg was used)"
                 if r.get("redecoded") else ""))
        print("vec[:8]=", np.round(r["vec"][:8], 3))
    else:
        print("FAILED:", r)
