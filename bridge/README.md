# Attune Bridge

A modern LAN web UI for a **still-running MusicIP Mixer**. The original engine makes the
mixes, so they're 100% authentic; the Bridge gives you what 2008 never did.

**You probably don't want this.** The Attune desktop app is the product, it needs no MusicIP,
and it does everything below and more. The Bridge exists for people who still have a working
MusicIP install and want to drive it from a phone. It's kept because it works, not because
it's being developed.

What it does:

- Browser UI from any device on your network: laptop, phone, tablet
- Instant library search, seed picking, size, style and variety sliders
- One-click `.m3u8` export into a playlists folder, readable by MusicBee, Plex or LMS
- **Copy to device**: mix straight onto a USB stick or SD card in one step. Numbered
  filenames (`01 - Artist - Title.mp3`) so car stereos and simple players play in order, ID3
  tags intact, optional track number renumbering, files written in playlist order so FAT
  directory order matches play order on dumb players, optional `.m3u` manifest. No staging
  copy on your disk: library in, device out.

## Setup

1. MusicIP Mixer running with its API on: **File, Preferences, Services,** tick **API**, then
   **Start**. This has to be redone every time MusicIP launches.
2. `pip install -e .[bridge]` from the repository root, or just `pip install flask mutagen`.
3. `copy config.example.json config.json` and edit:
   - `export_dir`: where `.m3u8` playlists land
   - `read_path_map`: optional, maps your library root to a faster local mirror
   - `library_json`: optional cached catalog; leave it empty to pull from the API at startup
4. `python bridge.py`, then open `http://localhost:8765`, or `http://<this-pc-ip>:8765` from
   another device. Allow TCP 8765 through the firewall once.

## Security and trust model

**Read this before you run it.** The Bridge binds `0.0.0.0` so your phone can reach it, and
it has **no authentication**. It assumes a trusted home LAN. Anyone who can reach the port
can drive it.

This is a deliberately different trust model from the Attune app, which binds to `127.0.0.1`
by default.

Guards that limit the blast radius:

- Export and copy **sources** are restricted to tracks in your loaded library, so the server
  won't read arbitrary files on the host even if asked.
- Copy **destinations** are an allowlist, not a blocklist. Network paths, device paths and
  relative paths are refused outright, and what is left has to be either a removable drive or
  a folder you named in `config.json`. A fixed drive you did not configure is refused, which
  is the point: this thing has no password.

If your network isn't trusted, bind it to localhost instead (edit `app.run(host=...)`) or put
it behind a reverse proxy with authentication. Don't expose port 8765 to the internet.

## Notes

- Windows-focused: the USB drive detection uses the Win32 API. The rest is portable.
- Optional. Attune's own engine, in `../src/`, needs no MusicIP at all.
