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


import re

# The installer's licence lines all have this shape (desktop/installer/attune.iss):
#   Source: "{#SourcePath}..\..\licenses\*";  DestDir: ...
#   Source: "{#SourcePath}..\..\LICENSE";     DestDir: "{app}"; DestName: "LICENSE.txt"; ...
# Only Source lines rooted at the repository count. Comments and [InstallDelete] lines
# mention the same names and must not count as evidence; the first version of this test
# matched bare substrings and still passed with all four Source lines deleted (cold
# audit, 2026-09-21).
_ISS_SOURCE = re.compile(r'^\s*Source:\s*"\{#SourcePath\}\.\.\\\.\.\\([^"]+)"', re.M)


def _installer_license_sources():
    iss = open(os.path.join(DESKTOP, "installer", "attune.iss"), encoding="utf-8").read()
    found = set()
    for raw in _ISS_SOURCE.findall(iss):
        name = raw.replace("\\", "/")
        if name.endswith("/*"):
            name = name[:-2]
        found.add(name)
    return found


def test_license_texts_list_mirrors_the_installer_script_both_ways():
    """The .iss Source lines and LICENSE_TEXTS must name the same set: a text the
    installer carries and the zip does not is the defect this file exists to stop, and
    the reverse would ship something the installer never agreed to."""
    iss_sources = _installer_license_sources()
    assert iss_sources, "no repository-rooted Source lines found in attune.iss"
    zip_sources = {src for src, _dest in build_installer.LICENSE_TEXTS}
    assert iss_sources == zip_sources, (
        f"installer ships {sorted(iss_sources)} but the zip list is {sorted(zip_sources)}")
    assert ("LICENSE", "LICENSE.txt") in build_installer.LICENSE_TEXTS, (
        "the installer renames LICENSE to LICENSE.txt; the zip must too")


def test_the_iss_parser_sees_exactly_the_four_source_lines():
    """Guards the guard: if the .iss changes shape the parser must fail loudly here,
    not silently return an empty set that the test above would then catch anyway."""
    assert _installer_license_sources() == {
        "licenses", "THIRD_PARTY_NOTICES.md", "NOTICE.md", "LICENSE"}


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
    out2 = tmp_path / "out2"
    with pytest.raises(SystemExit) as e:
        build_installer.make_zip(dist, str(out2), "9.9.9",
                                 use_7zip=False, repo=str(fake_repo))
    assert "NOTICE.md" in str(e.value)
    # and no archive is left behind for somebody to pick up: the refusal fires before
    # anything is written (the first version refused AFTER the zip was on disk)
    assert not (out2 / "Attune-9.9.9-win64.zip").exists()
    assert not out2.exists() or not any(out2.iterdir())


def test_add_license_texts_removes_a_partial_archive_when_it_refuses(tmp_path):
    """The belt to the braces above: called directly on an existing archive with a
    source missing, it deletes the archive and says so."""
    zip_path = tmp_path / "partial.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Attune/Attune.exe", b"MZ")
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    with pytest.raises(SystemExit) as e:
        build_installer.add_license_texts(str(zip_path), "Attune", repo=str(fake_repo))
    assert "deleted" in str(e.value)
    assert not zip_path.exists()


def test_ensure_license_texts_catches_an_archive_without_them(tmp_path):
    zip_path = tmp_path / "bare.zip"
    with zipfile.ZipFile(zip_path, "w") as zf:
        zf.writestr("Attune/Attune.exe", b"MZ")
    with pytest.raises(SystemExit) as e:
        build_installer._ensure_license_texts(str(zip_path), "Attune")
    assert "licenses" in str(e.value)
