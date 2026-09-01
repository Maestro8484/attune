r"""Keep a Plex playlist in step with a folder of mp3s. Preview first, apply on demand.

What it replaces: exporting a mix, copying the audio into a folder, copying that folder
onto a USB stick and carrying the stick to the car. The folder stays exactly where it
is and stays the roster of record; this makes Plex hold the same songs, streamed, so
the stick becomes optional rather than mandatory.

Rotation is the point. Add mp3s to the folder, delete mp3s from the folder, run this
again: the playlist gains the new ones and loses the dropped ones, and keeps its
identity so anything already pointing at it keeps working. ``--no-prune`` makes it
add-only when you want to keep what is already in the playlist.

Whatever is in the folder is on the playlist, subfolders included, dropped in now or
later (operator ruling 2026-09-01). ``--flat-only`` opts out for a one-off look.

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
    ap.add_argument("--title", default=plexmatch.DEFAULT_TITLE_TEMPLATE,
                    help="playlist name, or a template: {date} {ymd} {month} {year}")
    ap.add_argument("--order", default="folder", choices=list(plexmatch.ORDERS),
                    help="running order written into the playlist (default: folder)")
    ap.add_argument("--apply", action="store_true",
                    help="actually change the playlist; without this it only reports")
    ap.add_argument("--no-prune", action="store_true",
                    help="add missing tracks but do not remove ones the folder dropped")
    # Operator ruling 2026-09-01: whatever is in the folder is on the playlist,
    # subfolders included, now or later. Recursion is the rule, not an option; the
    # opt-out exists only so a one-off can ask for the flat view.
    ap.add_argument("--flat-only", dest="flat", action="store_true",
                    help="ignore subfolders (default: everything under the folder counts)")
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
    rep = plexmatch.resolve_folder(catalog, args.folder, recursive=not args.flat,
                                   prefer_zone=plexmatch.zone_chooser(px))
    # Stamped once, before anything is written, and printed -- so the name that gets
    # created is the name the report named, even if the clock rolls over mid-run.
    title = plexmatch.resolve_title(args.title)
    # The .bat runs preview and apply as two separate processes, so the shuffle seed
    # has to be something both can derive: today's date. Same day, same shuffle, so the
    # list previewed is the list written; a new day rolls a new one. (It was the track
    # count, which made "shuffle" the same permutation forever.)
    import datetime
    rep["resolved"] = plexmatch.order_resolved(rep["resolved"], args.order,
                                               datetime.date.today().toordinal())
    text = plexmatch.format_report(rep)
    print()
    print(text)

    if args.report:
        with io.open(args.report, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        side = os.path.splitext(args.report)[0] + ".json"
        with io.open(side, "w", encoding="utf-8") as fh:
            json.dump({"counts": rep["counts"], "folder": rep["folder"],
                       "title": title,
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
        pl = px.find_playlist(title)
        now = f"{len(px.playlist_items(pl['ratingKey']))} tracks" if pl else "does not exist yet"
        print(f"\nPREVIEW ONLY. Nothing on Plex was changed.")
        print(f"  playlist '{title}': {now}")
        print(f"  would hold: {len(keys)} tracks")
        print(f"  re-run with --apply to make it so.")
        return 0

    res = px.sync_playlist(title, keys, prune=not args.no_prune, order=True)
    if res.get("error"):
        print(f"\n[plex] FAILED: {res['error']}")
        return 1
    verb = "created" if res["created"] else "updated"
    print(f"\n[plex] {verb} '{res['title']}' (id {res['playlist']}): "
          f"{res['before']} -> {res['after']} tracks, "
          f"+{res['added']} added, -{res['removed']} removed")
    # The count above is read back off the server after the write, and this link opens
    # the real playlist -- so the confirmation can be the operator's own eyes.
    print(f"[plex] see it yourself: {res['web_url']}")
    if len(res["final_keys"]) != len(keys):
        print(f"[plex] WARNING: asked for {len(keys)}, playlist holds "
              f"{len(res['final_keys'])} -- re-run the preview to see which.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
