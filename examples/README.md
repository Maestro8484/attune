# Examples

## Try Attune with no real music at all

`make_demo_library.py` synthesises a tiny library of short WAV files in three distinct sonic
families: warm harmonic pads, bright bells, and percussive noise. No copyrighted audio is
involved. A working engine should mix within a family.

This is the fastest way to check that a change to the ranking still does something sensible,
and it's what a contributor should reach for before pointing anything at a real collection.

```
python examples/make_demo_library.py
attune-scan import-folder examples/sample_library
attune-scan analyze --workers 4
attune-mix --seed examples/sample_library/warm_pad_02.wav --size 5 --style 30
```

The `attune-*` commands come from `pip install -e .`. Without that, run the modules
directly: `python src/scan.py import-folder ...` and `python src/mixer.py --seed ...`.

You should see the other `warm_pad_*` tracks rank nearest, then `bright_bell_*`, with
`perc_noise_*` furthest away. If they don't, something in the ranking is wrong.

The generated `sample_library/` is git-ignored. Regenerate it whenever.

## Use your own music

Point `import-folder` at a real directory instead:

```
attune-scan import-folder "D:\Music"
attune-scan analyze --workers 6
attune-mix --seed "D:\Music\Artist\Album\track.mp3" --size 25 --style 40 --variety 3
```

That gives you the librosa engine. For the engine the app actually uses, fetch the model and
add the embeddings first. See [../INSTALL.md](../INSTALL.md).
