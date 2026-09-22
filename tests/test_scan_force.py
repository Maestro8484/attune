"""`scan.py analyze --force --paths-file F` reaches tracks nothing else can.

Why: about 74 tracks carried a vector built from a fragment of the song (one judged on
5.8 seconds of 724). Each is analyzed, unchanged on disk, and error-free, so
`--paths-file` alone subtracts it as done and `--retry-failed` never selects it. Verified
by reading src/scan.py on 2026-09-21: that is the whole reason they sat there for months.
`--force` subtracts exactly the tracks named in the file from the done set and touches
nothing that was not named. These checks hold the selection logic, which is a pure
function, so they need no audio, no database and no librosa.
"""
from __future__ import annotations

import pytest

import scan


def test_without_force_done_tracks_are_skipped():
    todo, forced = scan._select_todo(["a", "b", "c"], {"a", "c"})
    assert todo == ["b"]
    assert forced == set()


def test_force_re_analyzes_exactly_the_named_done_tracks():
    todo, forced = scan._select_todo(["a", "b"], {"a", "c", "d"}, force=True, named=True)
    assert todo == ["a", "b"]           # a forced (was done), b simply not done yet
    assert forced == {"a"}              # c and d were done but not named: untouched


def test_force_keeps_the_order_of_the_paths_file():
    todo, _ = scan._select_todo(["z", "y", "x"], {"z", "y", "x"}, force=True, named=True)
    assert todo == ["z", "y", "x"]


def test_force_refuses_without_a_paths_file():
    with pytest.raises(SystemExit) as e:
        scan._select_todo(["a"], {"a"}, force=True, named=False)
    assert "--paths-file" in str(e.value)


def test_the_flag_is_wired_into_the_command_line():
    import argparse
    import inspect
    src = inspect.getsource(scan.main)
    assert '"--force"' in src, "scan.py main() no longer offers --force"
    assert "force=" in inspect.getsource(scan.main) or "a.force" in src
