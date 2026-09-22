"""tools/gen_third_party_notices.py refuses to run under a Python that is not the one
that froze the app.

Why: the tool decides what is standard library from sys.stdlib_module_names of the
RUNNING interpreter. Measured 2026-09-21: under Python 3.14 it reported four genuine
3.12 standard-library modules (_compression, aifc, chunk, sunau, all removed in 3.13)
as unattributed; under the build environment's 3.12 it reported zero. A legal document
that changes with whichever python happened to run it is not a document. The version
that froze each program is read out of the .exe's own PyInstaller cookie, so nothing
here depends on a build being on disk: the cookie is synthesised byte for byte.
"""
from __future__ import annotations
import importlib.util
import os
import struct
import sys
from pathlib import Path

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
TOOL = os.path.join(REPO, "tools", "gen_third_party_notices.py")


def _load_tool():
    name = "gen_third_party_notices"
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, TOOL)
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: the tool declares dataclasses under
    # `from __future__ import annotations`, and dataclasses resolves string
    # annotations through sys.modules[cls.__module__], which is None otherwise.
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def _fake_frozen_exe(path: Path, pyvers: int) -> Path:
    """A file whose tail is a PyInstaller PKG cookie carrying `pyvers` (major*100+minor)."""
    tool = _load_tool()
    cookie = struct.pack(tool._COOKIE_FORMAT, tool._COOKIE_MAGIC,
                         0, 0, 0, pyvers, b"python312.dll".ljust(64, b"\0"))
    path.write_bytes(b"MZ" + b"\0" * 300 + cookie)
    return path


def test_frozen_version_is_read_from_the_cookie(tmp_path):
    tool = _load_tool()
    exe = _fake_frozen_exe(tmp_path / "Attune.exe", 312)
    assert tool.frozen_python_version(exe) == (3, 12)
    exe2 = _fake_frozen_exe(tmp_path / "Other.exe", 314)
    assert tool.frozen_python_version(exe2) == (3, 14)


def test_a_file_without_a_cookie_is_refused(tmp_path):
    tool = _load_tool()
    plain = tmp_path / "not_frozen.exe"
    plain.write_bytes(b"MZ" + b"\0" * 500)
    with pytest.raises(tool.ArchiveError):
        tool.frozen_python_version(plain)


def test_matching_interpreter_passes():
    tool = _load_tool()
    assert tool.interpreter_mismatch((3, 12), (3, 12), "Attune.exe") is None


def test_different_minor_is_refused_and_names_both_and_the_fix():
    tool = _load_tool()
    msg = tool.interpreter_mismatch((3, 12), (3, 14), "Attune.exe",
                                    Path("C:/x/.venv-standalone"))
    assert msg is not None
    assert "3.12" in msg and "3.14" in msg
    assert "Attune.exe" in msg
    assert "REFUSING" in msg
    assert ".venv-standalone" in msg and "python.exe" in msg


def test_the_gate_is_wired_into_main_before_anything_is_written(tmp_path, monkeypatch, capsys):
    """End to end through main(): a bundle frozen by a version this interpreter is not
    exits 2 and writes nothing, before the build environment is even opened."""
    tool = _load_tool()
    app = tmp_path / "Attune"
    (app / "_internal").mkdir(parents=True)
    (app / "analyzer" / "_internal").mkdir(parents=True)
    other = 312 if sys.version_info[:2] != (3, 12) else 313
    _fake_frozen_exe(app / "Attune.exe", other)
    _fake_frozen_exe(app / "analyzer" / "AttuneAnalyzer.exe", other)
    venv = tmp_path / "venv"
    venv.mkdir()
    out = tmp_path / "THIRD_PARTY_NOTICES.md"
    lic = tmp_path / "licenses"
    monkeypatch.setattr(sys, "argv", ["gen", "--app", str(app), "--venv", str(venv),
                                      "--out", str(out), "--licenses-dir", str(lic)])
    assert tool.main() == 2
    assert not out.exists()
    assert not lic.exists()
    assert "REFUSING" in capsys.readouterr().err


def test_a_folder_with_no_frozen_program_is_refused_before_anything_is_written(
        tmp_path, monkeypatch, capsys):
    """An --app folder that exists but holds neither exe must not skip the gate: the
    first version continued past both missing programs and wrote a notices file."""
    tool = _load_tool()
    app = tmp_path / "Attune"
    (app / "_internal").mkdir(parents=True)
    venv = tmp_path / "venv"
    venv.mkdir()
    out = tmp_path / "THIRD_PARTY_NOTICES.md"
    lic = tmp_path / "licenses"
    monkeypatch.setattr(sys, "argv", ["gen", "--app", str(app), "--venv", str(venv),
                                      "--out", str(out), "--licenses-dir", str(lic)])
    assert tool.main() == 2
    assert not out.exists()
    assert not lic.exists()
    err = capsys.readouterr().err
    assert "REFUSING" in err and "no frozen program" in err
