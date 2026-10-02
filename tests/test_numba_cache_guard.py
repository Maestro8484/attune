"""numba's compile cache never reaches a release (2026-10-02).

--collect-all librosa copied librosa's __pycache__ from the build environment, numba's cache
files with it, and those carry the build machine's paths, user name included: 95 of them were
in every download up to 0.1.2. desktop/build.py now deletes them after the analyzer build and
tools/make_release.py refuses a folder that still holds one. Both are held here on a folder
made in tmp_path; nothing is built.
"""
from __future__ import annotations
import os
import sys

import pytest

DESKTOP = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "desktop")
sys.path.insert(0, DESKTOP)

import build             # noqa: E402
import build_installer   # noqa: E402


def _tree(root):
    cache = root / "analyzer" / "_internal" / "librosa" / "util" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "utils._localmax-1035.py312.nbi").write_bytes(b"index")
    (cache / "utils._localmax-1035.py312.1.nbc").write_bytes(b"code")
    (cache / "utils.cpython-312.pyc").write_bytes(b"kept")
    return cache


def test_the_build_strips_numba_cache_and_nothing_else(tmp_path):
    cache = _tree(tmp_path)
    assert build.strip_numba_cache(str(tmp_path)) == 2
    assert sorted(os.listdir(cache)) == ["utils.cpython-312.pyc"]


def test_the_release_refuses_a_folder_that_still_holds_one(tmp_path):
    _tree(tmp_path)
    with pytest.raises(SystemExit, match="numba cache"):
        build_installer.assert_no_numba_cache(str(tmp_path))
    build.strip_numba_cache(str(tmp_path))
    build_installer.assert_no_numba_cache(str(tmp_path))
