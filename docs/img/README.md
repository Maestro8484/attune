# Screenshots

The pictures the repository's README shows. Every one is a published picture: anyone can read every pixel of it.

## What each one shows

| File | What it shows |
|---|---|
| `studio-main.png` | A mix in the main window, numbered in play order, with its bar of next steps and the playing song on the right |
| `genius.png` | Genius has picked a song, built a mix and started playing |
| `albums.png` | The album view |
| `context-menu.png` | Right-click on a song |
| `mix-options-radio.png` | Mix options with Radio switched on |
| `blend.png`, `adventure.png` | A Blend and an Adventure mix |
| `export.png`, `usb-copy.png` | The export panel, top and the copy-to-USB part |
| `preferences.png` | Preferences, Library |
| `first-run.png` | The Welcome window |
| `auto-playlist.png` | The Auto-Playlist builder |
| `hover-help.png` | The hover help on the Auto-DJ switch |

## Rules for capturing them

Rewritten 2026-09-22. The older text here said never to use the owner's real library and to capture a synthetic one; the published pictures have used the real library since 2026-09-22, by Joe's direction, with four kinds of text replaced. This is that rule.

**Replace these four before every shot, in the page itself:** the network share name, drive letters and every path (replaced whole, to the end of the line, because a folder name after a space is still a real folder name), the Windows username, and every playlist name and folder name in the side list. The literal strings to catch are in `.leakpatterns`, which is not published.

**Read every picture back by eye before committing it.** `tools/leak_check.py` reads text, not images, so it cannot catch a path in a corner of a picture. The first capture on 2026-09-22 leaked all four kinds; the second left a folder layout showing after a replaced drive name.

**Size 1400 by 860, the app's own theme.** The capture script used on 2026-09-22 drove the running app with Playwright in Microsoft Edge, set each view up, replaced the text, then shot it. It is not kept in the repository because it reads `.leakpatterns`.
