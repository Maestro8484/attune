"""The portable zip carries the project's own licence texts, the same four the
installer lays down beside Attune.exe (desktop/installer/attune.iss).

Why this exists: opened on 2026-09-21, Attune-0.1.0-win64.zip held 4,502 entries and
132 licence files, every one belonging to a bundled Python package, and NOT ONE for
Attune itself: no licenses/ folder, no THIRD_PARTY_NOTICES.md, no NOTICE.md, no
LICENSE. That shipped a program conveyed under GPL-3.0-or-later with none of its own
paperwork. desktop/build_installer.py now appends the four from the repository root
after the build folder is zipped; these checks hold that in place, and hold the
refusal that fires when one of the four is missing.

Hermetic: a three-entry fake build folder, Python's zipfile path (never 7-Zip), and a
temporary "repository" for the refusal case. The happy path uses the REAL repository
root on purpose, so it also proves the four sources are present in this checkout.
"""
from __future__ import annotations
import os
import sys
import zipfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
DESKTOP = os.path.join(REPO, "desktop")
if DESKTOP not in sys.path:
    sys.path.insert(0, DESKTOP)

import build_installer  # noqa: E402


def _fake_dist(tmp_path):
    """What desktop/build.py leaves at the top of dist/Attune: exactly three entries."""
    dist = tmp_path / "Attune"
    (dist / "_internal").mkdir(parents=True)
    (dist / "analyzer").mkdir()
    (dist / "Attune.exe").write_bytes(b"MZ not really")
    (dist / "_internal" / "base_library.zip").write_bytes(b"x")
    (dist / "analyzer" / "AttuneAnalyzer.exe").write_bytes(b"MZ not really either")
    return str(dist)


def test_license_texts_list_mirrors_the_installer_script():
    """The .iss and LICENSE_TEXTS must name the same four things, or the zip and the
    installer drift apart, which is the defect this file exists to stop."""
    iss = open(os.path.join(DESKTOP, "installer", "attune.iss"), encoding="utf-8").read()
    for src, _dest in build_installer.LICENSE_TEXTS:
        assert src in iss, f"{src} is in LICENSE_TEXTS but not in attune.iss"
    assert ("LICENSE", "LICENSE.txt") in build_installer.LICENSE_TEXTS, (
        "the installer renames LICENSE to LICENSE.txt; the zip must too")


def test_zip_carries_the_four_licence_texts(tmp_path):
    dist = _fake_dist(tmp_path)
    out = tmp_path / "out"
    zip_path = build_installer.make_zip(dist, str(out), "9.9.9", use_7zip=False)

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
    # the build folder itself
    assert "Attune/Attune.exe" in names
    assert "Attune/analyzer/AttuneAnalyzer.exe" in names
    # the four the installer ships, now in the zip too
    assert "Attune/THIRD_PARTY_NOTICES.md" in names
    assert "Attune/NOTICE.md" in names
    assert "Attune/LICENSE.txt" in names
    licence_files = [n for n in names if n.startswith("Attune/licenses/")]
    assert licence_files, "no Attune/licenses/ entries in the zip"
    # still exactly one top-level folder, so unpacking never spills loose files
    tops = {n.split("/")[0] for n in names}
    assert tops == {"Attune"}


def test_zip_refuses_when_a_licence_text_is_missing(tmp_path):
    """A zip with a gap must not be produced quietly; the refusal names the file."""
    dist = _fake_dist(tmp_path)
    fake_repo = tmp_path / "repo"
    (fake_repo / "licenses").mkdir(parents=True)
    (fake_repo / "licenses" / "x.txt").write_text("x")
    (fake_repo / "THIRD_PARTY_NOTICES.md").write_text("notices")
    (fake_repo / "LICENSE").write_text("mit")
    # NOTICE.md deliberately absent
    with pytest.raises(SystemExit) as e:
        build_installer.make_zip(dist, str(tmp_path / "out2"), "9.9.9",
                                 use_7zip=False, repo=str(fake_repo))
    assert "NOTICE.md" in str(e.value)


def test_ensure_license_texts_catches_an_archive_without_them(tmp_path):
    zip_path = tmp_path / "bare.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Attune/Attune.exe", b"MZ")
    with pytest.raises(SystemExit) as e:
        build_installer._ensure_license_texts(str(zip_path), "Attune")
    assert "licenses" in str(e.value)
