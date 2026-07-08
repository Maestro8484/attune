# Installing Attune

Attune is a local, single-file-DB tool. Core install runs the librosa engine; the neural
hybrid (V2, the blind-test winner) and the LAN Bridge are optional extras.

## Prerequisites
- **Python 3.10+**
- **ffmpeg / ffprobe on PATH** — used to decode audio and read tags. Not a pip package:
  - Windows: `winget install ffmpeg`   ·   macOS: `brew install ffmpeg`   ·   Linux: `apt install ffmpeg`
- For the neural extra: a GPU is optional but ~10x faster. Install the matching **torch**
  build from https://pytorch.org first (CPU or a CUDA build), then the extra below.

## Install
```bash
python -m venv .venv
. .venv/Scripts/activate        # Windows;  source .venv/bin/activate on macOS/Linux
pip install -e .                # core (librosa engine)
pip install -e .[neural]        # + hybrid V2 engine (torch + transformers)
pip install -e .[bridge]        # + LAN web UI over a live MusicIP
pip install -e .[all]           # everything
```

## Console commands (installed by any of the above)
| Command | What it does | Needs |
|---|---|---|
| `attune-scan import-folder <dir>` | walk a music folder, read tags | core |
| `attune-scan analyze --workers 6`  | extract 79-dim librosa features | core |
| `attune-scan stats`                | progress | core |
| `attune-mix --seed <path> --size 25 --style 40 --variety 3` | librosa-only playlist | core |
| `attune-embed --db data/mixer.db`  | compute CLAP embeddings -> `clap` table | `[neural]` |
| `attune-hybrid --seed <path> --size 25` | **hybrid V2** playlist (CLAP + librosa + rules) | `[neural]` + a `clap` table |

Typical first run:
```bash
attune-scan import-folder "D:\Music"
attune-scan analyze --workers 6
attune-mix --seed "D:\Music\Artist\Track.mp3" --size 25      # works now
attune-embed                                                  # optional: unlock the V2 engine
attune-hybrid --seed "D:\Music\Artist\Track.mp3" --size 25    # the blind-test-winning engine
```

## Bridge (optional LAN web UI)
See `bridge/README.md`. In short: `pip install -e .[bridge]`, copy
`bridge/config.example.json` -> `bridge/config.json`, edit paths, `python bridge/bridge.py`,
open `http://<this-machine-ip>:8765`. A live MusicIP Mixer with its API started is required.

## Verify
```bash
pip install pytest && pytest tests -q      # 136 passing
attune-mix -h                              # entry points resolve
```
