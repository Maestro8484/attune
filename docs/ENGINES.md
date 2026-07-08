# Attune's engines

Attune ships three ways to rank tracks by similarity. They share the same SQLite catalog;
they differ in what signal they rank on.

## 1. librosa engine (`mixer.py`) — the no-dependencies baseline

Hand-crafted 79-dim acoustic descriptor (MFCC / chroma / spectral / tempo), z-scored, ranked
by weighted Euclidean distance with `style` / `variety` knobs. Needs only Python + ffmpeg.
Decent, fully offline, tiny. Good enough to be useful; not the best Attune can do.

## 2. hybrid engine (`hybrid.py`) — the recommended "pro" engine

Blends a **music-trained CLAP embedding** (the "ears") with **music-theory constraints**
(the "rules"):

```
score = w_clap·cosine(CLAP)
      + w_genre·genreJaccard
      + w_key·keyCompatibility        (circle of fifths + relative major/minor)
      - w_bpm·foldedTempoDistance      (87 and 174 BPM treated as equal)
      - w_era·|yearGap|
```

Needs PyTorch + transformers (and `embed.py` run once). This is what a blind listening test
found competitive with genuine MusicIP.

## How we know the hybrid is better

We captured real MusicIP mixes for 5 diverse seeds, then ran a **blind A/B/C/V2/V3 test** —
each seed mixed by MusicIP, the librosa engine, raw CLAP, and two tuned hybrids, presented
anonymized. Findings:

- **Raw CLAP alone was the weakest** — it wanders across tempo, era, and key. Neural
  "ears" without rules make musically-incoherent jumps (an acoustic match that's the wrong
  tempo and decade).
- **Adding the music-theory constraints (the hybrid) fixed that** and pulled level with or
  ahead of MusicIP by ear.
- **The old hand-crafted spectral vector contributed nothing once tempo and key were
  explicit** — every tuned config zeroed its weight. So the hybrid drops it: CLAP + theory
  is the whole engine.

### The design lesson

The winning architecture wasn't "more/better acoustic statistics" — it was **learned
perceptual similarity plus a few hard musical rules**. Tempo-octave folding and key
compatibility are cheap, interpretable, and did more for playlist coherence than any amount
of spectral feature engineering.

## Which should you use?

- No GPU / want zero heavy deps → **librosa engine**. It works.
- Want the best mixes and can install PyTorch → **hybrid engine**. Worth it.
- Still running MusicIP → the **Bridge** (`bridge/`) uses the original engine directly,
  which is also excellent — the hybrid is for when you *don't* have MusicIP.
