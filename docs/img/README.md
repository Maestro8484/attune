# Screenshots

Three slots are referenced from the repository's README, and they're commented out there so
that a missing file never renders as a broken image on the GitHub page. Capture the images,
drop them in here, and delete the `<!-- SCREENSHOT SLOT ... -->` wrapper around each block.

| File | What it shows | Width in the README |
|---|---|---|
| `studio-main.png` | The main window: library list on the left, a finished mix on the right, something playing | 820 |
| `first-run.png` | The **Welcome to Attune** window with a folder picked and **Scan my music** visible | 620 |
| `export.png` | The export panel with **Folder / USB** filled in and **Layout** showing | 620 |

## Rules for capturing them

**Never use the owner's real library.** Album and track names are personal. Capture against
a scratch library instead: `python examples/make_demo_library.py` builds a small synthetic
one with no copyrighted audio in it, or use a folder of your own test files.

**Point `APPDATA` at an empty folder** before launching, so the capture shows a clean app and
not somebody's saved settings.

**Capture from the built app**, not the dev server in a browser tab. The window chrome is
part of what a reader is trying to recognise.

**Default theme, default window size.** A reader should see what they will get.

**Check every pixel of text before committing.** A file path, a server address, a Plex
library name or a track title can all sit in a corner of a screenshot and go straight onto a
public page. `tools/leak_check.py` reads text, not images, so it will not catch this for you.

Save as PNG. Keep each one under about 400 KB.
