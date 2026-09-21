# Installing Attune

Three ways in, depending on what you want.

1. **[The installer](#1-the-installer)** if you just want to use Attune. This is the one
   almost everybody wants.
2. **[From source](#2-from-source)** if you want the Python package: the command-line
   mixer, `.m3u` export and Plex, out of a checkout.
3. **[Development](#3-development)** if you want to change Attune or build the app yourself.

---

## 1. The installer

Windows 10 or 11. Nothing else needed. No Python, no ffmpeg, no administrator password.

1. Download **AttuneSetup-0.1.0.exe** from the
   [Releases page](https://github.com/Maestro8484/attune/releases).
2. Run it. Windows shows **"Windows protected your PC"** because the file isn't
   code-signed. Click **More info**, then **Run anyway**.
3. It installs into `%LOCALAPPDATA%\Programs\Attune` and adds an **Attune** entry to the
   Start menu. A desktop shortcut is offered as a tick box and is off by default.

Prefer not to install anything? Take **Attune-0.1.0-win64.zip** from the same page instead,
unzip it anywhere, and run `Attune.exe` from the folder.

Either way, what happens next is in [docs/FIRST_RUN.md](docs/FIRST_RUN.md).

### Checking the download

`SHA256SUMS.txt` on the Releases page carries a checksum for both files.

```
Get-FileHash .\AttuneSetup-0.1.0.exe -Algorithm SHA256
```

With Git Bash, WSL, macOS or Linux, `sha256sum -c SHA256SUMS.txt` checks both at once.

### Removing it

Settings, then Apps, then Attune, then Uninstall. Or the **Uninstall Attune** entry in the
Start menu. It removes the program and deliberately leaves your settings and your analysed
library alone, in `%APPDATA%\Attune`. Delete that folder by hand if you want no trace.

If you used the portable zip, delete the folder.

---

## 2. From source

For the Python package rather than the app: the command-line tools, `.m3u` export and the
Plex integration, running out of a checkout. Works anywhere Python does, though only
Windows has been exercised.

### What you need

- **Python 3.10 or newer.**
- **ffmpeg, only if you want m4a, aac or wma.** Everything else decodes through libsndfile,
  which arrives with the `soundfile` wheel. The installed app bundles its own ffmpeg; a
  source checkout does not, so put `ffmpeg` on your PATH if you need those three
  (`winget install ffmpeg`, `brew install ffmpeg`, `apt install ffmpeg`).

### Install

```
git clone https://github.com/Maestro8484/attune.git
cd attune
python -m venv .venv
.venv\Scripts\activate
pip install -e .[app]
```

`source .venv/bin/activate` on macOS and Linux.

**Install the `[app]` extra, not the bare package.** The bare `pip install -e .` gives you
the librosa engine and nothing to read tags with, so every track imports without an artist
or a title. `[app]` adds the tag reader, the web UI and the ONNX runtime the good engine
needs. The other extras:

```
pip install -e .[bridge]   # the LAN web UI over a live MusicIP Mixer
pip install -e .[dev]      # pytest
pip install -e .[neural]   # PyTorch, only if you want to re-derive embeddings the slow way
```

### Get the model

The neural model the default engine uses is **not in the repository**. It's 263 MiB, and
keeping it out is what makes a clone small and fast.

```
python tools/fetch_model.py
```

That downloads it from the project's own release assets, checks it against a pinned SHA-256,
and puts it in `src/models/`. `--check` verifies what's already there and downloads nothing.
`--force` fetches again over a good copy.

Without the model the librosa engine still works, and the CLAP-based default does not.

### Use it

```
attune-scan import-folder "D:\Music"      # walk the folder, read tags
attune-scan analyze --workers 6           # listen to each track, once
attune-scan stats                         # how far along it is
attune-mix --seed "D:\Music\Artist\Track.mp3" --size 25 --style 40 --variety 3
```

`attune-mix` is the librosa engine and works as soon as `analyze` has finished.

For the engine the app actually uses, add the embeddings and mix with `attune-hybrid`:

```
attune-embed-onnx --db data/mixer.db      # needs the model fetched above
attune-hybrid --seed "D:\Music\Artist\Track.mp3" --size 25
```

`attune-embed-onnx` is the torch-free embedder and is the one to use. There's also
`attune-embed`, which does the same job through PyTorch and needs the `[neural]` extra and
about 2.5 GB of downloads. It exists as the reference implementation; you don't need it.

### The web UI from a checkout

```
python web/app.py --db data/mixer.db --port 8778
```

Then open `http://127.0.0.1:8778/`. `python desktop/app_desktop.py` wraps that same UI in a
window instead of a browser tab.

### The LAN bridge

Only useful if you still have a working MusicIP Mixer. See
[bridge/README.md](bridge/README.md).

---

## 3. Development

Everything in section 2, plus the tests and, if you want it, the build.

### Tests

```
pip install -e .[dev]
pytest tests -q -rs
```

On a clean clone this gives **97 passed, 40 skipped** (measured 2026-09-21). The 40 skips
are all in `test_key_compat.py` and are correct: those cases exercise a module that lives in
the author's private research workspace, not in this repository, so they can't run anywhere
else and skip loudly rather than failing.

**You don't need the model to run the tests.** None of them load it.

### Building the Windows app

```
pip install -r desktop/requirements-build.txt
python desktop/build.py
```

That produces `dist/Attune/`, holding `Attune.exe` and a nested `analyzer/` program.

**One thing a fresh clone doesn't have.** The build needs `desktop/ffbin/ffmpeg.exe` and
that folder is deliberately git-ignored, because the binary is about 148 MB and has no
business in a source tree. So a fresh clone or a git worktree never has it, and
`desktop/build.py` **refuses to build without it** rather than quietly shipping an app that
can't read some of your music. Download an ffmpeg build for Windows, put `ffmpeg.exe` in
`desktop/ffbin/`, and build again. To build deliberately without it, pass `--no-ffmpeg`, and
understand that roughly one mp3 in fourteen from a real library then fails to analyse.

`--gui-only` rebuilds just `Attune.exe` and keeps the analyzer that's already there, which is
much faster while you're working on the UI.

### Making a release

See [docs/RELEASING.md](docs/RELEASING.md).
