# Attune walkthrough

Keep this open beside the app the first time. Three journeys, click by click. Every button
and field is named by the words printed on it.

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

4. As soon as there are enough analysed tracks to mix with, a **Start mixing** button
   appears. Press it. The wizard closes onto your library.
5. Click any track in the list to select it.
6. Press **Create Mix** (or Ctrl+M).

That's a mix. It appears on the right, in order, ready to play.

**If you'd rather not scan yet,** press **Skip for now**. Attune opens empty and won't nag
you. Rescan later from Preferences, under Library, with **Rescan library**.

### Steering the mix

Above the mix are the two knobs that matter:

- **Similarity** decides how strictly it matches the sound of your seed. Low sticks close to
  the seed's own character. High lets more in.
- **Variety** decides how far it's willing to wander down the ranked list. Zero gives you the
  nearest neighbours every time. Higher numbers give a different mix from the same seed.

Underneath, five weights say what "similar" is allowed to mean: **CLAP** (the sound itself),
**Timbre**, **Genre**, **Tempo** and **Era**. There are saved **Presets** if you don't want
to fiddle. Change one, press Create Mix again, listen. That's the whole loop.

### The other ways to start a mix

- **Radio** keeps going forever instead of stopping at a fixed length, and **Energy arc**
  shapes it: flat, rising, falling, or a wave.
- **Blend** takes two or more seeds and aims between them.
- **Adventure** takes a start and an end and builds the path from one to the other.
- **Genius** picks the seed for you, from tracks you have loved and haven't played lately.
- **Smart playlists** skip the seed entirely and select on rules: artist, genre, year,
  rating, tempo.

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
"This PC" writes drive letters. "Network share" writes `\\server\share` paths. "Plex server
path" writes the path as your Plex server sees it. Each one has its own field underneath,
and only the one you picked is enabled.

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
   **"Connected to MyPlex, 1 music library found."** If the address is wrong you get a
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
