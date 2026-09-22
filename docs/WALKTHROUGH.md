# Attune walkthrough

Keep this open beside the app the first time. Three journeys, click by click. Every button
and field is named by the words printed on it. Some buttons carry a small icon before the
words, so "Browse..." on screen is a folder glyph then the word; the words are what to look
for.

1. [First run to your first mix](#1-first-run-to-your-first-mix)
2. [A mix onto a USB stick for the car](#2-a-mix-onto-a-usb-stick-for-the-car)
3. [A mix into Plex, and keeping a folder mirrored](#3-a-mix-into-plex-and-keeping-a-folder-mirrored)

---

## 1. First run to your first mix

Start Attune from the Start menu, or run `Attune.exe` out of the portable folder.

A window opens titled **Welcome to Attune**.

1. Press **Browse...** and pick the folder your music is in. It can be a local drive, an
   external one, or a network share.
2. If your music lives in more than one place, press **+ Add another** and pick the next
   folder. Repeat as needed.
3. Press **Scan my music**.

The window changes to show progress and the line **Reading your music...** with a count that
climbs. Two things are happening: Attune walks your folders and reads the tags, which is
quick, and then it listens to each track, which is not. Listening is once per track, forever.
A large library takes hours.

You don't have to sit there. The scan carries on in the background and survives being
interrupted, and the main window has a progress bar along the top with a **Details** button
if you want to watch it.

4. **Start mixing** appears when the scan has finished, not part way through. Press it. The
   wizard closes onto your library. On a big collection that is the end of the wait, so shut
   the lid and come back; on a small one it is a minute.
5. Click any track in the list to select it.
6. Press **Create Mix** (or Ctrl+M).

That's a mix. It appears on the right, in order, ready to play.

**If you'd rather not scan yet,** press **Skip for now**. Attune opens empty and won't nag
you. Rescan later from Preferences, under Library, with **Rescan library**.

### Steering the mix

Five sliders sit above the mix. Each one says how much that thing is allowed to matter:

- **CLAP** is the sound itself, the neural judgement of what the track is like. It starts at
  1.0 and it is the biggest term by design.
- **Timbre** is the classic acoustic description: the texture and the colour of the sound.
- **Genre** rewards overlapping genre tags.
- **Tempo** penalises a big gap in BPM.
- **Era** penalises a big gap in years.

**Presets** sets them all at once. **V2 (default)** is the combination that came out ahead
in a partly blind listening test over five seed songs and six engines, thirty playlists ranked by ear, and it is what the sliders are already set
to when you open the app.

Two tick boxes underneath:

- **MMR variety** stops the mix filling up with tracks that are near-identical to each other
  as well as to the song you started from.
- **Flow ordering** arranges the result so it plays as a set, instead of just listing it
  best-match-first.

Change one, press Create Mix again, listen. That's the whole loop. Nothing here is a number
to get right; it is a dial to turn until it sounds like what you wanted.

**If you still run MusicIP** and have picked it as the engine, this panel is replaced by
MusicIP's own two knobs, **Similarity** and **Variety**, because those are the ones the old
engine actually takes.

### The other ways to start a mix

- **Radio (keep the queue playing forever)** keeps going instead of stopping at a fixed
  length, with **Radio variety** and an **Energy arc**: flat, rising, falling, or a wave.
- **Blend Selected (2+)** takes two or more songs and aims between them.
- **Adventure** takes a first and a last track and builds the path from one to the other.
- **Genius** picks the starting song for you: something you loved and haven't played
  lately if it can, then something you rated highly, then anything analysed.
- **Auto-Playlists**, in the sidebar, skip the starting song entirely and select on rules: artist,
  genre, year, rating, tempo. Not to be confused with **Smart Views** just above them, which
  are a fixed set like Loved and Top Rated.

---

## 2. A mix onto a USB stick for the car

This is the one that made Attune exist. Make a mix first, then:

1. Press **Export**. The export panel opens.
2. Beside **Folder / USB**, press **Browse...** and pick the stick.
3. **Layout** decides how the files are arranged on the stick:
   - **Flat**, which plays in mix order, writes everything into one folder with numbered
     filenames like `01 - Artist - Title.mp3`. This is the one for a car stereo or any
     simple player that sorts by filename.
   - **Folders**, Artist then Album, rebuilds the tree instead. Better for a player with a
     proper browser.
4. **Name** is optional. Leave it and Attune picks one.
5. Press **Copy to folder / USB**.

While it runs the panel shows **Copying 12/25...** and the track it's on, and a **Cancel**
button that really does stop it. At the end you get **Copied 25 tracks** and the folder it
wrote to.

Your original files are never moved or changed. Tags travel with the copies.

### Saving a playlist file instead

Same panel. **Name** sets the filename, **Path style** decides how the paths inside the file
are written, and the button saves an `.m3u8` into your playlist folder. MusicBee,
foobar2000, Plex and most things else will read it.

**Path style** matters if the playlist is going to be read by something other than this PC.
The three choices are **Local drive**, for this PC; **UNC share**, for other players on your
network; and **Plex server path**, for the path as your Plex server sees the same files.

The prefix each one writes is set once in **Preferences**, under **Playlists and export**,
where the same three choices appear as **How paths are written** with a field indented under
each: **This PC path**, **Network share path** and **Plex server path**. Only the field for
the choice you made is enabled.

---

## 3. A mix into Plex, and keeping a folder mirrored

You need your Plex server's address and a Plex key. The address you know. The key is the one
thing in this whole app you can't read off your own screen, which is why there's a button
that takes you to Plex's own instructions.

### Set it up once

1. Open **Preferences**, then the **Plex** section down the left.
2. **Server address**: type your server's address, like `http://192.168.1.50:32400`.
3. **Key**: paste your Plex key. **Show** reveals what you pasted. **Forget this server**
   clears the address, the key and the library together.

   To find the key: in Plex, open any song, press the three dots, choose **Get Info**, then
   **View XML**. The key is at the end of the address bar, after `X-Plex-Token=`. The
   **Show me where, on Plex's site** button opens Plex's own page on this in your browser.
4. Press **Test connection**. You want to see something like
   **"Connected to <your server's name>, 1 music library found."** If the address is wrong you get a
   sentence saying the server couldn't be reached; if the key is wrong you get a different
   sentence saying so. They are deliberately different, so you know which to fix.
5. **Library** now fills with the real names of the music libraries on that server. Pick
   yours.
6. Press **Save**.

The key is stored on your machine and is never shown in any log, any playlist, or any page
Attune serves.

### Push a mix into Plex

Make a mix, then press **Create Plex playlist**. You get back a line like
**"Plex: 23 added, 2 missed"**. "Missed" means Plex doesn't have those tracks, usually
because it hasn't scanned them yet.

Plex only knows about files it has scanned. **Rescan Plex library**, in the export panel,
asks it to go and look.

### Mirror a folder onto a Plex playlist

Different job: instead of pushing one mix, you keep a folder on disk and a Plex playlist in
step with each other. Add music to the folder, run the mirror, and the playlist follows.

In **Preferences**, under **Plex**, indented beneath Library and switched off until you've
picked one:

- **Mirror folder**: the folder of songs to keep matched. **Browse...** beside it.
- **Mirror name**: the Plex playlist it writes to.
- **Mirror order**: how the playlist is ordered.

Then, from the export panel, under **Mirror a folder to a Plex playlist**:

1. Press **Check the folder**. Nothing is written. It reports what it found and what it
   would do.
2. Read that. If it looks right, press **Update Plex**, which writes.
3. **See it in Plex** opens the playlist in Plex.

Two buttons rather than one is deliberate. You see what's about to happen before anything
happens.

---

## When something goes wrong

- **A track won't play.** Its file has moved or its drive is unplugged. Open **Missing
  Files** in the sidebar and press **Check files now**. It reads only, nothing is moved,
  deleted or re-tagged, and it fills the list with everything it couldn't find so you can
  point the library at where the files went.
- **The mix ignores a track you know is there.** It probably hasn't been analysed yet, or it
  failed to decode. The scan panel lists failures with the reason.
- **Plex says a lot of tracks were missed.** Press **Rescan Plex library** and try again once
  Plex has caught up.
- **You changed a setting and nothing happened.** Preferences applies on **Save**. The path
  to your settings file is shown at the top of the Preferences window if you want to look at
  it.

If a step in this guide took more than a sentence to explain, that's a defect in the app and
not in you. Please open an issue.
