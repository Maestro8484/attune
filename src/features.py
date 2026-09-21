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
import numpy as np

SR = 22050
CENTER_SECONDS = 90.0     # analyze up to this many seconds from the middle
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


def decoder_available() -> bool:
    """True when an ffmpeg is on PATH for the audio decoders to shell out to. The
    analyzer puts the user's own folder and any bundled bin on PATH before this runs
    (desktop/worker_entry.py), and scan.py does the same for the ML-venv route."""
    return bool(shutil.which("ffmpeg"))


def user_bin_hint() -> str:
    r"""The folder to tell a person to drop ffmpeg.exe into, as a real path they can
    paste into Explorer rather than the literal text %APPDATA%."""
    base = os.environ.get("APPDATA")
    return os.path.join(base, "Attune", "bin") if base else r"%APPDATA%\Attune\bin"


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
                        f"carry. Put ffmpeg.exe in {user_bin_hint()}, or install ffmpeg "
                        f"on this PC, then rescan - Attune retries these by itself.")
            return (f"{NEEDS_DECODER} Attune could not read this {ext} file on its own; "
                    f"it is probably damaged. ffmpeg can usually read a damaged file: "
                    f"put ffmpeg.exe in {user_bin_hint()} and it will be tried again.")
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
    return f"could not read the file ({name}: {exc})".strip()


def extract(path: str) -> dict | None:
    """Return {'vec': float32[79], 'tempo': float, 'seconds': float} or None on failure."""
    import librosa
    try:
        y, sr = librosa.load(path, sr=SR, mono=True)
    except Exception as e:
        return {"error": _load_error(path, e)}
    if y is None or len(y) < sr * 5:
        return {"error": "the file holds under 5 seconds of audio, too little to analyze"}

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
    return {"vec": vec, "tempo": tempo, "seconds": dur}


if __name__ == "__main__":
    import sys
    r = extract(sys.argv[1])
    if r and "vec" in r:
        print(f"dim={r['vec'].shape[0]} tempo={r['tempo']:.1f} dur={r['seconds']:.0f}s")
        print("vec[:8]=", np.round(r["vec"][:8], 3))
    else:
        print("FAILED:", r)
