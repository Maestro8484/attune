"""Generate a tiny synthetic 'music' library so you can try Attune's full pipeline
without any copyrighted audio. Creates short WAV files in three distinct sonic
families; a good engine should mix within a family.

  python examples/make_demo_library.py
  python src/scan.py import-folder examples/sample_library
  python src/scan.py analyze --workers 4
  python src/mixer.py --seed examples/sample_library/warm_pad_02.wav --size 5

No external deps beyond numpy (already required). Writes 16-bit PCM WAV.
"""
import os, wave, struct, math

OUT = os.path.join(os.path.dirname(__file__), "sample_library")
SR = 22050
DUR = 6.0


def write_wav(path, samples):
    with wave.open(path, "w") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SR)
        frames = b"".join(struct.pack("<h", int(max(-1, min(1, s)) * 32767)) for s in samples)
        w.writeframes(frames)


def tone(freqs, n, amp=0.3):
    for i in range(n):
        t = i / SR
        yield sum(math.sin(2 * math.pi * f * t) for f in freqs) * amp / len(freqs)


def noisy(n, seed, amp=0.25):
    # deterministic LCG noise + slow amplitude pulses (percussive family)
    x = seed
    for i in range(n):
        x = (1103515245 * x + 12345) & 0x7FFFFFFF
        r = (x / 0x7FFFFFFF) * 2 - 1
        env = 0.5 + 0.5 * math.sin(2 * math.pi * 2.0 * i / SR)  # 2 Hz pulse
        yield r * amp * env


def main():
    os.makedirs(OUT, exist_ok=True)
    n = int(SR * DUR)
    families = {
        # warm harmonic pads: low fundamentals + fifths (smooth, tonal)
        "warm_pad": [[110, 165], [130.8, 196], [146.8, 220], [98, 147]],
        # bright bells: high partials (sharp, high spectral centroid)
        "bright_bell": [[880, 1320, 1760], [988, 1480, 1976], [1047, 1568], [784, 1176, 1568]],
    }
    count = 0
    for fam, chordset in families.items():
        for i, freqs in enumerate(chordset, 1):
            write_wav(os.path.join(OUT, f"{fam}_{i:02d}.wav"), tone(freqs, n))
            count += 1
    # percussive/noisy family
    for i in range(1, 5):
        write_wav(os.path.join(OUT, f"perc_noise_{i:02d}.wav"), noisy(n, seed=1234 + i * 7))
        count += 1
    print(f"wrote {count} demo WAVs to {OUT}")
    print("Now: python src/scan.py import-folder examples/sample_library && "
          "python src/scan.py analyze")


if __name__ == "__main__":
    main()
