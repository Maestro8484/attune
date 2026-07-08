# Contributing to Attune

Thanks for your interest! Attune is a small, hackable codebase and contributions are welcome.

## Ground rules

- **Never commit personal or proprietary data.** No `data/`, `*.db`, `library.json`,
  `groundtruth/`, `*.m3lib`, or any MusicIP binary. See [NOTICE.md](NOTICE.md). `.gitignore`
  guards these, but double-check `git status` before pushing.
- **Keep it local-first.** Features that require a cloud account or phone home won't be
  merged. Optional integrations with self-hosted services (Plex, Jellyfin, LMS) are welcome.
- **Keep it small.** Prefer a clear function over a framework. No microservices, no vector DB,
  unless a real workload proves it's needed.

## Dev setup

```bash
python -m venv .venv && . .venv/Scripts/activate   # or source .venv/bin/activate
pip install -r requirements.txt
```

Ensure `ffmpeg`/`ffprobe` are on your PATH.

## Good first issues / roadmap

- `.m3u` / `.m3u8` playlist export from a mix.
- **Plex** integration: create a playlist via the Plex API from a mix.
- **Jellyfin** integration.
- Optional **ANN backend** (FAISS/hnswlib) behind `mixer.rank()` for very large libraries.
- A small **local web UI** (seed drag-drop, sliders, audio preview).
- Better tag reading (embedded cover art, multi-value genres).
- Alternative descriptors (e.g. optional neural embeddings) behind the same DB schema —
  bump `SCHEMA_VERSION`.
- Package to PyPI (`pip install attune`) with a console entry point.

## Style

- Standard library + the listed deps; avoid adding heavy dependencies.
- Match the existing terse-but-commented style. Comments explain *why*, not *what*.
- If you change the feature descriptor, bump `SCHEMA_VERSION` in `db.py`.

## Testing a change end-to-end

```bash
python src/scan.py import-folder ./examples/sample_library   # or your own folder
python src/scan.py analyze --workers 4
python src/mixer.py --seed "<a path from the catalog>" --size 10 --style 40 --variety 2
```

Open a PR with a clear description of the behavior change and how you verified it.
