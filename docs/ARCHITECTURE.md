# Architecture

Attune is deliberately small. One SQLite file holds everything, a ranking engine reads it,
and a local web server puts a window on top. No services, no cloud, no vector database, no
containers.

## Two programs, one install

The shipped app is two executables in one folder, and the split is the most important
structural decision in the project.

```
  dist/Attune/
    Attune.exe                 the app you launch: flask, numpy, mutagen, onnxruntime,
    _internal/                 and the webview shell
    analyzer/
      AttuneAnalyzer.exe       the audio analyzer: librosa, numba, scipy, onnxruntime,
      _internal/               the CLAP encoder and ffmpeg
```

**Why.** Analysing new audio needs roughly half a gigabyte of machinery that playing and
mixing never touch, because the numbers a mix is built from are already in the database.
Folding it all into one executable made the program you launch every day about eight times
bigger than it needs to be, and gave the virus scanner that much more to walk through on a
cold first start. So the GUI carries only what the GUI uses, and `web/scanjob.py` hands
analysis to `AttuneAnalyzer.exe` when you scan.

It is still one install. Nothing to download separately, no second setup step.

The analyzer is a console build on purpose: a windowed PyInstaller executable has no usable
standard output, and `scanjob.py` reads its progress lines to drive the progress bar. No
console window ever appears, because the process is started with `CREATE_NO_WINDOW`.

`desktop/build.py`'s docstring is the authoritative description of all this and is kept
current with the build.

## The flow

```
  music files -> features.py  -> \
                  (librosa)       > db.py (one SQLite file) -> engine.py -> a playlist
               -> embed_onnx.py -> /                             ^
                  (CLAP, ONNX)                            web/app.py serves it
```

## The store: `src/db.py`

One SQLite file, WAL mode. The engine makes four tables; the app adds five more to the
same file as you use it.

| Table | Holds |
|---|---|
| `meta` | schema version and feature dimension, so a stale database refuses to load rather than producing quiet nonsense |
| `tracks` | the catalog: path, tags, duration |
| `features` | the 79-number acoustic descriptor as raw little-endian float32, plus tempo and any decode error |
| `clap` | the 512-number CLAP embedding, same storage |

Errors are recorded, not just successes, so a file that fails to decode is not retried on
every run. Some failures are marked as worth retrying, for instance when the reason was a
missing decoder that might turn up later.

The five the app adds are `audioinfo` (`web/audioinfo.py`), `filestate` (`web/libverify.py`),
`recipes` (`web/recipes.py`), `smartlists` (`web/smartlists.py`) and `usermeta`
(`web/userdata.py`): what the analyzer found in each file, what the last verify pass saw on
disk, your saved mix recipes, your Auto-Playlists, and your ratings, loved flags and tags.
Nine in all, and the whole lot is still one file you can copy.

`SCHEMA_VERSION` is 1. Change the descriptor and you must bump it, which makes every stored
vector stale and forces a re-analysis.

## The descriptors: `src/features.py` and `src/embed_onnx.py`

Two independent descriptions of the same audio.

**`features.py`** loads the track at 22.05 kHz mono, takes a 90-second window from the
middle, and produces 79 float32 numbers:

| Offset | Count | What |
|---|---|---|
| 0-39 | 40 | MFCC mean and standard deviation: timbre |
| 40-63 | 24 | Chroma CQT mean and standard deviation: harmony |
| 64-70 | 7 | Spectral contrast: tonal against noisy energy bands |
| 71-77 | 7 | Centroid, bandwidth, rolloff, zero-crossing, loudness mean and spread, flatness |
| 78 | 1 | Tempo in BPM |

Decoding goes through libsndfile first and falls back to ffmpeg, which is bundled with the
analyzer. The fallback also fires when the first decoder *succeeds* but hands back a
fragment, because a decoder that quietly returns four seconds of a four-minute song is worse
than one that fails outright.

**`embed_onnx.py`** produces the 512-number CLAP embedding: three ten-second windows at 15%,
50% and 85% through the track, mean-pooled and normalised. It runs the model through
onnxruntime with a numpy port of the log-mel front end, so the shipped app needs no PyTorch.
`embed.py` is the PyTorch reference implementation of the same protocol and exists for
comparison, not for shipping.

## The engines: `src/engine.py`

Three, sharing one interface and one pool of tracks, so playback and export never care which
one picked a row. See [ENGINES.md](ENGINES.md).

## The cataloger: `src/scan.py`

`import-folder` walks a directory and reads tags with mutagen, falling back to ffprobe.
`analyze` extracts features for everything not yet done: a thread pool, incremental,
resumable, committing as it goes. `--paths-file` restricts a run to a subset.

## The window: `web/` and `desktop/`

`web/app.py` is a Flask app bound to `127.0.0.1` by default, plus twelve feature modules
beside it: library browsing, playlists, smart lists, recipes, scanning, reload, verification,
export, Plex sync, logs. 77 routes in all.

`desktop/app_desktop.py` starts that server on a free port and wraps it in a native window
through pywebview and the Windows WebView2 runtime. There is no browser tab and no terminal.
The same server is perfectly usable from a browser if you prefer, which is how it's developed.

## Design principles

1. **Local-first, always.** Nothing leaves the machine. See [PRIVACY.md](PRIVACY.md).
2. **Transparent where it can be.** The rules on top of the embedding are explainable, and
   every knob maps to something you can reason about.
3. **One file to back up.** The whole state is the database.
4. **Right-sized.** No microservices for a single-user tool. Brute-force distance over tens
   of thousands of rows is a few milliseconds in NumPy. Swap the linear scan for an
   approximate-nearest-neighbour index behind the same interface only when a real workload
   demands it.
