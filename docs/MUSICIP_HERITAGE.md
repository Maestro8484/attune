# The MusicIP heritage

## What MusicIP Mixer was

**MusicIP Mixer** (originally **MusicMagic Mixer**, by Predixis, later MusicIP Corporation)
was a desktop application from roughly 2005–2010. Its signature feature was **acoustic
similarity playlisting**: you picked a *seed* track and it generated a playlist of songs that
genuinely *sounded* like it — analyzed from the audio itself, not from genre tags or
collaborative-filtering "people also liked" data.

Under the hood it:
- Analyzed each track into a proprietary acoustic signature (stored in a `.m3lib` library).
- Optionally identified tracks via the MusicDNS/GenPUID fingerprint service (a *separate*
  system from the similarity engine — this is the part that died with the servers).
- Exposed a local HTTP API on `localhost:10002`, which let other software (notably the
  **Logitech Media Server / Squeezebox** ecosystem via the `MusicIP`/`lms-mipmixer` plugins)
  request mixes programmatically.
- Offered tuning sliders — **style** and **variety** — plus artist/genre constraints.

## Why it died

MusicIP Corporation wound down around 2010. The MusicDNS identification service went offline,
registration/activation servers disappeared, and the software was never open-sourced. Modern
installs still *run* (the local similarity engine works offline), but the product is
abandonware: no updates, no support, dead online components, and configuration quirks (its
API must be re-enabled by hand on every launch).

## What Attune keeps, drops, and modernizes

| MusicIP | Attune |
|---|---|
| Proprietary closed acoustic signature | A music-trained CLAP embedding plus an open librosa descriptor, both stored where you can read them |
| MusicDNS/GenPUID online identification | **Dropped.** Identification is not similarity and mixing never needed it |
| `.m3lib` binary library | One SQLite file |
| `style` / `variety` sliders, artist spacing | The same knobs, called Similarity and Variety, with the implementation on show |
| `localhost:10002` HTTP API, re-enabled by hand every launch | A local web app that just runs. `.m3u8` export and Plex shipped; Jellyfin has not |
| Heavy legacy desktop UI | A small local web UI in a native window. The engine stays UI-agnostic |
| Windows and Mac only, unmaintained | Windows for now, with a portable Python engine underneath. Source MIT, built app GPL-3.0-or-later because of what it bundles |

## The common misconception (worth stating loudly)

A lot of "modernize MusicIP" advice reaches for **Chromaprint/AcoustID**. That's a category
error. Chromaprint is an *identification* fingerprint — it tells you whether two files are
the *same recording*. The distance between Chromaprint hashes of two *different* songs is
meaningless, so you cannot build a "sounds like" mixer on it. MusicIP's magic was
*similarity*, which requires *descriptive* features with a meaningful metric between distinct
tracks. Attune is built on the latter. (Chromaprint is great — just for a different job.)

## Credit

Attune stands on the shoulders of the MusicIP community that kept the flame alive for over a
decade — especially the Logitech Media Server plugin authors (e.g. `lms-mipmixer`) who
documented the local API and proved the concept still had legs. Attune reimplements the idea
from scratch under an open license so it can outlive its inspiration.
