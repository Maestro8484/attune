<h1 align="center">🎧 Attune</h1>

<p align="center">
  <b>Local-first acoustic playlist generator.</b><br>
  Point it at your music. Pick a song you love. Get a whole playlist that <i>sounds like it</i>.
</p>

<p align="center">
  <i>A spiritual successor to the legendary — and long-abandoned — MusicIP Mixer / MusicMagic.</i>
</p>

<p align="center">
  <a href="#quick-start">Quick start</a> ·
  <a href="#how-it-works">How it works</a> ·
  <a href="#self-hosted--plex--jellyfin">Self-hosted</a> ·
  <a href="docs/MUSICIP_HERITAGE.md">Heritage</a> ·
  <a href="#license--legal">License</a>
</p>

<p align="center">
  <img alt="Python 3.10+" src="https://img.shields.io/badge/python-3.10%2B-blue">
  <img alt="License: MIT" src="https://img.shields.io/badge/license-MIT-green">
  <img alt="Local-first" src="https://img.shields.io/badge/cloud-not%20required-brightgreen">
</p>

---

## Why this exists (a true story)

My car plays MP3s straight off a USB stick, by file and folder. So do my kids' music
players, and the boombox. It's the most solid, dependable music setup I've ever owned —
your files, your devices, no cloud middleman, no subscription, nothing to break.

The missing piece was always the *playlist brain*. **MusicIP Mixer** (2005–2010) was that
brain: hand it one song and it built a playlist of tracks that were *acoustically similar*
— not "fans also liked," not tag matching, but actual **sound**. Then the company folded
and it became abandonware. Those of us who kept it alive paid a tax for it: getting one
mix onto a USB stick meant MusicIP → export (it only speaks 2000s-Winamp) → save `.m3u` →
import into MusicBee → rebuild playlist → export files to a folder → copy to the stick.
Five steps 'til Sunday, for every single mix, for fifteen years.

Attune ends that. Seed a song in a browser, get the mix, press one button, pull the stick
out of the port. And when the old engine finally won't run anywhere, Attune's own
open-source analysis engine is here to outlive it.

**Attune rebuilds that magic on open, modern, offline tools.** It analyzes the audio of your
local library, learns each track's sonic character (timbre, harmony, rhythm, texture), and
generates seed-based similarity playlists — entirely on your own machine. No account, no
cloud, no phoning home. Your library and your listening stay yours.

