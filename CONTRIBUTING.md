# Contributing to Attune

Attune is a small, hackable codebase and contributions are welcome. It's also one person's
side project, so please open an issue before doing anything large; I'd rather talk about it
first than turn down work you've already done.

## Ground rules

- **Never commit personal or proprietary data.** No `data/`, no `*.db`, no `library.json`,
  no `groundtruth/`, no `*.m3lib`, no MusicIP binary. A music library database lists every
  file path, album and track name on someone's machine. `.gitignore` guards these and
  `tools/leak_check.py` scans for them, but read `git status` before you push anyway.
- **Keep it local-first.** Anything that needs a cloud account, or phones home, won't be
  merged. Optional integrations with things you host yourself, like Plex, Jellyfin or LMS,
  are welcome.
- **Keep it small.** Prefer a clear function over a framework. No microservices and no
  vector database until a real workload proves one is needed.
- **Taste is judged by ear.** Retrieval scores are worth reporting and are never the reason
  to ship a change to how mixes are ranked. A metric that rewards more-of-the-same will
  happily tell you a dull playlist is a good one. If you change the ranking, listen to it,
  and say what you heard.

## Dev setup

```
git clone https://github.com/Maestro8484/attune.git
cd attune
python -m venv .venv
.venv\Scripts\activate
pip install -e .[app,dev]
```

Full detail, including the model and the app build, is in [INSTALL.md](INSTALL.md).

## Tests

```
pytest tests -q -rs
```

On a clean clone that gives **97 passed, 40 skipped**. The skips are correct: those cases
exercise a module that lives in the author's private research workspace rather than in this
repository, so they can't run anywhere else and skip loudly instead of failing.

**You don't need the model to run the tests.** Nothing in `tests/` loads it, so you can skip
`tools/fetch_model.py` entirely if all you want is to run the suite.

Continuous integration runs the same command on `windows-latest`, plus the personal-data
scan. It deliberately does not build the installer.

## Testing a change end to end, without any real music

```
python examples/make_demo_library.py
attune-scan import-folder examples/sample_library
attune-scan analyze --workers 4
attune-mix --seed examples/sample_library/warm_pad_02.wav --size 5 --style 30
```

That generates a tiny library of synthesised WAV files in three distinct sonic families, so
you can check that an engine change still groups the obvious things together without needing
anyone's music. See [examples/README.md](examples/README.md).

## Style

- Standard library plus the listed dependencies. Adding a heavy one needs a reason.
- Match the existing terse-but-commented style. Comments explain *why*, not *what*.
- If you change the feature descriptor, bump `SCHEMA_VERSION` in `src/db.py`, because every
  stored vector becomes stale.
- No em dashes in prose, no emojis. American spelling.

## Open items, for real

These are genuinely not done. Roughly easiest first.

- **Jellyfin export**, the sibling of the Plex integration. `src/export.py` is the shape to
  copy.
- **Stop a library reload from overlapping a scan.** There's a narrow window where both can
  run. It needs one lock shared between `web/scanjob.py` and `web/libreload.py`. Low
  exposure, since folder watching is off by default, but it's a real race.
- **Boot gracefully when the library's drive is unplugged.** Today `web/app.py` stops before
  the window appears, so there's nothing on screen to press. It should start on a default
  library and say loudly which path it couldn't reach, with a button into Preferences.
  Careful: a *missing* file is the only case that should do this. A corrupt or
  wrong-schema database must keep failing closed rather than being quietly replaced.
- **More tests.** Coverage is thin around the settings file, the Plex client and the export
  naming rules.
- **macOS and Linux packaging.** The engine is portable Python. The app, the installer and
  the USB drive detection are Windows-specific.
- **A numba-free feature path.** librosa pulls in numba, which is most of the analyzer's
  size and most of its cold-start time.
- **An approximate-nearest-neighbour index** behind the existing ranking interface, for
  libraries far larger than the tens of thousands this was built for. Brute force is a few
  milliseconds today, so this is not urgent.

## Opening a pull request

Say what changed in behaviour, and how you checked it. "Ran the tests" is fine for most
things. For anything touching how mixes are ranked, say what you listened to.
