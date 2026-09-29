# GUI assessment, 2026-09-28: export is the payoff, so the window now says so

One session, branch `gui/export-discoverability`. This file records what was found in the running app, what was changed and why, and what was left alone. Pictures are in `docs/screens2026-09-28/before/` and `docs/screens2026-09-28/after/`, taken from the app at 1400 by 860 with private names and paths replaced.

**This file is a record of one moment, not standing truth.** Staleness check:

```
git log -1 --oneline
git log -1 --oneline -- docs/GUI-ASSESS-2026-09-28.md
```

Same commit, nothing has landed since. Different, the window may have moved on.

## The owner's framing, and the reframing

The brief said export was buried and took "a minute or two of clicking to find" after a playlist was tuned. Measured in the app before any change: from a finished mix, the Plex playlist was two clicks away (Export..., then Create Plex playlist) and the USB copy was one click plus the folder picker. So the depth was never the problem.

What was actually wrong, read off the before picture: the two payoff actions were two of four small grey buttons on the header line above the list, the same size and colour as Save as new, and neither button said where the list would go. Export... is a verb without a destination; Plex appeared nowhere on screen until a panel opened, and that panel stacked three unrelated jobs (playlist files, copying files, mirroring a folder to Plex) in one tall scroll with eight buttons. The eye had nothing to land on, and the word it needed was hidden one layer down.

So the fix is weight and naming, not depth. The destination has to be named on the button itself, and the button has to be the thing on the pane that looks different.

## What changed

**A SEND TO bar along the foot of the list pane.** This is where a Winamp 2.x playlist window keeps its row of chunky buttons and its little time readout, so a Winamp user recognises the arrangement at once. It appears only while the list on screen is a playlist (a mix, Now Playing, an opened playlist, an auto-playlist), the same condition that already governed the Play and Save row, so it shows up the moment there is something worth sending and is absent from the Library.

Left to right:

- **A green LCD readout**: `101 TRACKS  7:21:27`. How many songs and how long, in the same display font as the player.
- **SEND TO**, then two amber bevelled buttons, the only accent-coloured controls on the pane: **PLEX PLAYLIST** and **USB / CAR FOLDER**. Each names its destination and each is one click. Plex writes the playlist straight away, named as before ("Attune, like <song>" for a mix, the list's own name otherwise). USB opens the existing folder picker at the folder last used and starts the copy on OK, with a thin progress bar in the same strip. The chosen folder is remembered on this PC.
- **More...** opens the old Export panel, rising from the bar instead of dropping from the top of the window. It keeps everything it held: download a playlist file, save to the playlist folder, path style, and the folder-to-Plex mirror. Nothing was removed.
- **A result line** in the bar itself: "On Plex now: 'Attune, like 2 Unlimited - Twilight Zone', 21 of 21 songs" or "On the drive now: 21 songs in E:\Car USB\Attune mix". The payoff reports where it landed without opening anything.

**If Plex is not connected**, the Plex button stays on screen, drawn dashed and grey, and its hover text and its click both lead to Preferences, Plex. A disabled button would have hidden the destination again.

**Copy to USB and Export left the header row above the list.** The elements stay in the page, hidden, because the capture tool and older scripts reach them by id. Play, Save as new and Save stay where they were: they are housekeeping for the list, not where it goes.

**No new pipeline.** The Plex button calls the same `/api/export/plex` route as the panel's Create Plex playlist button, through one shared function both now use. The USB button runs the same copy job, `startCopy`, that the panel's Copy to folder button runs. The server did not change.

**Also in this pass, on the owner's word mid-session:** rating stars, the heart and album cards no longer grow or lift when the pointer rests on them. They only change colour. Verified by hovering a star in the running app: colour changed, size stayed 13 by 17 pixels, no transform.

## Click counts

| from a tuned mix to | before | after |
|---|---|---|
| a Plex playlist | 2 clicks, and the first was exploratory: "Export..." does not say Plex | 1 click on a button that says Plex |
| the files on a USB or car folder | 1 click plus the folder picker, on a button reading "Copy to USB..." at the same weight as Save as new | 1 click plus the same folder picker, on an accent button that names the destination, picker opening at the last folder |

The count for USB did not change; what changed is that a first-time user can see it. Both destinations are on screen, in words, before anything is pressed.

## Winamp identity, before and after

Before: header-row buttons in the bevelled theme, popover from the top. After: the same bevel and gradient recipe the toolbar's Create Mix button already uses, on a bottom strip with an LCD readout in the player's display font. A 2001 Winamp user would read the strip as the playlist editor's button row. The alternate flat themes get a flat version of the same bar from the shared rules.

## What was declined, and why

- **Splitting the Export panel into three panels.** Proposed on 2026-09-22 and never marked. Not needed once the two destinations have their own buttons; the panel is now the long tail behind More..., and splitting it would add clicks to reach the rarely used parts.
- **Copying to the remembered folder with no picker.** One click fewer, but a copy of a hundred files to a drive nobody confirmed is the kind of surprise the picker exists to prevent. The picker opens at the remembered folder, so it is one press of OK.
- **Touching the rest of the busy-ness.** The brief allowed it only where it serves export discoverability. Making the two destination buttons the only accent-coloured controls on the pane was enough; nothing else on the pane was thinned.
- **`SNAPSHOT_LATEST.md`, `HANDOFF_CURRENT.md`, `symmap.json`.** The brief asked for them to be read and updated. None exists, and `CLAUDE.md` section 7 retired handoffs and state documents on 2026-09-21 in favour of the four live files. The record went into `CHANGELOG.md` (what shipped) and this file (why), and nothing was reinstated.

## The real runs

Both destinations were exercised for real from the new bar, by clicking the buttons in the running app, on a 21-song mix seeded from "Twilight Zone" by 2 Unlimited.

| target | what happened | where it landed |
|---|---|---|
| Plex | `POST /api/export/plex` answered 200; bar read "On Plex now: 'Attune, like 2 Unlimited - Twilight Zone', 21 of 21 songs" | a playlist of that name on the household Plex server; it was not deleted afterwards |
| USB / car folder | `POST /api/export/copy` answered 200; bar read "On the drive now: 21 songs in ...\car-usb-2026-09-28\Attune mix" | 21 numbered mp3 files in the workspace scratch folder `_scantest\car-usb-2026-09-28\Attune mix`, listed on disk after the run |

## How this was checked

- **Before picture and click counts**: the running app at http://127.0.0.1:8778 on commit `6947ef0`, before the branch was cut, captured with the workspace tool `gui-audit\capture_readme_shots.py` pointed at the before folder.
- **After pictures**: same tool, same size, from the branch, into the after folder. Four pictures: the bar on a fresh mix, the bar after a send, More... open, and the Plex-not-connected state.
- **Hover help**: `AttuneTips.uncovered()` in the browser console returned 0 after the change.
- **Console**: no errors on load or after a mix.
- **Tests**: `pytest tests/ -q` from `attune\`, 265 passed and 1 skipped, the same as before the change.
- **Personal-data scan**: `tools/leak_check.py --patterns .leakpatterns --require-patterns`, tree clean.
- **Not verified**: the installed desktop build was not rebuilt or run; this was proved in the dev server only. The alternate themes were not looked at by eye.

## The one thing to do next

Look at the bar in the real window on the owner's machine, at his window size, and say whether the LCD readout and the two amber buttons are the thing the eye lands on after tuning a mix. If they are, retake the README export picture from the finished window and update `README.md`. If they are not, the lever is the bar's height and the button size, not another move.