> **Why not just use an audio fingerprinter like Chromaprint/AcoustID?** Because those
> answer *"is this the exact same recording?"* (identification). Attune answers *"what else
> sounds like this?"* (similarity) — a fundamentally different problem that needs descriptive
> acoustic features, not identity hashes. [More on this below](#how-it-works).

---

## Features

- 🎵 **Seed a playlist from any track** — one song in, a coherent queue out.
- 🎚️ **Real tuning knobs**, inherited from MusicIP:
  - **Style** (0–100): strict timbre match ↔ broader stylistic match.
  - **Variety** (0–9): tight nearest-neighbours ↔ adventurous exploration.
  - **Artist spacing**: don't stack the same artist back-to-back.
- 🗄️ **One SQLite file.** No database server, no vector DB, no containers.
- ⚡ **Fast at library scale** — brute-force similarity over tens of thousands of tracks is
  sub-second; the analysis is the only slow part and it's incremental + resumable.
- 🔌 **Standalone** — scan a plain music folder; MusicIP is *not* required.
- 🧪 **Provably faithful** — an optional harness scores Attune against real MusicIP output so
  fidelity is a number, not a vibe. (See [docs/VALIDATION.md](docs/VALIDATION.md).)
- 🏠 **Self-host friendly** — designed to sit next to Plex/Jellyfin on a NAS or home server.

---

## Two ways to use Attune

| | **Bridge mode** ([bridge/](bridge/)) | **Standalone engine** ([src/](src/)) |
|---|---|---|
| Needs | a working MusicIP Mixer install | just Python + ffmpeg |
| Mix quality | the original MusicIP engine itself | Attune's open acoustic analysis |
| Gives you | LAN browser UI, .m3u export, **copy-to-USB with car-stereo naming** | the same, minus the legacy engine |
| For | people still running MusicIP | everyone else |

**Minimum requirements:** Windows 10+ (Bridge device-detection; engine itself is
cross-platform), Python 3.10+, [ffmpeg](https://ffmpeg.org/download.html) on PATH,
~2 GB RAM. No GPU needed. No internet needed after install. Optional AI "discovery
mode" (CLAP embeddings) additionally needs PyTorch (~2.5 GB) and benefits from any
NVIDIA GPU.

## Quick start (standalone engine)

**Prerequisites:** Python 3.10+ and [ffmpeg](https://ffmpeg.org/download.html) on your PATH.

```bash
git clone https://github.com/Maestro8484/attune.git
cd attune
python -m venv .venv && . .venv/Scripts/activate      # (Linux/macOS: source .venv/bin/activate)
pip install -r requirements.txt

# 1. Catalog your music (reads tags via ffprobe)
python src/scan.py import-folder "/path/to/your/music"

# 2. Analyze the audio into acoustic fingerprints (incremental; safe to stop/resume)
python src/scan.py analyze --workers 6

# 3. Make a mix from a seed track
python src/mixer.py --seed "/path/to/your/music/Artist/Album/track.mp3" \
                    --size 25 --style 40 --variety 3
```

Analysis is a one-time cost (~1–3 s per track, parallelized). After that, mixing is instant
and re-runs never re-analyze unchanged files.

### Pro engine (recommended): neural embeddings + music theory

The standalone `mixer.py` above uses hand-crafted acoustic features. The **hybrid engine**
(`hybrid.py`) is better — in a blind listening test against genuine MusicIP on a real 17k-track
library, its tuned config matched or beat the original engine. It blends a music-trained
**CLAP** neural embedding with music-theory constraints:

```bash
pip install torch transformers          # + a CUDA build of torch for GPU (much faster)
python src/embed.py --db data/mixer.db   # one-time neural embedding pass
python src/hybrid.py --db data/mixer.db --seed "/path/to/track.mp3" --size 25
```

Needs PyTorch (~2.5 GB) and benefits from any NVIDIA GPU; without it, the librosa engine
above still works. See [docs/ENGINES.md](docs/ENGINES.md) for how the two compare.

---

## How it works

Attune describes every track with a compact **79-dimension acoustic descriptor**:

| Group | Dims | What it captures |
|------:|:----:|:-----------------|
| **Timbre** | 40 | MFCC mean+std — the dominant "what does it sound like" signal |
| **Harmony** | 24 | Chroma mean+std — key / chord character |
| **Contrast** | 7 | Spectral contrast — tonal vs. noisy energy bands |
| **Texture** | 7 | Centroid, bandwidth, rolloff, zero-crossing, loudness dynamics, flatness |
| **Tempo** | 1 | BPM |

Descriptors are z-scored across your library, then the mixer ranks tracks by weighted
distance from your seed. The **style** knob re-weights timbre vs. everything else; the
**variety** knob widens the candidate pool and samples from it. That's the whole trick —
no black box, no cloud model. See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Self-hosted / Plex / Jellyfin

Attune is built for the home-server crowd. The intended shape:

```
  [ NAS: your music library ]  ──►  [ home server: Attune analyzes + mixes ]  ──►  [ Plex / Jellyfin playlists ]
```

Run the analysis once against your NAS-mounted library, then generate `.m3u` playlists (or,
on the roadmap, push them straight into Plex/Jellyfin via their APIs). This is the same role
the community plugin `lms-mipmixer` played for Logitech Media Server — modernized and
decoupled from any single player. **Roadmap:** `.m3u` export → Plex API integration →
Jellyfin integration → a small local web UI.

---

## Project status

Attune is **early but working**: the analysis engine, mixer, and MusicIP-fidelity validation
harness are functional today. It began as a clean-room modernization project validated
against a real 17k-track MusicIP library. Interfaces may shift before 1.0. Issues and PRs
welcome — see [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License & legal

Attune's code is **MIT-licensed** (see [LICENSE](LICENSE)). It is an independent,
clean-room work — it contains **no MusicIP/MusicMagic code or binaries** and does not
reverse-engineer them. "MusicIP" and "MusicMagic" are third-party marks referenced only to
describe heritage and interoperability; Attune is not affiliated with or endorsed by their
rights holders. Attune analyzes audio files **you already possess**; it neither copies nor
distributes music. Full details and your responsibilities: **[NOTICE.md](NOTICE.md)**.

---

<p align="center"><sub>Built for people who miss the days when your computer actually understood your music.</sub></p>
