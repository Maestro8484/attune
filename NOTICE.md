# Notices, Legal Considerations & Attributions

This document exists so contributors and users understand exactly what Attune is,
what it is not, and where the legal lines are. It is informational, not legal advice.

## 1. Attune's own code

- Licensed **MIT** ([LICENSE](LICENSE)) — permissive; use, modify, redistribute freely
  with attribution and no warranty.
- Written as an **independent, clean-room implementation**. It contains **no source code,
  binaries, decompiled output, or data files from MusicIP / MusicMagic / Predixis**.

## 2. Relationship to MusicIP / MusicMagic (the important part)

Attune is inspired by, and aims to be a spiritual successor to, the discontinued **MusicIP
Mixer** (formerly *MusicMagic Mixer*, by Predixis/MusicIP). To be unambiguous:

- **Not affiliated, not endorsed.** "MusicIP", "MusicMagic", and "Predixis" are trademarks
  of their respective owners. They are used here only *nominatively* — to describe what
  Attune is compatible with and descended from. This is standard nominative fair use and
  does not imply any endorsement or partnership.
- **No proprietary code or binaries are included or redistributed.** Do not commit
  `MusicMagicMixer.exe`, `mipcore.exe`, `genpuid`, `*.m3lib`, `register.key`, `client.pem`,
  or any other MusicIP asset to this repository. `.gitignore` blocks the common ones as a
  safety net, but the responsibility is yours.
- **No reverse-engineering of MusicIP binaries** is performed by Attune. The engine is built
  from open, well-documented signal-processing techniques (MFCC, chroma, spectral features),
  not by disassembling anyone's program.
- **The optional MusicIP-interop tools** in [`tools/`](tools/) talk to MusicIP's *own local
  HTTP API* (the `localhost:10002` interface, publicly documented for years by the
  Squeezebox/Logitech Media Server community). Using a program's own network API to read
  your own library's data is ordinary interoperability — it does not copy or modify the
  program. These tools are for users who still run MusicIP and want to (a) migrate their
  catalog or (b) benchmark Attune's fidelity. They are not required to use Attune.

> If you are a rights holder to MusicIP/MusicMagic/Predixis IP and have a concern, please
> open an issue — this project is built in good faith and will address problems promptly.

## 3. Your music library

- Attune only reads audio files **you already have on your own storage**. It does not
  download, upload, share, or redistribute music.
- Whether you have the right to possess and analyze those files is **your responsibility**,
  governed by where you live and how you obtained them. Attune takes no position and stores
  nothing off your machine.
- Attune extracts **acoustic feature vectors** (numeric descriptors) and tag metadata into a
  local SQLite file. These derived features are not the music and cannot reconstruct it.

## 4. Do not publish personal / derived data

When sharing forks, screenshots, or bug reports, do **not** include:
- Your `data/` directory, `*.db` files, or `library.json` — they contain your library's file
  paths and metadata (personal information).
- Any **ground-truth capture** (`groundtruth/`) — it is derived from running MusicIP over
  *your* library and reveals its contents. Keep it local.

`.gitignore` excludes all of the above by default.

## 5. Third-party dependencies & their licenses

Attune depends on (does not vendor) the following, all under permissive licenses:

| Dependency | License | Used for |
|---|---|---|
| [librosa](https://librosa.org) | ISC | audio feature extraction |
| [NumPy](https://numpy.org) | BSD-3-Clause | array math |
| [SciPy](https://scipy.org) | BSD-3-Clause | signal processing |
| [SoundFile](https://github.com/bastibe/python-soundfile) | BSD-2-Clause | audio I/O |
| librosa's transitive deps (numba, audioread, etc.) | BSD/MIT-family | — |

**ffmpeg / ffprobe** (LGPL/GPL) is an *external program*, not a Python library linked into
this code. Attune invokes it as a separate process (the same way you'd run it from a shell),
which does not create a derivative work of ffmpeg or impose its license on Attune. Users
install ffmpeg themselves.

## 6. Patents

Attune uses long-established, textbook signal-processing methods (MFCC, chroma, spectral
descriptors) that predate and are independent of any MusicIP/Predixis acoustic-analysis
patents. It deliberately does **not** implement or practice MusicIP's proprietary
fingerprinting algorithm. As with all software, Attune is provided without any patent
warranty (see the MIT license's "AS IS" clause).

## 7. Before you flip the repo public — checklist

- [ ] Set the real copyright holder in [LICENSE](LICENSE) (replace "Attune contributors").
- [ ] Confirm `git status` shows **no** `data/`, `*.db`, `*.m3lib`, `library.json`,
      `groundtruth/`, or MusicIP binaries staged.
- [ ] `git ls-files | grep -Ei 'm3lib|\.db$|library\.json|groundtruth|register\.key'`
      returns nothing.
- [ ] You're comfortable with MIT (permissive). If you'd prefer copyleft, swap to GPL-3.0
      before the first public commit.
- [ ] Run the local leak checker against `.leakpatterns` (personal paths, emails, tokens):
      `python tools/leak_check.py`. It must report clean.
- [ ] **Pickaxe the FULL history**, not just the working tree — a secret removed from HEAD
      still lives in old commits: `git log -p -S '<email-or-token>' --all`. If anything is
      found, the repo history must be rewritten/reset before publishing (this is what bit us
      last time — an email that was clean at HEAD but present in history).
