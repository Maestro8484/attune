# Attune's engines

Three ways to rank tracks by similarity. They share the same catalog and the same pool of
tracks, so playback and export never care which one picked a row. They differ in what signal
they rank on.

Pick one in Preferences, under Mixing, with the **Engine** field, or on the command line with
`--engine`.

## 1. V2, the default

`src/engine.py`, class `V2Engine`, built on `src/hybrid.py`.

Blends a music-trained **CLAP embedding**, the ears, with **music-theory constraints**, the
rules:

```
score = w_clap  * cosine(CLAP)
      + w_genre * genre overlap
      + w_key   * key compatibility     (circle of fifths, plus relative major/minor)
      - w_bpm   * folded tempo distance (87 and 174 BPM count as the same)
      - w_era   * year gap
```

Those weights are the five sliders in the mix panel: CLAP, Timbre, Genre, Tempo, Era. The
**Similarity** and **Variety** knobs sit on top: Similarity decides how tight the match has
to be, Variety widens the candidate pool and samples from it with a rank-decaying
probability, so the same seed gives a different mix each time.

Needs no PyTorch. The embedding is computed once per track by `embed_onnx.py` and read out
of the database afterwards.

## 2. MusicIP adapter

`src/engine.py`, class `MusicIPAdapter`.

If you still have a working MusicIP Mixer, Attune will drive it: it asks the original engine
for the mix on `http://localhost:10002` and resolves the answer back onto its own pool.
Authentic 2008 output, in a 2026 window.

MusicIP's API has to be switched on by hand every time MusicIP launches: File, Preferences,
Services, tick API, Start. With `--engine auto` Attune probes for it at startup and uses V2
if nothing answers.

MusicIP is closed third-party software. Attune neither bundles it nor launches it.

## 3. Learned metric

`src/engine.py`, class `LearnedEngine`.

A small ONNX network distilled onto the CLAP features already in the database, replacing the
hand-tuned weights above with a learned distance. `--engine learned` selects it.

**Selectable, not the default.** It is built, it runs, and its output was checked against
the reference implementation it was distilled from and matched. What it has not had is a
blind listening test against V2, and in this project a retrieval number never decides what
ships. Until somebody sits down and listens, V2 stays the default.

## The librosa-only engine

`src/mixer.py` still exists and still works: it ranks on the 79-number acoustic descriptor
alone, z-scored, by weighted Euclidean distance, with the same style and variety knobs. It
needs no model and no ONNX runtime.

It is not one of the three engines the app offers. It is the from-source baseline, useful if
you don't want to fetch the 263 MiB model, and it's what `attune-mix` runs.

## How we know V2 is the one

Five engines were mixed on the same seeds and presented blind: genuine MusicIP, the librosa
engine, raw CLAP with no rules, and two tuned hybrids. What came out of it:

- **Raw CLAP alone was the weakest.** Neural ears with no rules wander across tempo, era and
  key, and make matches that are acoustically defensible and musically wrong.
- **Adding the music-theory constraints fixed that,** and pulled level with or ahead of
  MusicIP by ear.
- **The hand-crafted spectral vector contributed nothing** once tempo and key were explicit.
  Every tuned configuration drove its weight to zero. So V2 drops it: CLAP plus theory is the
  whole engine.

The lesson worth keeping: the winning architecture was not more or better acoustic
statistics. It was learned perceptual similarity plus a few hard musical rules.
Tempo-octave folding and key compatibility are cheap, interpretable, and did more for
playlist coherence than any amount of spectral feature engineering.

**And the metric never decides.** Retrieval scores are reported, never used to pick what
ships. A number that rewards more-of-the-same is exactly the number that will tell you a
boring playlist is a good one. That is why the learned engine is not the default yet.

## Which should you use?

- **Just use V2.** It's the default for a reason.
- **Still running MusicIP?** Try the adapter and compare. They're genuinely different and
  the old one is genuinely good.
- **Curious?** `--engine learned` and tell us what you hear.
