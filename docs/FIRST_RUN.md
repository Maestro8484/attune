# First run

What happens the first time you start Attune, where it puts things, and how to take it back
out again.

For the click-by-click version of the whole journey, see
[WALKTHROUGH.md](WALKTHROUGH.md).

## What happens

A window opens titled **Welcome to Attune**. It asks for one thing: where your music is.

Point it at a folder, add more folders if your music is spread around, and press **Scan my
music**. Two passes follow.

**Pass one reads your files.** It walks the folders, finds the audio, and reads the tags
already in each file: artist, album, title, genre, year, length. Minutes, not hours.

**Pass two listens.** Every track is decoded and turned into numbers that describe how it
sounds. This is the slow part. It happens once per track and never again unless the file
changes.

It doesn't listen to the whole track. The acoustic description takes a 90-second window from
the middle, and the neural one takes three ten-second windows at roughly a sixth, a half and
five sixths of the way through. Both skip intros and fade-outs, which describe a song badly,
and both bound the cost.

You don't have to wait. The scan runs in the background, survives you closing the wizard, and
resumes if it's interrupted. A progress bar sits along the top of the main window with a
**Details** button beside it.

**Start mixing** appears when the scan has finished, not part way through, so on a big
library that button is the end of the wait rather than the middle of it.

**If you'd rather not scan yet,** press **Skip for now**. Attune opens empty and won't ask
again. Start a scan later from Preferences, under Library, with **Rescan library**.

## Formats

mp3, flac, m4a, aac, ogg, opus, wma and wav. The decoder for the awkward ones is bundled, so
there's nothing for you to install.

Some files won't analyse: a truncated download, a file with a damaged header, a format that
isn't really what its extension says. Those are listed with their reason rather than failing
silently, and they're retried automatically if the thing that was missing turns up later.

## Where everything lives

| What | Where |
|---|---|
| The program | `%LOCALAPPDATA%\Programs\Attune` (or wherever you unzipped the portable copy) |
| Your settings | `%APPDATA%\Attune\settings.json` |
| The analysed library | a database file, by default in `%APPDATA%\Attune`, named in Preferences under Advanced |
| Logs | `%APPDATA%\Attune\logs\` |

`%APPDATA%` is `C:\Users\<you>\AppData\Roaming`. `%LOCALAPPDATA%` is the `Local` folder
beside it. Paste either into the Explorer address bar to get there.

The Preferences window shows the path to your settings file along its top, and the database
and log paths under **Advanced**, so you never have to remember any of this.

**Moving the library.** The database is one file. Copy it wherever you like and point
Preferences, under Advanced, at the new location. Back it up the same way.

## Uninstalling

Settings, then Apps, then Attune, then Uninstall. Or the **Uninstall Attune** entry in the
Start menu.

**What it removes:** the program.

**What it leaves, on purpose:** everything in `%APPDATA%\Attune`. Your settings and your
analysed library. That's an overnight scan you paid for once, and throwing it away because
you installed a newer version would be rude. Reinstall and it's all still there.

Delete `%APPDATA%\Attune` by hand if you want no trace. Your music files are never touched by
any of this.

If you used the portable zip, delete the folder. The same `%APPDATA%\Attune` note applies.

## Upgrading

Download the newer installer and run it over the top. There's no automatic update.

Close Attune first. The installer deliberately removes the old program files before writing
the new ones, so nothing stale is left behind to be loaded by accident. Two things interrupt
that: cancelling halfway, and leaving Attune running and telling the installer not to close
it. Either leaves the app incomplete until you run the installer again. Your settings and
your analysed library are never touched by any of it.

## If the first run goes wrong

- **The window opens but there's nothing in it.** The scan hasn't found anything mixable
  yet. Look at the progress bar along the top, or open Preferences under Library and check
  that the folder you picked is the one your music is actually in.
- **The scan finds files but nothing gets analysed.** Look at the failure list in the scan
  details; it names the reason per file.
- **Attune doesn't open at all.** If the drive holding its library database is unplugged,
  Attune stops before its window appears rather than opening onto a library that isn't
  there. Reconnect the drive and start it again. This is a known rough edge: it
  should say so on screen and offer to pick another library, and it doesn't yet.
- **Something else.** Preferences, under Advanced, has the logs. Please open an issue and
  say what you saw.
