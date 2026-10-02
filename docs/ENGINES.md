# Attune's engines

Three ways to rank tracks by similarity. They share the same catalog and the same pool of
tracks, so playback and export never care which one picked a row. They differ in what signal
they rank on.

Pick one in Preferences, under Mixing, with the **Engine** field, or on the command line with
`--engine`.

## 1. The default: the sound profile, or V2

`src/engine.py`, class `V2Engine`, built on `src/hybrid.py`. It starts from one of two
recipes, and the library decides which. A library records which copy of the CLAP model made
its fingerprints: one made by Attune 0.2 or later says the trained weights and gets the
**sound profile**; one made by 0.1.x says nothing and keeps **V2**, scored exactly as before.
The two kinds of fingerprint are unrelated numbers (every version up to 0.1.2 analysed with a
copy of the model that had never been trained; [NOTICE.md](../NOTICE.md) section 8), so a
library never holds both, and an older one is brought across under Preferences, Library,
Sound fingerprints.

The window starts its five dials from the recipe the library gets, and the header says which:
**Attune sound profile** or **Attune V2**.

### The sound profile, for a library analysed by 0.2 or later

Each ingredient is put on one common scale, its distance from its own average in units of
its own spread, before its weight applies:

```
fingerprint through the trained head   1.0
genre overlap                          0.2
year gap                               1.5
Earfeel                                0.25
artist links                           0.12   (only in a library that holds that table)
timbre descriptor, tempo               0
```

The **trained head** (`src/models/sound_head.npz`, 3 MB) reshapes the 512 numbers of a
fingerprint into 256 so that songs by artists ListenBrainz listeners play together sit
closer. **Earfeel** is five sensations measured from the sound itself, never from genre:
Pulse, Glow, Heat, Voice and Grain (`src/models/earfeel.json`). The dials start at CLAP 1.0,
Timbre 0, Genre 0.2, Tempo 0, Era 1.5, and Presets offers **Sound profile (default)**.

**How it was chosen, and what that does not tell you.** Not by ear. On the owner's ruling for
this work, over 150 versions were scored on two things that know nothing of the sound: which
artists real listeners play together, and the catalog's styles, with the artists used for
scoring held out of everything trained. On a library of about 21,000 songs, of the 50 songs
each version picked by other artists, 48.8 in 100 were by an artist listeners play with the
seed's artist for this profile, against 33.1 for Attune as released up to 0.1.2 and 16.3 for
random picks. That is evidence it finds music people like together. It is not a listening
test, and none has been sat on it yet.

### V2, for a library analysed by 0.1.x

Blends the **CLAP embedding**, the ears, with the librosa descriptor and a few plain musical
facts:

```
score = w_clap  * cosine(CLAP)
      + w_lib   * librosa timbre similarity
      + w_genre * genre overlap
      - w_bpm   * |tempo gap| / 40          (LINEAR, not octave-folded)
      - w_era   * |year gap| / 25
      + w_key   * key compatibility         (OFF by default)
```

The shipped weights, `DEFAULT_WEIGHTS` in `src/hybrid.py`, are
`clap 1.0, lib 0.4, genre 0.3, bpm 0.3, era 0.1, key 0.0`. Those are the five sliders in the
mix panel: **CLAP**, **Timbre**, **Genre**, **Tempo**, **Era**, with **Presets** to set them
together. Two tick boxes sit with them: **MMR variety**, which stops the result filling up
with near-duplicates of each other, and **Flow ordering**, which arranges the mix rather than
just ranking it.

**Two things that are in the code and switched off.** `hybrid.py`'s own notes say why, and
they are worth reading before anyone turns them on again:

- **Key compatibility** is a weight of zero. The idea was sound, it lost by ear, and its
  implementation was reversed until that was fixed. It is available now that the formula is
  correct, but it does not ship on.
- **Tempo is a linear gap, not octave-folded.** Folding 87 and 174 BPM together is an
  appealing idea that nobody has ever sat down and listened to, so it is deliberately kept
  out of the shipped default. The folded form exists in an offline metric.

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

**Where its judgment comes from, said plainly.** It was trained to reproduce **MusicIP's**
rankings over the author's own library, obtained through MusicIP's local HTTP interface on
his own audio. No MusicIP code or data is in this repository, but the weights encode
another program's judgments, and that is a thing to disclose rather than leave for somebody
to find in a source comment. [NOTICE.md](../NOTICE.md) section 4 is the full account.

**Selectable from the command line, and no longer offered in Preferences.** It is built and
it runs. It was removed from the Preferences engine list on 2026-09-21 because choosing it
there did not actually select it: every mix still came out as the ordinary V2 mix, and the
setting also switched off Radio, Blend, Adventure, thumbs-up steering and Explain, which the
learned engine does not provide. So it promised a different mix, delivered the same one, and
quietly removed five things that worked.

What it has never had is a blind listening test against V2. Until somebody sits down and
listens, it stays optional.

**It does not start on a library analysed by 0.2 or later.** Its head was trained on
fingerprints of the earlier kind, so `--engine learned` on a library with trained-weight
fingerprints stops with a sentence saying so.

## The librosa-only engine

`src/mixer.py` still exists and still works: it ranks on the 79-number acoustic descriptor
alone, z-scored, by weighted Euclidean distance, with the same style and variety knobs. It
needs no model and no ONNX runtime.

It is not one of the three engines the app offers. It is the from-source baseline, useful if
you don't want to fetch the 267 MiB model, and it's what `attune-mix` runs.

## How we know V2 is the one

This is V2's test. The sound profile has not been through one yet.

This is the one round written up in full; the same test was sat about twenty times, five seeds
a round, five to seven engines each, ranked by ear, and the README says how it was run. In the
written-up round six engines were mixed on the same five seeds and rated, partly blind: genuine MusicIP,
the librosa engine and raw CLAP with no rules were shuffled and unlabelled, while the two tuned
hybrids and a larger neural model that was later dropped were labelled. What came out of it:

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

**And the metric never decides, with one stated exception.** Retrieval scores are reported,
never used to pick what ships. A number that rewards more-of-the-same is exactly the number
that will tell you a boring playlist is a good one. That is why the learned engine is not the
default. The exception is the sound profile, picked on scores by the owner's ruling, and it
stays provisional until it has been listened to.

## Which should you use?

- **Just use the default.** Your library picks the recipe; you don't have to.
- **Still running MusicIP?** Try the adapter and compare. They're genuinely different and
  the old one is genuinely good.
- **Curious?** `--engine learned` and tell us what you hear.
