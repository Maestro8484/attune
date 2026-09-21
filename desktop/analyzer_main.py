r"""AttuneAnalyzer.exe — Attune's audio analyzer, split out of the GUI program.

    AttuneAnalyzer.exe scan import-folder <dir> --db <db>
    AttuneAnalyzer.exe scan analyze --db <db>
    AttuneAnalyzer.exe embed_onnx --db <db>

Why it is a separate program: analyzing audio needs librosa + numba + llvmlite +
scipy + soundfile + onnxruntime and the 276 MB CLAP encoder — roughly 500 MB that
PLAYING and MIXING never touch (the mixable vectors are already in mixer.db).
Shipping that inside Attune.exe made the GUI program eight times bigger than it
needs to be and gave every cold start that much more for the virus scanner to chew
through. So the GUI bundle stays lean and this program carries the analyzer, in an
`analyzer\` subfolder beside it.

It no longer carries ffprobe (141 MB for one job, reading tags, which src/scan.py now
does with mutagen; operator ruling B5 of 2026-08-04, executed for v0.1.0). It DOES still
carry ffmpeg: the v0.1.0 proof found 7.33% of a random 300-file mp3 sample that
libsndfile alone cannot decode. mp3, flac, ogg, opus and wav normally decode through
libsndfile with no help; m4a, aac and wma always need ffmpeg, as do damaged files of any
format. A build without ffmpeg says so in plain words per track, and picks those tracks
up by itself once an ffmpeg appears on PATH or in the user's own Attune bin folder.

`web/scanjob.py` launches this exe for the frozen + no-ML-venv case; it finds it
next to the GUI exe and refuses honestly if the folder is absent. The tool
dispatch itself is `worker_entry.run`, shared verbatim with app_desktop.py's
original `--attune-worker` sentinel, so both entry points behave identically.
"""
import os
import sys

import worker_entry

HERE = os.path.dirname(os.path.abspath(__file__))
ATT = os.path.dirname(HERE)                     # .../attune  (source layout only)


def main():
    argv = sys.argv[1:]
    # tolerate the historical sentinel so an old-style argv still works
    if argv and argv[0] == "--attune-worker":
        argv = argv[1:]
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip(), file=sys.stderr)
        sys.exit(0 if argv else 2)
    # analyzer=True: this entry point IS the analyzer, so worker_entry never has to
    # guess from what happens to be in the bundle.
    worker_entry.run(argv[0], argv[1:], ATT, os.path.join(HERE, "ffbin"), analyzer=True)


if __name__ == "__main__":
    main()
