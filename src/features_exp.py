"""EXPERIMENTAL extended descriptor: adds delta-MFCC (temporal timbre dynamics) and
delta-chroma to the base features. Tests whether temporal information closes the gap
to MusicIP. Kept separate from the shipped features.py until proven.

dim = mfcc(40) + dmfcc(40) + chroma(24) + dchroma(24) + contrast(7) + texture(7) + tempo(1) = 143
"""
from __future__ import annotations
import numpy as np

SR = 22050
CENTER_SECONDS = 120.0
N_MFCC = 20
FEATURE_DIM = 143

FEATURE_GROUPS = {
    "timbre":   (0, 40),
    "dtimbre":  (40, 80),
    "harmony":  (80, 104),
    "dharmony": (104, 128),
    "contrast": (128, 135),
    "texture":  (135, 142),
    "tempo":    (142, 143),
}


def extract(path):
    import librosa
    try:
        y, sr = librosa.load(path, sr=SR, mono=True)
    except Exception as e:
        return {"error": f"load: {e.__class__.__name__}: {e}"}
    if y is None or len(y) < sr * 5:
        return {"error": "too short / empty"}
    dur = len(y) / sr
    if dur > CENTER_SECONDS:
        start = int((dur - CENTER_SECONDS) / 2 * sr)
        y = y[start:start + int(CENTER_SECONDS * sr)]
    try:
        mfcc = librosa.feature.mfcc(y=y, sr=sr, n_mfcc=N_MFCC)
        dmfcc = librosa.feature.delta(mfcc)
        chroma = librosa.feature.chroma_cqt(y=y, sr=sr)
        dchroma = librosa.feature.delta(chroma)
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
        mfcc.mean(1), mfcc.std(1),
        dmfcc.mean(1), dmfcc.std(1),
        chroma.mean(1), chroma.std(1),
        dchroma.mean(1), dchroma.std(1),
        contrast.mean(1),
        np.array([centroid.mean(), bandwidth.mean(), rolloff.mean(),
                  zcr.mean(), rms.mean(), rms.std(), flatness.mean()]),
        np.array([tempo]),
    ]).astype(np.float32)
    if vec.shape[0] != FEATURE_DIM:
        return {"error": f"dim {vec.shape[0]} != {FEATURE_DIM}"}
    if not np.all(np.isfinite(vec)):
        vec = np.nan_to_num(vec, nan=0.0, posinf=0.0, neginf=0.0)
    return {"vec": vec, "tempo": tempo, "seconds": dur}
