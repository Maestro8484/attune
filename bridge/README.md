# Attune Bridge

A modern LAN web UI for a **still-running MusicIP Mixer**. The original engine makes the
mixes (100% authentic); the Bridge gives you what 2008 never did:

- Browser UI from any device on your network — laptop, phone, tablet
- Instant library search, seed picking, size/style/variety sliders
- One-click `.m3u8` export into a playlists folder (MusicBee / Plex / LMS readable)
- **Copy to device**: mix → USB stick/SD card in one step. Numbered filenames
  (`01 - Artist - Title.mp3`) so car stereos and simple players play in order,
  ID3 tags intact, optional track# renumbering, files written in playlist order
  (FAT directory order = play order on dumb players), optional .m3u manifest.
  No staging copy on your disk — library in, device out.

## Setup

1. MusicIP Mixer running with its API on: **File → Preferences → Services →
   check "API" → Start** (must be re-done each time MusicIP launches).
2. `pip install flask mutagen`
3. `copy config.example.json config.json` and edit:
   - `export_dir` — where .m3u8 playlists land
   - `read_path_map` — optional: map your library root to a faster local mirror
   - `library_json` — optional cached catalog; leave empty to pull from the API at startup
4. `python bridge.py` → open `http://localhost:8765` (or `http://<this-pc-ip>:8765`
   from other devices; allow TCP 8765 through the firewall once).

## Security / trust model

The Bridge binds `0.0.0.0` (so your phone/laptop can reach it) and has **no
authentication** — it assumes a **trusted home LAN**. Anyone who can reach the port can
drive it. Guards that limit the blast radius of that:

- Export/copy **sources** are restricted to tracks in your loaded library — the server
  won't read arbitrary host files even if asked.
- Copy **destinations** are rejected if they target Windows / Program Files / ProgramData.

If your network isn't trusted: run it bound to `localhost` only (edit `app.run(host=...)`),
or put it behind a reverse proxy with auth. Don't expose port 8765 to the internet.

## Notes

- Windows-focused (USB drive detection uses the Win32 API); the rest is portable.
- The Bridge is optional — the standalone Attune engine in `../src/` needs no MusicIP.
