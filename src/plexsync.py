r"""Keep a Plex playlist in step with a folder of mp3s. Preview first, apply on demand.

What it replaces: exporting a mix, copying the audio into a folder, copying that folder
onto a USB stick and carrying the stick to the car. The folder stays exactly where it
is and stays the roster of record; this makes Plex hold the same songs, streamed, so
the stick becomes optional rather than mandatory.

Rotation is the point. Add mp3s to the folder, delete mp3s from the folder, run this
again: the playlist gains the new ones and loses the dropped ones, and keeps its
identity so anything already pointing at it keeps working. ``--no-prune`` makes it
add-only when you want to keep what is already in the playlist.

    preview (default -- reads Plex, changes nothing):
      python src/plexsync.py --folder "L:\_MUSIC\MP3 CD" --title Car-MP3usb

    apply:
      python src/plexsync.py --folder "L:\_MUSIC\MP3 CD" --title Car-MP3usb --apply

The matching itself lives in src/plexmatch.py and is deliberately conservative: file
name, then ID3 artist+title, then title with an artist substring, every tier gated on
duration. Anything it cannot answer honestly is printed as "not in your library"
rather than resolved to a plausible neighbour.

Connection settings come from .env exactly as the rest of export.py does
(PLEX_URL / PLEX_ACCOUNT_TOKEN / PLEX_SECTION_KEY / PLEX_MACHINE_ID); nothing here
reads or writes mixer.db, and no audio file is opened for anything but its tags.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import export          # noqa: E402  (path shim above must run first)
import plexmatch       # noqa: E402

# The two Location paths of the operator's music section, used only as a one-point
# tiebreak nudge when a name matches in both. Read off the server at run time rather
# than hardcoded, so a renamed folder cannot silently stop nudging.
def zone_chooser(exporter, section_key):
    try:
        d = exporter._get("/library/sections")
        locs = []
        for s in d.get("MediaContainer", {}).get("Directory", []):
            if str(s.get("key")) == str(section_key):
                locs = [l.get("path") for l in s.get("Location", []) if l.get("path")]
        singles = next((p for p in locs if "single" in p.lower()), None)
        albums = next((p for p in locs if "album" in p.lower()), None)
    except Exception:
        singles = albums = None
    if not (singles and albums):
        return None

    def choose(filename):
        return (albums if plexmatch._LEAD_NUM.match(filename) else singles).rstrip("/") + "/"
    return choose


def build(cfg):
    mapper = export.PathMapper(cfg.get("LOCAL_LIBRARY_ROOT"), cfg.get("UNC_LIBRARY_ROOT"),
                               cfg.get("PLEX_LIBRARY_ROOT"))
    missing = [k for k in ("PLEX_URL", "PLEX_ACCOUNT_TOKEN", "PLEX_MACHINE_ID")
               if not cfg.get(k)]
    if missing:
        raise SystemExit("Plex sync needs these in .env: " + ", ".join(missing))
    return export.PlexExporter(cfg["PLEX_URL"], cfg["PLEX_ACCOUNT_TOKEN"],
                               cfg.get("PLEX_SECTION_KEY", "1"), cfg["PLEX_MACHINE_ID"],
                               mapper, timeout=60)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--folder", required=True, help="the folder of mp3s that IS the roster")
    ap.add_argument("--title", default="Car-MP3usb", help="Plex playlist title (exact)")
    ap.add_argument("--apply", action="store_true",
                    help="actually change the playlist; without this it only reports")
    ap.add_argument("--no-prune", action="store_true",
                    help="add missing tracks but do not remove ones the folder dropped")
    ap.add_argument("--recursive", action="store_true", help="descend into subfolders")
    ap.add_argument("--report", help="write the full report here (.txt, plus a .json beside it)")
    ap.add_argument("--env", help="path to .env (default: auto-discover)")
    args = ap.parse_args(argv)

    # Windows consoles default to cp1252 and this report is full of real track titles.
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

    if not os.path.isdir(args.folder):
        raise SystemExit(f"not a folder: {args.folder}")

    cfg = export.load_env(args.env)
    px = build(cfg)

    print(f"[plex] reading library section {px.section_key} ...", flush=True)
    tracks = px.build_meta_index()
    print(f"[plex] {len(tracks)} tracks indexed", flush=True)

    catalog = plexmatch.PlexCatalog(tracks)
    rep = plexmatch.resolve_folder(catalog, args.folder, recursive=args.recursive,
                                   prefer_zone=zone_chooser(px, px.section_key))
    text = plexmatch.format_report(rep)
    print()
    print(text)

    if args.report:
        with io.open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        side = os.path.splitext(args.report)[0] + ".json"
        with io.open(side, "w", encoding="utf-8") as fh:
            json.dump({"counts": rep["counts"], "folder": rep["folder"],
                       "title": args.title,
                       "residue": [{k: r[k] for k in ("file", "artist", "title", "secs")}
                                   for r in rep["residue"]],
                       "flagged": [{"file": f["file"], "grade": f["grade"],
                                    "tier": f["tier"], "drift": f["drift"],
                                    "chose": (f["track"].get("files") or [""])[0],
                                    "tied": f["tied"]} for f in rep["flagged"]],
                       "resolved": [{"file": r["file"], "rk": r["track"]["rk"],
                                     "grade": r["grade"],
                                     "plex": (r["track"].get("files") or [""])[0]}
                                    for r in rep["resolved"]]},
                      fh, indent=1, ensure_ascii=False)
        print(f"\n[report] {args.report}\n[report] {side}")

    keys = [r["track"]["rk"] for r in rep["resolved"]]
    if not args.apply:
        pl = px.find_playlist(args.title)
        now = f"{len(px.playlist_items(pl['ratingKey']))} tracks" if pl else "does not exist yet"
        print(f"\nPREVIEW ONLY. Nothing on Plex was changed.")
        print(f"  playlist '{args.title}': {now}")
        print(f"  would hold: {len(keys)} tracks")
        print(f"  re-run with --apply to make it so.")
        return 0

    res = px.sync_playlist(args.title, keys, prune=not args.no_prune)
    if res.get("error"):
        print(f"\n[plex] FAILED: {res['error']}")
        return 1
    verb = "created" if res["created"] else "updated"
    print(f"\n[plex] {verb} '{res['title']}' (id {res['playlist']}): "
          f"{res['before']} -> {res['after']} tracks, "
          f"+{res['added']} added, -{res['removed']} removed")
    if len(res["final_keys"]) != len(keys):
        print(f"[plex] WARNING: asked for {len(keys)}, playlist holds "
              f"{len(res['final_keys'])} -- re-run the preview to see which.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
