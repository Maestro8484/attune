"""features.load_audio rescues a song the first decoder read only a fragment of, and
leaves a healthy file with a lying header alone.

Measured 2026-09-21 over the whole library: 77 tracks carried vectors built from
between 5.7 and 137 seconds of songs three to seventy minutes long. The rescue that
landed on 2026-09-20 fired only when the first decoder returned under five seconds, so
5.8 seconds of a 504-second song was never rescued. The container's stated length now
nominates a second decode too; ffmpeg's answer is believed only when it holds
CONFIRM_GAIN times more audio, which is what keeps the 2026-09-20 objection answered: a
variable-bitrate mp3 whose header overstates its length gets the same audio back from
both decoders and keeps its vector.

Hermetic: a fake librosa module, and the container reader, the decoder probe and the
ffmpeg decode all replaced, so no audio file and no ffmpeg are needed.
"""
from __future__ import annotations
import sys
import types

import numpy as np
import pytest

import features

SR = features.SR


def _fake_librosa(seconds):
    mod = types.ModuleType("librosa")
    mod.load = lambda path, sr=SR, mono=True: (np.zeros(int(seconds * sr), dtype=np.float32), sr)
    return mod


@pytest.fixture
def rig(monkeypatch):
    """Returns a function that sets up one scenario and runs load_audio."""
    def run(first_seconds, claimed, ffmpeg_seconds, decoder=True):
        monkeypatch.setitem(sys.modules, "librosa", _fake_librosa(first_seconds))
        monkeypatch.setattr(features, "container_seconds", lambda p: claimed)
        monkeypatch.setattr(features, "decoder_available", lambda: decoder)
        calls = []

        def fake_ffmpeg(path, sr=SR):
            calls.append(path)
            return None if ffmpeg_seconds is None else np.zeros(int(ffmpeg_seconds * sr), dtype=np.float32)
        monkeypatch.setattr(features, "_ffmpeg_decode", fake_ffmpeg)
        y, redecoded = features.load_audio("x.mp3", SR)
        return len(y) / SR, redecoded, len(calls)
    return run


def test_a_fragment_above_the_floor_is_rescued_when_ffmpeg_finds_the_song(rig):
    got, redecoded, asked = rig(first_seconds=5.8, claimed=504.0, ffmpeg_seconds=504.6)
    assert asked == 1
    assert redecoded is True
    assert abs(got - 504.6) < 0.01


def test_a_lying_header_on_a_healthy_file_keeps_the_first_read(rig):
    """ffmpeg returns the same audio give or take encoder delay: below CONFIRM_GAIN, so
    the first read stands and nothing is rewritten."""
    got, redecoded, asked = rig(first_seconds=120.0, claimed=400.0, ffmpeg_seconds=120.02)
    assert asked == 1
    assert redecoded is False
    assert abs(got - 120.0) < 0.01


def test_no_second_decode_when_the_claim_is_not_short(rig):
    got, redecoded, asked = rig(first_seconds=200.0, claimed=210.0, ffmpeg_seconds=210.0)
    assert asked == 0 and redecoded is False and abs(got - 200.0) < 0.01


def test_no_second_decode_for_a_short_claim(rig):
    """Under MIN_CLAIMED_SECONDS, 'half of it' is not a meaningful gap."""
    got, redecoded, asked = rig(first_seconds=6.0, claimed=15.0, ffmpeg_seconds=15.0)
    assert asked == 0 and redecoded is False


def test_no_second_decode_without_a_decoder_on_the_machine(rig):
    got, redecoded, asked = rig(first_seconds=5.8, claimed=504.0, ffmpeg_seconds=504.0, decoder=False)
    assert asked == 0 and redecoded is False and abs(got - 5.8) < 0.01


def test_the_floor_case_still_takes_any_improvement(rig):
    """Under five seconds the 2026-09-20 rule stands unchanged: anything more wins, no
    CONFIRM_GAIN bar, because there was no usable vector to protect."""
    got, redecoded, asked = rig(first_seconds=3.0, claimed=None, ffmpeg_seconds=4.0)
    assert asked == 1 and redecoded is True and abs(got - 4.0) < 0.01


class _Ran:
    def __init__(self, out):
        self.stdout = out


@pytest.fixture
def ffmpeg(monkeypatch):
    """_ffmpeg_decode with ffmpeg itself replaced. `answers` maps "guess" and "mp3" to the bytes
    that run hands back; returns the list of how each run was asked."""
    def rig(answers):
        asked = []

        def fake_run(cmd, **_kw):
            forced = cmd[cmd.index("-i") - 2:cmd.index("-i")] == ["-f", "mp3"]
            asked.append("mp3" if forced else "guess")
            out = answers["mp3" if forced else "guess"]
            if isinstance(out, Exception):
                raise out
            return _Ran(out)
        monkeypatch.setattr(features.shutil, "which", lambda _name: "ffmpeg")
        monkeypatch.setattr(features.subprocess, "run", fake_run)
        return asked
    return rig


def test_an_mp3_ffmpeg_cannot_place_is_read_again_as_mp3(ffmpeg):
    """Measured 2026-10-02: 18 MP3 files with a tag in front of a RIFF wrapper gave nothing by
    ffmpeg's own guess of the container, and 15 of them the whole song when told it is MP3."""
    song = np.ones(8, dtype="<f4").tobytes()
    asked = ffmpeg({"guess": b"", "mp3": song})
    y = features._ffmpeg_decode("damaged.MP3", SR)
    assert asked == ["guess", "mp3"] and len(y) == 8


def test_a_file_ffmpeg_reads_is_not_asked_twice(ffmpeg):
    asked = ffmpeg({"guess": np.ones(8, dtype="<f4").tobytes(), "mp3": b""})
    assert len(features._ffmpeg_decode("fine.mp3", SR)) == 8 and asked == ["guess"]


def test_only_an_mp3_is_read_again_as_mp3(ffmpeg):
    asked = ffmpeg({"guess": b"", "mp3": np.ones(8, dtype="<f4").tobytes()})
    assert features._ffmpeg_decode("nothing.flac", SR) is None and asked == ["guess"]


def test_a_run_that_hung_is_not_asked_a_second_time(ffmpeg):
    """A second ask after a timeout would double the longest wait on one file."""
    asked = ffmpeg({"guess": TimeoutError("hung"), "mp3": np.ones(8, dtype="<f4").tobytes()})
    assert features._ffmpeg_decode("hung.mp3", SR) is None and asked == ["guess"]
