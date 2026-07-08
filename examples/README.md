# Examples

## Try Attune with zero real music

`make_demo_library.py` synthesizes a tiny library of short WAV files in three distinct
sonic families (warm harmonic pads, bright bells, percussive noise) — no copyrighted audio
needed. A working engine should mix within a family.

```bash
python examples/make_demo_library.py
python src/scan.py import-folder examples/sample_library
python src/scan.py analyze --workers 4
python src/mixer.py --seed examples/sample_library/warm_pad_02.wav --size 5 --style 30
```

You should see the other `warm_pad_*` tracks rank nearest, then `bright_bell_*`, with
`perc_noise_*` furthest away.

The generated `sample_library/` is git-ignored; regenerate it any time.

## Use your own music

Just point `import-folder` at a real directory:

```bash
python src/scan.py import-folder "/path/to/your/music"
python src/scan.py analyze --workers 6
python src/mixer.py --seed "/path/to/a/song.mp3" --size 25 --style 40 --variety 3
```
