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


def extract(path: str) -> dict | None:
    """Return {'vec': float32[79], 'tempo': float, 'seconds': float} or None on failure."""
    import librosa
    try:
        y, sr = librosa.load(path, sr=SR, mono=True)
    except Exception as e:
        return {"error": f"load: {e.__class__.__name__}: {e}"}
    if y is None or len(y) < sr * 5:
        return {"error": "too short / empty"}

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
        return {"error": f"feature: {e.__class__.__name__}: {e}"}

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
        return {"error": f"dim {vec.shape[0]} != {FEATURE_DIM}"}
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
