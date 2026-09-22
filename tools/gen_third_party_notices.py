#!/usr/bin/env python3
"""gen_third_party_notices.py - write THIRD_PARTY_NOTICES.md and licenses/ from the BUILT app.

Attune ships as a PyInstaller "onedir" bundle with two frozen programs:

    dist/Attune/Attune.exe                    + _internal/           (window + local web server)
    dist/Attune/analyzer/AttuneAnalyzer.exe   + analyzer/_internal/  (audio analysis)

Only a handful of ``*.dist-info`` folders survive into that bundle (8 and 12 when this was
written), so anything that walks the built folder alone under-reports by an order of
magnitude. The authoritative list of pure-Python packages that actually shipped is the PYZ
archive embedded in each .exe. This script reads that archive directly. The format comes
from PyInstaller's own readers; the two readers below are a stdlib reimplementation so the
tool needs nothing installed.

Evidence, in order of trust:

  1. PYZ contents of both .exe files          what pure-Python code is really inside
  2. ``*.dist-info`` folders in the bundle    the exact versions PyInstaller kept
  3. package folders on disk in the bundle    compiled packages PyInstaller unpacked
  4. NATIVE_COMPONENTS below                  DLLs and programs no metadata describes

Every top-level import name found that way is mapped back to the distribution that provides
it, using the metadata in the BUILD virtualenv. Version, license and license text all come
from that metadata or from the bundle; nothing is invented and nothing is downloaded. Where
the bundle and the build environment disagree about a version, the bundle wins and the
disagreement is printed.

Prior art: pip-licenses (MIT) does the metadata half and accepts ``--python <venv python>``.
It is not installed in Attune's build environment, this repo does not add a build-time
dependency for a once-per-release document, and it could not do the other half anyway:
pip-licenses reports the whole virtualenv rather than what PyInstaller put in the box, and
it knows nothing about the DLLs inside the wheels.

Stdlib only. No network. Never runs either .exe. Writes only under --out and --licenses-dir.

    python tools/gen_third_party_notices.py --app dist/Attune \
        --venv ../mixer-ng/.venv-standalone

Run it with the build environment's own python, so that the standard-library name list and
the marshal format match the interpreter that froze the app. THIS IS ENFORCED, not
advised: the tool reads the Python version out of each frozen program's own PyInstaller
cookie and refuses (exit 2) when the interpreter running it is a different major.minor.
Measured 2026-09-21: under Python 3.14 the tool reported four genuine 3.12
standard-library modules (_compression, aifc, chunk, sunau, all removed in 3.13) as
unattributed, because ``sys.stdlib_module_names`` is the RUNNING interpreter's list.
Under the build environment's 3.12 it reports zero. A legal document that changes with
whichever python happened to run it is not a document; hence the refusal.

Exit codes:  0 clean
             1 a declared native component is missing, a component has no license text, or
               a top-level name could not be attributed
             2 bad input (missing app folder or build environment, unreadable archive, or
               an interpreter whose major.minor differs from the one that froze the app)
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import json
import marshal
import os
import re
import shutil
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

from importlib.metadata import Distribution, DistributionFinder

BACKSLASH = chr(92)

# ---------------------------------------------------------------------------------------
# PyInstaller archive readers, reimplemented on the stdlib.
#
# Formats read out of PyInstaller 6.21:
#   PyInstaller/archive/readers.py          CArchiveReader   (the PKG appended to the .exe)
#   PyInstaller/loader/pyimod01_archive.py  ZlibArchiveReader (the PYZ inside the PKG)
# ---------------------------------------------------------------------------------------

_COOKIE_MAGIC = b"MEI\014\013\012\013\016"
_COOKIE_FORMAT = "!8sIIII64s"      # magic, archive length, toc offset, toc length, pyvers, pylib
_COOKIE_LENGTH = struct.calcsize(_COOKIE_FORMAT)
_TOC_ENTRY_FORMAT = "!IIIIBc"      # entry length, offset, length, uncompressed length, zflag, type
_TOC_ENTRY_LENGTH = struct.calcsize(_TOC_ENTRY_FORMAT)
_PYZ_MAGIC = b"PYZ\0"


class ArchiveError(RuntimeError):
    pass


def _find_cookie(fp) -> int:
    """Scan backwards from end of file for the PKG cookie. Returns its offset, or -1."""
    fp.seek(0, os.SEEK_END)
    end = fp.tell()
    chunk = 8192
    overlap = len(_COOKIE_MAGIC) - 1
    while end >= len(_COOKIE_MAGIC):
        start = max(end - chunk, 0)
        fp.seek(start, os.SEEK_SET)
        data = fp.read(end - start)
        pos = data.rfind(_COOKIE_MAGIC)
        if pos != -1:
            return start + pos
        if start == 0:
            break
        end = start + overlap
    return -1


def read_frozen_archive(exe_path: Path) -> tuple[list[str], list[str]]:
    """Return (module names in the PYZ, names of the other PKG entries) for a frozen .exe."""
    with open(exe_path, "rb") as fp:
        cookie_at = _find_cookie(fp)
        if cookie_at == -1:
            raise ArchiveError(f"no PyInstaller PKG cookie in {exe_path}")
        fp.seek(cookie_at, os.SEEK_SET)
        (_magic, archive_length, toc_offset, toc_length,
         _pyvers, pylib_name) = struct.unpack(_COOKIE_FORMAT, fp.read(_COOKIE_LENGTH))
        if not pylib_name.rstrip(b"\0"):
            raise ArchiveError(f"no Python library name in the cookie of {exe_path}")

        # Every TOC entry offset is relative to the start of the PKG, not the file.
        archive_start = (cookie_at + _COOKIE_LENGTH) - archive_length

        fp.seek(archive_start + toc_offset, os.SEEK_SET)
        toc_data = fp.read(toc_length)

        pyz_entry = None
        other_entries: list[str] = []
        cursor = 0
        while cursor + _TOC_ENTRY_LENGTH <= len(toc_data):
            (entry_length, offset, _length, _ulen, compressed, typecode) = struct.unpack(
                _TOC_ENTRY_FORMAT, toc_data[cursor:cursor + _TOC_ENTRY_LENGTH])
            if entry_length < _TOC_ENTRY_LENGTH:
                raise ArchiveError(f"corrupt PKG table of contents in {exe_path}")
            raw_name = toc_data[cursor + _TOC_ENTRY_LENGTH:cursor + entry_length]
            name = raw_name.split(b"\0", 1)[0].decode("utf-8", "replace")
            if typecode == b"z" and pyz_entry is None:
                pyz_entry = (offset, compressed)
            else:
                other_entries.append(name)
            cursor += entry_length

        if pyz_entry is None:
            raise ArchiveError(f"no PYZ entry in the PKG of {exe_path}")

        offset, compressed = pyz_entry
        if compressed:
            # PyInstaller leaves the PYZ entry itself uncompressed; the modules inside it are
            # compressed one by one. Fail loudly rather than read garbage if that changes.
            raise ArchiveError(
                f"the PYZ entry in {exe_path} is compressed; this reader needs updating")

        pyz_start = archive_start + offset
        fp.seek(pyz_start, os.SEEK_SET)
        if fp.read(len(_PYZ_MAGIC)) != _PYZ_MAGIC:
            raise ArchiveError(f"PYZ magic mismatch in {exe_path}")
        fp.read(4)                                     # the frozen interpreter's bytecode magic
        (pyz_toc_offset,) = struct.unpack("!i", fp.read(4))
        fp.seek(pyz_start + pyz_toc_offset, os.SEEK_SET)
        try:
            raw_toc = marshal.load(fp)                 # a list of (name, entry) pairs
            toc = dict(raw_toc)
        except Exception as exc:                       # noqa: BLE001 - reported, not swallowed
            raise ArchiveError(
                f"could not read the PYZ index of {exe_path}: {exc!r}. Run this script with "
                "the build environment's own python, the same minor version that froze the "
                "app."
            ) from exc
        if not toc or not all(isinstance(k, str) for k in toc):
            raise ArchiveError(f"the PYZ index of {exe_path} is not a module table")
    return sorted(toc), sorted(other_entries)


def frozen_python_version(exe_path: Path) -> tuple[int, int]:
    """The major.minor of the Python that froze this .exe, from its own PKG cookie.

    PyInstaller writes the version into the cookie as one integer, major*100+minor
    (312 for 3.12), so it is read straight out of the binary and cannot be stale.
    """
    with open(exe_path, "rb") as fp:
        cookie_at = _find_cookie(fp)
        if cookie_at == -1:
            raise ArchiveError(f"no PyInstaller PKG cookie in {exe_path}")
        fp.seek(cookie_at, os.SEEK_SET)
        (_magic, _archive_length, _toc_offset, _toc_length,
         pyvers, _pylib_name) = struct.unpack(_COOKIE_FORMAT, fp.read(_COOKIE_LENGTH))
    return (pyvers // 100, pyvers % 100)


def interpreter_mismatch(frozen: tuple[int, int], running: tuple[int, int],
                         program: str = "", venv: Path | None = None) -> str | None:
    """A refusal message when the running interpreter is not the one that froze the app,
    or None when it is. Pure, so a test can hold it without a build on disk."""
    if tuple(frozen[:2]) == tuple(running[:2]):
        return None
    hint = (f"{venv}{os.sep}Scripts{os.sep}python.exe" if venv is not None
            else "the build environment's own python")
    return (f"[notices] REFUSING: {program or 'the frozen app'} was frozen by Python "
            f"{frozen[0]}.{frozen[1]} and this tool is running under Python "
            f"{running[0]}.{running[1]}. The standard-library list and the bytecode format "
            f"are read from the RUNNING interpreter, so the answer would be wrong in a way "
            f"the document cannot show. Run it with {hint}.")


# ---------------------------------------------------------------------------------------
# License text collection
# ---------------------------------------------------------------------------------------

_LICENSE_STEMS = ("LICENSE", "LICENCE", "COPYING", "COPYRIGHT", "NOTICE", "AUTHORS",
                  "THIRDPARTY", "THIRD-PARTY", "THIRD_PARTY", "PATENTS")
_LICENSE_SUFFIXES = {"", ".txt", ".md", ".rst", ".html", ".thirdparty", ".apache", ".bsd",
                     ".mit", ".gpl", ".lgpl", ".third-party", ".third_party"}
_MAX_LICENSE_BYTES = 512 * 1024


def _looks_like_license(path_name: str) -> bool:
    stem, _, suffix = path_name.rpartition(".")
    if not stem:
        stem, suffix = path_name, ""
    else:
        suffix = "." + suffix
    flat = re.sub(r"[^A-Za-z]", "", stem).upper()
    if not any(flat.startswith(k.replace("-", "").replace("_", "")) for k in _LICENSE_STEMS):
        return False
    return suffix.lower() in _LICENSE_SUFFIXES


_CLASSIFIER_RE = re.compile(r"^License :: (?:OSI Approved :: )?(.+)$")


@dataclass
class Component:
    name: str
    version: str = ""
    version_source: str = ""
    license: str = ""
    url: str = ""
    kind: str = "python"                        # python | native
    programs: set[str] = field(default_factory=set)
    top_levels: set[str] = field(default_factory=set)
    license_paths: list[str] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    note: str = ""
    ambiguous_with: list[str] = field(default_factory=list)

    def as_json(self) -> dict:
        data = dict(self.__dict__)
        data["programs"] = sorted(self.programs)
        data["top_levels"] = sorted(self.top_levels)
        return data


def _site_packages(venv_root: Path) -> Path:
    for candidate in (venv_root / "Lib" / "site-packages",
                      venv_root / "lib" / "site-packages"):
        if candidate.is_dir():
            return candidate
    for candidate in sorted((venv_root / "lib").glob("python*/site-packages")):
        if candidate.is_dir():
            return candidate
    raise SystemExit(f"[notices] no site-packages under {venv_root}")


def _base_prefix(venv_root: Path) -> Path | None:
    """The Python installation a virtualenv was made from, per its pyvenv.cfg `home` line."""
    cfg = venv_root / "pyvenv.cfg"
    if not cfg.is_file():
        return None
    for line in cfg.read_text(encoding="utf-8", errors="replace").splitlines():
        key, _, value = line.partition("=")
        if key.strip().lower() == "home":
            home = Path(value.strip())
            if home.is_dir():
                return home
    return None


def _dist_info_dir(dist: Distribution, site: Path) -> Path | None:
    path = getattr(dist, "_path", None)
    if path is not None and Path(path).is_dir():
        return Path(path)
    name = (dist.metadata["Name"] or "").replace("-", "_")
    for pattern in (f"{name}-*.dist-info", f"{name}-*.egg-info"):
        hits = sorted(site.glob(pattern))
        if hits:
            return hits[0]
    return None


def _license_of(dist: Distribution) -> str:
    md = dist.metadata
    expr = (md.get("License-Expression") or "").strip()
    if expr:
        return expr
    classifiers = [m.group(1) for m in
                   (_CLASSIFIER_RE.match(c) for c in md.get_all("Classifier") or []) if m]
    classifiers = [c for c in classifiers if c.lower() != "other/proprietary license"]
    raw = (md.get("License") or "").strip()
    # Some packages dump their whole license text into the License field. Only trust it as an
    # identifier when it is short and on one line.
    if raw and "\n" not in raw and len(raw) <= 60 \
            and raw.lower() not in {"unknown", "see license file", "see licence file"}:
        return raw
    if classifiers:
        return "; ".join(classifiers)
    return "not declared - read the license text"


def _url_of(dist: Distribution) -> str:
    md = dist.metadata
    home = (md.get("Home-page") or "").strip()
    if home:
        return home
    entries = md.get_all("Project-URL") or []
    for wanted in ("homepage", "source", "repository", "source code", "github", "documentation"):
        for entry in entries:
            label, _, url = entry.partition(",")
            if label.strip().lower() == wanted:
                return url.strip()
    if entries:
        return entries[0].partition(",")[2].strip()
    return ""


def _strip_module_suffix(name: str) -> str:
    for suffix in (".py", ".pyd", ".so", ".pyi"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return ""


def _top_levels_of(dist: Distribution, info_dir: Path | None) -> set[str]:
    """Import names a distribution provides, from top_level.txt and from RECORD."""
    names: set[str] = set()
    if info_dir is not None:
        top = info_dir / "top_level.txt"
        if top.is_file():
            for line in top.read_text(encoding="utf-8", errors="replace").splitlines():
                line = line.strip().replace(BACKSLASH, "/")
                if line and not line.startswith("#"):
                    names.add(line.split("/")[0])
    # RECORD as well as top_level.txt, never instead of it: modern wheels often have no
    # top_level.txt, and some that do list data directories rather than import names.
    for file in dist.files or []:
        parts = Path(str(file)).parts
        if not parts:
            continue
        head = parts[0]
        if head.endswith((".dist-info", ".egg-info", ".data")) or head in {"..", "."}:
            continue
        if len(parts) == 1:
            stem = _strip_module_suffix(head)
            if stem:
                names.add(stem.split(".")[0])
        else:
            names.add(head)
    dist_name = (dist.metadata["Name"] or "").strip()
    if dist_name:
        names.add(dist_name.replace("-", "_"))
    return {n for n in names if n and n.isidentifier()}


def load_build_env(site: Path) -> tuple[dict[str, list[Distribution]], dict[str, Path | None]]:
    """Map every top-level import name in the build environment to the distributions with it."""
    context = DistributionFinder.Context(path=[str(site)])
    by_top: dict[str, list[Distribution]] = {}
    info_dirs: dict[str, Path | None] = {}
    for dist in Distribution.discover(context=context):
        name = (dist.metadata["Name"] or "").strip()
        if not name:
            continue
        info_dir = _dist_info_dir(dist, site)
        info_dirs[name] = info_dir
        for top in _top_levels_of(dist, info_dir):
            by_top.setdefault(top, []).append(dist)
    return by_top, info_dirs


def _pick_owner(top: str, candidates: list[Distribution]) -> tuple[Distribution, list[str]]:
    """Choose the distribution that owns a top-level name; report any it is shared with."""
    if len(candidates) == 1:
        return candidates[0], []
    exact = [d for d in candidates
             if (d.metadata["Name"] or "").replace("-", "_").lower() == top.lower()]
    others = sorted({(d.metadata["Name"] or "").strip() for d in candidates})
    if exact:
        return exact[0], [n for n in others
                          if n.replace("-", "_").lower() != top.lower()]
    # A shared namespace such as `google`. Pick deterministically and say so out loud.
    chosen = sorted(candidates, key=lambda d: (d.metadata["Name"] or "").lower())[0]
    return chosen, [n for n in others if n != (chosen.metadata["Name"] or "").strip()]


def _copy_license_files(dist: Distribution, info_dir: Path | None, site: Path,
                        dest: Path) -> list[str]:
    """Copy every license text a distribution ships, wherever in site-packages it sits."""
    sources: list[tuple[Path, Path]] = []      # (source file, path to use under dest)

    for file in dist.files or []:
        rel = Path(str(file))
        if not _looks_like_license(rel.name):
            continue
        try:
            src = Path(dist.locate_file(file)).resolve()
        except Exception:                      # noqa: BLE001 - a broken RECORD row, skip it
            continue
        if not src.is_file():
            continue
        parts = list(rel.parts)
        # Drop the `<name>-<version>.dist-info` and its `licenses/` wrapper from the layout.
        if parts and parts[0].endswith((".dist-info", ".egg-info")):
            parts = parts[1:]
            if parts and parts[0] == "licenses":
                parts = parts[1:]
        sources.append((src, Path(*parts) if parts else Path(rel.name)))

    if info_dir is not None and info_dir.is_dir():
        scan = [p for p in info_dir.rglob("*") if p.is_file() and _looks_like_license(p.name)]
        for src in scan:
            rel = src.relative_to(info_dir)
            parts = list(rel.parts)
            if parts and parts[0] == "licenses":
                parts = parts[1:]
            sources.append((src, Path(*parts) if parts else Path(src.name)))

    copied: list[str] = []
    seen: set[Path] = set()
    for src, rel in sources:
        if src in seen:
            continue
        seen.add(src)
        if src.stat().st_size > _MAX_LICENSE_BYTES:
            continue
        target = dest / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
        copied.append(str(target).replace(BACKSLASH, "/"))
    return sorted(copied)


# ---------------------------------------------------------------------------------------
# Native components: everything no metadata tool can see.
#
# `files` are glob patterns relative to the built app folder, and the script checks every one
# of them, so a row cannot quietly go stale when the build changes. `license_from` is a list of
# globs pointing at license texts already on disk, either inside the bundle or, prefixed with
# "venv:", inside the build environment's site-packages. Anything hand-placed under
# licenses/<slug>/ is picked up as well. `text_unavailable` records, in words, why a component
# has no text to ship.
# ---------------------------------------------------------------------------------------

NATIVE_COMPONENTS = [
    dict(name="FFmpeg", license="GPL-3.0-or-later",
         url="https://ffmpeg.org/",
         files=["analyzer/_internal/attune/bin/ff*.exe"],
         ffmpeg_build_info=True,
         note="Attune runs it as a separate program and never links it. It is bundled so "
              "that Attune can decode files libsndfile cannot: m4a, aac and wma, and the "
              "mp3 files whose last fraction of a second carries stray bytes libsndfile "
              "refuses to step over. The version and the configure flags below are read "
              "out of the binary on every run, not written here by hand. Because this is "
              "a static build, the libraries listed under it are compiled inside the "
              "executable and are conveyed under FFmpeg's own GPL version 3; FFmpeg's "
              "LICENSE.md at the upstream link records each one's own terms."),
    dict(name="libsndfile", license="LGPL-2.1-or-later",
         url="https://libsndfile.github.io/libsndfile/",
         files=["analyzer/_internal/_soundfile_data/libsndfile_x64.dll"],
         license_from=["analyzer/_internal/_soundfile_data/COPYING"],
         note="Inside the soundfile wheel. Decodes wav, flac, ogg and opus."),
    dict(name="libsoxr", license="LGPL-2.1-or-later",
         url="https://sourceforge.net/projects/soxr/",
         files=["analyzer/_internal/soxr/*.pyd"],
         license_from=["venv:soxr-*.dist-info/licenses/*"],
         note="Statically linked into the python-soxr extension module, so there is no "
              "separate DLL to point at. Also statically present inside ffmpeg.exe, which is "
              "built with --enable-libsoxr. Resamples audio to the rate the model expects."),
    dict(name="OpenBLAS", license="BSD-3-Clause",
         url="https://github.com/OpenMathLib/OpenBLAS/",
         files=["_internal/numpy.libs/libscipy_openblas*.dll",
                "analyzer/_internal/numpy.libs/libscipy_openblas*.dll",
                "analyzer/_internal/scipy.libs/libscipy_openblas*.dll"],
         license_from=["venv:numpy-*.dist-info/licenses/LICENSE.txt"],
         note="Inside the numpy and scipy wheels, with the GCC runtime libraries it needs "
              "linked in statically. numpy's own LICENSE.txt carries the bundled texts, "
              "including the GCC Runtime Library Exception, and is copied here."),
    dict(name="LLVM", license="Apache-2.0 WITH LLVM-exception",
         url="https://llvm.org/",
         files=["analyzer/_internal/llvmlite/binding/llvmlite.dll"],
         license_from=["analyzer/_internal/llvmlite-*.dist-info/licenses/LICENSE.thirdparty"],
         # LICENSE.thirdparty only, NOT LICENSE: the latter is llvmlite's own BSD-2
         # license from Anaconda, which belongs to the llvmlite row, not to LLVM.
         note="Inside llvmlite.dll, which is how numba compiles the feature extractor to "
              "machine code at run time."),
    # Intel oneTBB was declared here until 2026-09-21, pointing at
    # analyzer/_internal/tbb12.dll. That DLL is in the build of 2026-09-01 and is NOT in
    # the build made today from the recorded build environment, so numba's TBB threading
    # layer cannot load and it uses another one. Leaving the row in made
    # THIRD_PARTY_NOTICES.md say a component was inside the bundle when it was not, which
    # is a false statement in a legal document, so the row is gone rather than the gap
    # being tolerated. Nothing is lost if it comes back: an undeclared tbb12.dll lands in
    # "unattributed binaries", which is a gate, and licenses/intel-onetbb/LICENSE.txt is
    # still on disk. To restore it, put this row back and re-add the HAND_PLACED entry.
    dict(name="Microsoft OpenMP runtime", license="Microsoft Visual C++ redistributable terms",
         url="https://learn.microsoft.com/cpp/parallel/openmp/",
         files=["analyzer/_internal/VCOMP140.DLL"],
         text_unavailable="Redistributed with the scikit-learn and numba wheels under the "
                          "Visual C++ redistributable terms. Microsoft ships no license file "
                          "alongside the DLL.",
         note="Parallel loops inside scikit-learn and numba."),
    dict(name="OpenSSL", license="Apache-2.0",
         url="https://www.openssl.org/",
         files=["_internal/libcrypto-3.dll", "_internal/libssl-3.dll",
                "analyzer/_internal/libcrypto-3.dll", "analyzer/_internal/libssl-3.dll"],
         note_extra="CPython's own LICENSE.txt does not carry OpenSSL's text, so the copy "
                    "here was taken from the OpenSSL project itself; licenses/SOURCES.md "
                    "records where and when.",
         note="Part of the Python runtime, and behind any https request Attune makes. "
              "Attune reaches the network only where you point it at something: a Plex "
              "server you configured, or a MusicIP instance on your own machine."),
    dict(name="SQLite", license="Public domain",
         url="https://www.sqlite.org/copyright.html",
         files=["_internal/sqlite3.dll", "analyzer/_internal/sqlite3.dll"],
         note="Part of the Python runtime. Holds your library database. SQLite's authors "
              "placed it in the public domain, so there is no licence to reproduce; the "
              "file under licenses/sqlite is this project's note recording that, not a "
              "licence text."),
    dict(name="libffi", license="MIT",
         url="https://sourceware.org/libffi/",
         files=["_internal/libffi-8.dll", "analyzer/_internal/libffi-8.dll"],
         license_from=["base:LICENSE.txt"],
         note="Part of the Python runtime, behind ctypes and cffi. Its licence text is "
              "inside CPython's own LICENSE.txt, in the section headed 'libffi', which is "
              "the copy shipped here."),
    dict(name="CPython", license="PSF-2.0",
         url="https://docs.python.org/3/license.html",
         files=["_internal/python312.dll", "analyzer/_internal/python312.dll",
                "analyzer/_internal/python3.dll"],
         license_from=["base:LICENSE.txt"],
         note="The Python runtime itself, frozen into both programs."),
    dict(name="Microsoft Visual C++ runtime",
         license="Microsoft Visual C++ redistributable terms",
         url="https://learn.microsoft.com/cpp/windows/latest-supported-vc-redist",
         files=["_internal/vcruntime140*.dll", "_internal/msvcp140*.dll",
                "analyzer/_internal/vcruntime140*.dll", "analyzer/_internal/msvcp140*.dll"],
         text_unavailable="Microsoft's redistributable runtime carries no license file with "
                          "the DLLs; the terms are at the upstream link.",
         note="Required by everything on this list that was compiled with MSVC."),
    dict(name="Microsoft Edge WebView2", license="Microsoft WebView2 SDK terms",
         url="https://developer.microsoft.com/microsoft-edge/webview2/",
         files=["_internal/webview/lib/runtimes/win-x64/native/WebView2Loader.dll",
                "_internal/webview/lib/runtimes/win-x86/native/WebView2Loader.dll",
                "_internal/webview/lib/runtimes/win-arm64/native/WebView2Loader.dll",
                "_internal/webview/lib/Microsoft.Web.WebView2.Core.dll",
                "_internal/webview/lib/Microsoft.Web.WebView2.WinForms.dll"],
         text_unavailable="Redistributed inside the pywebview wheel without a license file. "
                          "The terms are at the upstream link.",
         note="Draws Attune's window using the Edge runtime already on the machine."),
    dict(name="ONNX Runtime (native)", license="MIT",
         url="https://onnxruntime.ai/",
         files=["_internal/onnxruntime/capi/onnxruntime*.dll",
                "analyzer/_internal/onnxruntime/capi/onnxruntime*.dll"],
         license_from=["analyzer/_internal/onnxruntime/LICENSE",
                       "analyzer/_internal/onnxruntime/ThirdPartyNotices.txt"],
         note="Runs the CLAP model and the optional learned head. Its own "
              "ThirdPartyNotices.txt ships beside it in the bundle."),
    dict(name="pythonnet .NET bridge", license="MIT",
         url="https://github.com/pythonnet/pythonnet",
         files=["_internal/pythonnet/runtime/Python.Runtime.dll"],
         license_from=["venv:pythonnet-*.dist-info/licenses/LICENSE"],
         note="pywebview reaches WebView2 through pythonnet, and the MIT license here is "
              "pythonnet's own, covering Python.Runtime.dll. The System.*.dll and "
              "netstandard.dll assemblies beside it are Microsoft .NET reference "
              "assemblies: pythonnet redistributes them, but they are Microsoft's "
              "copyright under Microsoft's .NET terms, not pythonnet's to license."),
]


# ---------------------------------------------------------------------------------------
# Third-party code ported into Attune's own source files.
#
# Attune's code is MIT, but a few files carry a port of someone else's work, and a notices
# file that lists only packages and DLLs would miss them. Same shape as NATIVE_COMPONENTS
# with one addition: `marker` is a string that must still be present in the shipped file. If
# somebody strips the attribution comment, the tool notices instead of the rights holder.
# ---------------------------------------------------------------------------------------

VENDORED_SOURCE = [
    dict(name="Hugging Face Transformers (log-mel front-end)", license="Apache-2.0",
         url="https://github.com/huggingface/transformers",
         files=["analyzer/_internal/attune/src/embed_onnx.py"],
         marker="audio_utils.py (Apache-2.0)",
         note="The log-mel front-end in embed_onnx.py is a numpy port of the exact numeric "
              "path in transformers 5.13.0 (feature_extraction_clap.py and audio_utils.py), "
              "written so the analyzer can produce the same features as the CLAP model "
              "expects without importing torch. The port is bit-exact by design and the "
              "attribution is in the file's own comments. Attune's code around it is MIT."),
]


# ---------------------------------------------------------------------------------------
# Licence texts committed to this repository by hand, because the component ships none in
# the bundle or the wheel. Each records where it came from and when, so the folder is
# auditable instead of a pile of files nobody can source. The generator never fetches
# anything: it checks these exist and writes licenses/SOURCES.md from this table.
# ---------------------------------------------------------------------------------------

HAND_PLACED = [
    dict(slug="ffmpeg", file="COPYING.GPLv3", retrieved="2026-09-20",
         source="https://www.gnu.org/licenses/gpl-3.0.txt",
         why="The GPL version 3 text, for the bundled FFmpeg build."),
    # intel-onetbb's hand-placed Apache-2.0 text was here. It goes back the moment the
    # component row above does; the text itself is still at licenses/intel-onetbb/.
    dict(slug="openssl", file="LICENSE.txt", retrieved="2026-09-20",
         source="https://raw.githubusercontent.com/openssl/openssl/master/LICENSE.txt",
         why="OpenSSL 3 is Apache-2.0; CPython's LICENSE.txt does not include it."),
    dict(slug="flatbuffers", file="LICENSE", retrieved="2026-09-20",
         source="https://raw.githubusercontent.com/google/flatbuffers/master/LICENSE",
         why="The flatbuffers wheel declares Apache-2.0 but ships no text."),
    dict(slug="proxy-tools", file="LICENSE.txt", retrieved="2026-09-20",
         source="https://raw.githubusercontent.com/jtushman/proxy_tools/master/LICENSE.txt",
         why="The proxy_tools wheel ships no text. Its metadata declares MIT while the file "
             "the project publishes is a 3-clause BSD text; both are permissive and what "
             "upstream publishes is what is shipped here."),
    dict(slug="hugging-face-transformers-log-mel-front-end", file="LICENSE",
         retrieved="2026-09-20",
         source="https://www.apache.org/licenses/LICENSE-2.0.txt",
         why="Apache-2.0, for the port of Transformers code in src/embed_onnx.py."),
    dict(slug="sqlite", file="PUBLIC-DOMAIN-NOTE.txt", retrieved="2026-09-20",
         source="written by this project, recording https://www.sqlite.org/copyright.html",
         why="SQLite has no licence to reproduce. This is a note saying so, not a licence."),
]

# Where a package's own declared licence is true but incomplete enough to mislead.
PYTHON_LICENSE_NOTES = {
    "pyinstaller":
        "The classifier says GPLv2. PyInstaller's own COPYING.txt is GPL-2.0-or-later WITH "
        "a bootloader exception that lets the bootloader ship inside an application under "
        "any licence, and its runtime hooks are Apache-2.0. PyInstaller's package tree ends "
        "up frozen into both programs as a side effect of the build; nothing in Attune "
        "calls it while the app is running.",
    "proxy_tools":
        "The wheel's metadata declares MIT. The licence file the project publishes, and the "
        "one shipped here, is a 3-clause BSD text. Both are permissive.",
}


# ---------------------------------------------------------------------------------------
# Bundle inspection
# ---------------------------------------------------------------------------------------

PROGRAMS = (
    ("Attune.exe", "Attune.exe", "_internal"),
    ("AttuneAnalyzer.exe", "analyzer/AttuneAnalyzer.exe", "analyzer/_internal"),
)

# Names that are not third-party distributions: PyInstaller's own runtime shims and Attune's
# own modules. The standard library is filtered separately, by name.
_OWN_NAMES = {"attune", "app_desktop", "worker_entry"}
_PYI_SHIMS = ("pyimod", "pyiboot", "pyi_rth")

_BINARY_SUFFIXES = {".dll", ".pyd", ".exe", ".so"}


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fp:
        for block in iter(lambda: fp.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def bundle_evidence(app: Path):
    """Collect (top-level name -> programs), (dist-info versions), (problems)."""
    tops: dict[str, set[str]] = {}
    versions: dict[str, dict[str, str]] = {}
    problems: list[str] = []

    for label, exe_rel, internal_rel in PROGRAMS:
        exe = app / exe_rel
        internal = app / internal_rel
        if not exe.is_file():
            problems.append(f"missing program: {exe_rel}")
            continue
        modules, _others = read_frozen_archive(exe)
        for module in modules:
            tops.setdefault(module.split(".")[0], set()).add(label)
        if not internal.is_dir():
            problems.append(f"missing folder: {internal_rel}")
            continue
        for child in sorted(internal.iterdir()):
            if child.name.endswith(".dist-info"):
                stem = child.name[: -len(".dist-info")]
                dist_name, _, version = stem.rpartition("-")
                versions.setdefault(dist_name, {})[label] = version
            elif child.is_dir() and not child.name.startswith("_") \
                    and not child.name.endswith(".libs") and child.name.isidentifier():
                tops.setdefault(child.name, set()).add(label)
            elif child.suffix.lower() == ".pyd":
                # .pyd only. A loose .dll at this level is a native library, never an import
                # name, and every one of them is covered by NATIVE_COMPONENTS. Drop the ABI
                # tag: _cffi_backend.cp312-win_amd64.pyd is imported as _cffi_backend.
                stem = child.stem.split(".")[0]
                if stem.isidentifier():
                    tops.setdefault(stem, set()).add(label)

    return tops, versions, problems


_FFMPEG_CONFIG_RE = re.compile(rb"--enable-gpl[ -~]{0,8000}")
_FFMPEG_VERSION_RE = re.compile(rb"[0-9][0-9.]{1,10}-[A-Za-z0-9_]+-www\.gyan\.dev"
                                rb"|N-[0-9]+-g[0-9a-f]+")


def ffmpeg_build_info(path: Path) -> dict:
    """Read an ffmpeg build's version and configure flags out of the binary itself.

    The banner ffmpeg prints for -version is stored as a plain string inside the executable,
    so the identifying facts can be read without running it. This is what keeps the FFmpeg
    entry honest: swap the binary for a different build and the notices change with it,
    instead of repeating a version somebody typed here once.
    """
    data = path.read_bytes()
    config = _FFMPEG_CONFIG_RE.search(data)
    version = _FFMPEG_VERSION_RE.search(data)
    flags = re.findall(rb"--(?:enable|disable)-[a-z0-9-]+",
                       config.group(0)) if config else []
    decoded = [f.decode() for f in flags]
    return dict(
        version=version.group(0).decode() if version else "",
        gpl="--enable-gpl" in decoded,
        version3="--enable-version3" in decoded,
        nonfree="--enable-nonfree" in decoded,
        libraries=sorted({f[len("--enable-"):] for f in decoded
                          if f.startswith("--enable-lib")}),
    )


def _slug(name: str) -> str:
    """Folder-safe name. Underscores and hyphens are the same character for matching, which
    is how PyPI normalizes distribution names (lazy-loader and lazy_loader are one project)."""
    return re.sub(r"[^A-Za-z0-9.-]+", "-", name.replace("_", "-")).strip("-.").lower()


def _expand(app: Path, pattern: str) -> list[str]:
    base, _, tail = pattern.rpartition("/")
    folder = app / base if base else app
    if not folder.is_dir():
        return []
    return [f"{base}/{p.name}" if base else p.name
            for p in sorted(folder.iterdir())
            if p.is_file() and fnmatch.fnmatch(p.name.lower(), tail.lower())]


def main() -> int:
    here = Path(__file__).resolve().parent
    repo = here.parent
    ap = argparse.ArgumentParser(
        description="Generate THIRD_PARTY_NOTICES.md and licenses/ from the built app.")
    ap.add_argument("--app", default=str(repo / "dist" / "Attune"),
                    help="built app folder, read only")
    ap.add_argument("--venv", default=str(repo.parent / "mixer-ng" / ".venv-standalone"),
                    help="build virtualenv whose metadata describes the bundled packages")
    ap.add_argument("--out", default=str(repo / "THIRD_PARTY_NOTICES.md"))
    ap.add_argument("--licenses-dir", default=str(repo / "licenses"))
    ap.add_argument("--json", default="", help="optional machine-readable dump")
    ap.add_argument("--lenient", action="store_true",
                    help="report gaps but still exit 0; the default is to fail on them")
    args = ap.parse_args()

    app = Path(args.app).resolve()
    venv = Path(args.venv).resolve()
    out = Path(args.out).resolve()
    lic_dir = Path(args.licenses_dir).resolve()

    if not app.is_dir():
        print(f"[notices] built app folder not found: {app}", file=sys.stderr)
        return 2
    if not venv.is_dir():
        print(f"[notices] build environment not found: {venv}", file=sys.stderr)
        return 2

    # The interpreter gate, before anything is read or written. Both programs are
    # checked: they are frozen by the same environment today, and if that ever stops
    # being true this is where it shows.
    for label, exe_rel, _internal_rel in PROGRAMS:
        exe = app / exe_rel
        if not exe.is_file():
            continue                      # bundle_evidence() reports the missing program
        try:
            frozen = frozen_python_version(exe)
        except ArchiveError as exc:
            print(f"[notices] {exc}", file=sys.stderr)
            return 2
        refusal = interpreter_mismatch(frozen, sys.version_info[:2], label, venv)
        if refusal:
            print(refusal, file=sys.stderr)
            return 2
        print(f"[notices] {label}: frozen by Python {frozen[0]}.{frozen[1]}, "
              f"running under {sys.version_info[0]}.{sys.version_info[1]}: match")

    site = _site_packages(venv)
    by_top, info_dirs = load_build_env(site)
    try:
        tops, bundle_versions, problems = bundle_evidence(app)
    except ArchiveError as exc:
        print(f"[notices] {exc}", file=sys.stderr)
        return 2

    lic_dir.mkdir(parents=True, exist_ok=True)

    stdlib = set(sys.stdlib_module_names)
    components: dict[str, Component] = {}
    unmapped: list[str] = []
    skipped_stdlib = 0

    for top, programs in sorted(tops.items()):
        if top in _OWN_NAMES or top.startswith(_PYI_SHIMS):
            continue
        candidates = by_top.get(top)
        if not candidates:
            if top in stdlib:
                skipped_stdlib += 1
            else:
                unmapped.append(top)
            continue
        dist, shared_with = _pick_owner(top, candidates)
        name = (dist.metadata["Name"] or top).strip()
        comp = components.get(name)
        if comp is None:
            info_dir = info_dirs.get(name)
            comp = Component(
                name=name,
                version=(dist.metadata["Version"] or "").strip(),
                version_source="build environment",
                license=_license_of(dist),
                url=_url_of(dist),
                kind="python",
            )
            folder = lic_dir / _slug(name)
            comp.license_paths = _copy_license_files(dist, info_dir, site, folder)
            # A text hand-placed in the repo counts too: some wheels ship none at all.
            if folder.is_dir():
                comp.license_paths = sorted(
                    {str(f).replace(BACKSLASH, "/") for f in folder.rglob("*") if f.is_file()})
            components[name] = comp
        comp.programs |= programs
        comp.top_levels.add(top)
        for other in shared_with:
            if other not in comp.ambiguous_with:
                comp.ambiguous_with.append(other)

    # The bundle's own dist-info wins over the build environment on version, because it is
    # what actually shipped.
    version_notes: list[str] = []
    for dist_name, per_program in sorted(bundle_versions.items()):
        match = next((c for c in components.values()
                      if _slug(c.name) == _slug(dist_name)), None)
        if match is None:
            version_notes.append(f"{dist_name}: a dist-info in the bundle that no frozen "
                                 "module claims")
            continue
        for label, version in sorted(per_program.items()):
            if version and match.version and version != match.version:
                version_notes.append(
                    f"{match.name}: the bundle carries {version} ({label}), the build "
                    f"environment has {match.version}; the bundle wins")
                match.version = version
                match.version_source = "the bundle"
            elif version:
                match.version_source = "the bundle and the build environment agree"

    # Native components, every declared file checked and hashed.
    native_missing: list[str] = []
    pattern_gaps: list[str] = []
    natives: list[Component] = []
    vendored_source: list[Component] = []
    attributed_files: set[str] = set()
    for row, bucket, kind in ([(r, natives, "native") for r in NATIVE_COMPONENTS]
                              + [(r, vendored_source, "vendored") for r in VENDORED_SOURCE]):
        found: list[str] = []
        empty_patterns: list[str] = []
        for pattern in row["files"]:
            hits = _expand(app, pattern)
            if not hits:
                # Every pattern is checked on its own. Testing only the aggregate lets one
                # matching file hide a pattern that has gone stale, which is how the
                # WebView2 loader hid behind the Core DLL until an auditor found it.
                empty_patterns.append(pattern)
            found.extend(hits)
        comp = Component(name=row["name"], license=row["license"], url=row.get("url", ""),
                         kind=kind, note=row.get("note", ""))
        for pattern in empty_patterns:
            comp.evidence.append(f"PATTERN MATCHED NOTHING: {pattern}")
            pattern_gaps.append(f"{row['name']}: no file matches {pattern}")
        for rel in found:
            path = app / rel
            comp.evidence.append(f"{rel}  ({path.stat().st_size:,} bytes, "
                                 f"sha256 {_sha256(path)[:16]})")
            attributed_files.add(rel.lower())
        if not found:
            native_missing.append(row["name"])
            comp.evidence.append("NOT FOUND IN THE BUNDLE")
        marker = row.get("marker")
        if marker and found:
            carried = [rel for rel in found
                       if marker in (app / rel).read_text(encoding="utf-8",
                                                          errors="replace")]
            if carried:
                comp.evidence.append(
                    f"attribution still in the shipped file: {marker!r} in {carried[0]}")
            else:
                native_missing.append(f"{row['name']} (attribution comment gone)")
                comp.evidence.append(
                    f"ATTRIBUTION MISSING: {marker!r} is no longer in the shipped file")
        if row.get("ffmpeg_build_info") and found:
            info = ffmpeg_build_info(app / found[0])
            terms = []
            if info["gpl"]:
                terms.append("--enable-gpl")
            if info["version3"]:
                terms.append("--enable-version3")
            if info["nonfree"]:
                terms.append("--enable-nonfree")
            comp.evidence.append(
                f"read from the binary: version {info['version'] or 'unknown'}, "
                f"built with {' '.join(terms) or 'no GPL flags'}")
            if info["nonfree"]:
                pattern_gaps.append(
                    "FFmpeg: the bundled build has --enable-nonfree, which cannot be "
                    "redistributed at all. Stop and replace it.")
            if not (info["gpl"] and info["version3"]):
                pattern_gaps.append(
                    "FFmpeg: the bundled build no longer reports --enable-gpl "
                    "--enable-version3; the licence stated for it is now wrong.")
            if info["libraries"]:
                comp.evidence.append(
                    f"statically linked inside it ({len(info['libraries'])}): "
                    + ", ".join(info["libraries"]))
        comp.note = " ".join(x for x in (comp.note, row.get("note_extra", ""),
                                         row.get("text_unavailable", "")) if x).strip()

        for src_spec in row.get("license_from", []):
            if src_spec.startswith("venv:"):
                hits = sorted(site.glob(src_spec[len("venv:"):]))
            elif src_spec.startswith("base:"):
                base = _base_prefix(venv)
                hits = sorted(base.glob(src_spec[len("base:"):])) if base else []
            else:
                hits = sorted(app.glob(src_spec))
            for hit in hits:
                if not hit.is_file() or hit.stat().st_size > _MAX_LICENSE_BYTES:
                    continue
                dest = lic_dir / _slug(row["name"]) / hit.name
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(hit, dest)
                comp.license_paths.append(str(dest).replace(BACKSLASH, "/"))
        # A text hand-placed in the repo counts too.
        hand = lic_dir / _slug(row["name"])
        if hand.is_dir():
            for f in sorted(hand.rglob("*")):
                p = str(f).replace(BACKSLASH, "/")
                if f.is_file() and p not in comp.license_paths:
                    comp.license_paths.append(p)
        comp.license_paths = sorted(set(comp.license_paths))
        bucket.append(comp)

    # Anything binary in the bundle that neither a native row nor a mapped package accounts
    # for. Reported as folders, not as 400 file names.
    package_folders = {t.lower() for c in components.values() for t in c.top_levels}
    own_programs = {exe_rel.lower() for _label, exe_rel, _internal in PROGRAMS}
    unattributed: list[str] = []
    for path in sorted(app.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in _BINARY_SUFFIXES:
            continue
        rel = path.relative_to(app).as_posix()
        if rel.lower() in attributed_files or rel.lower() in own_programs:
            continue
        parts = rel.split("/")
        bare = parts[-1].split(".")[0]          # the import name, with any ABI tag dropped
        owner = None
        for part in list(parts) + [bare]:
            stem = part[:-5] if part.endswith(".libs") else part
            if stem.lower() in package_folders:
                owner = stem
                break
        if owner:
            continue
        # A loose extension module named after a standard-library module is part of CPython,
        # which is already listed: _ssl.pyd, select.pyd, unicodedata.pyd and the rest.
        if bare.lstrip("_") and (bare in stdlib or bare.lstrip("_") in stdlib):
            continue
        unattributed.append(rel)

    # License texts sitting in the bundle that belong to something vendored inside a package.
    vendored: list[str] = []
    for path in app.rglob("*"):
        if path.is_file() and _looks_like_license(path.name):
            rel = path.relative_to(app).as_posix()
            if ".dist-info" not in rel:
                vendored.append(rel)

    all_comps = list(components.values()) + natives + vendored_source
    hand_missing: list[str] = []
    for row in HAND_PLACED:
        if not (lic_dir / row["slug"] / row["file"]).is_file():
            hand_missing.append(f"{row['slug']}/{row['file']}")
    _write_sources(lic_dir, hand_missing)

    no_text = [c.name for c in all_comps if not c.license_paths]

    _write_markdown(out, sorted(components.values(), key=lambda c: c.name.lower()),
                    natives, vendored_source, unmapped, version_notes, unattributed,
                    vendored, no_text, app)

    if args.json:
        Path(args.json).write_text(json.dumps(dict(
            app=str(app), venv=str(venv),
            python=[c.as_json() for c in components.values()],
            native=[c.as_json() for c in natives],
            vendored_source=[c.as_json() for c in vendored_source],
            unmapped=unmapped, version_notes=version_notes,
            unattributed_binaries=unattributed,
            vendored_license_files=vendored,
            no_license_text=no_text, problems=problems), indent=2), encoding="utf-8")

    print(f"[notices] python distributions   : {len(components)}")
    print(f"[notices] native components      : {len(natives)}")
    print(f"[notices] ported source          : {len(vendored_source)}")
    print(f"[notices] hand-placed texts      : {len(HAND_PLACED)}"
          + (f"  MISSING: {', '.join(hand_missing)}" if hand_missing else ", all present"))
    print(f"[notices] standard-library names : {skipped_stdlib} skipped")
    print(f"[notices] unattributed top-levels: {len(unmapped)}"
          + (f"  -> {', '.join(unmapped)}" if unmapped else ""))
    print(f"[notices] without license text   : {len(no_text)}"
          + (f"  -> {', '.join(no_text)}" if no_text else ""))
    print(f"[notices] unattributed binaries  : {len(unattributed)}"
          + (f"  -> {', '.join(unattributed[:6])}" if unattributed else ""))
    for line in version_notes:
        print(f"[notices] version                : {line}")
    for line in problems:
        print(f"[notices] problem                : {line}")
    print(f"[notices] wrote {out}")
    print(f"[notices] licenses under {lic_dir}")

    if problems:
        return 2
    gaps = []
    if native_missing:
        gaps.append("declared native components missing from the bundle: "
                    + ", ".join(native_missing))
    if unmapped:
        gaps.append("top-level names nothing in the build environment claims: "
                    + ", ".join(unmapped))
    if unattributed:
        gaps.append(f"binary files nothing accounts for ({len(unattributed)}): "
                    + ", ".join(unattributed[:6]))
    gaps.extend(pattern_gaps)
    if hand_missing:
        gaps.append("licence texts this repository is supposed to carry are missing: "
                    + ", ".join(hand_missing))
    if version_notes:
        gaps.extend(version_notes)
    declared_no_text = {r["name"] for r in NATIVE_COMPONENTS if r.get("text_unavailable")}
    text_gaps = [c.name for c in natives + vendored_source
                 if not c.license_paths and c.name not in declared_no_text]
    text_gaps += [c.name for c in components.values() if not c.license_paths]
    if text_gaps:
        gaps.append("no license text found for: " + ", ".join(sorted(set(text_gaps))))
    if gaps:
        for gap in gaps:
            print(f"[notices] GAP: {gap}", file=sys.stderr)
        if not args.lenient:
            return 1
    return 0


def _write_sources(lic_dir: Path, missing: list[str]) -> None:
    """Record where every hand-placed licence text came from, so the folder is auditable."""
    lines = ["# Where the hand-placed licence texts came from",
             "",
             "Most of the texts under `licenses/` are copied straight out of the package "
             "that ships them, by `tools/gen_third_party_notices.py`, every time it runs. "
             "The ones below are not: those components ship no licence text at all, inside "
             "the bundle or inside their wheel, so the text was fetched once by hand and "
             "committed. This file records the source and the date for each, because a "
             "licence text nobody can trace back is not much better than no licence text.",
             "",
             "The generator itself never touches the network. It checks these files exist "
             "and fails if one has gone missing.",
             "",
             "| Component folder | File | Taken from | When | Why by hand |",
             "|---|---|---|---|---|"]
    for row in HAND_PLACED:
        state = "" if (lic_dir / row["slug"] / row["file"]).is_file() else " **MISSING**"
        lines.append(f"| `{row['slug']}`{state} | `{row['file']}` | {row['source']} "
                     f"| {row['retrieved']} | {row['why']} |")
    lines += ["", "Everything else under `licenses/` is generated. Do not edit it by hand; "
                  "re-run the generator instead.", ""]
    if missing:
        lines += ["**Missing right now:** " + ", ".join(missing) + ".", ""]
    (lic_dir / "SOURCES.md").write_text("\n".join(lines), encoding="utf-8")


def _write_markdown(out: Path, python_comps: list[Component], natives: list[Component],
                    vendored_source: list[Component],
                    unmapped: list[str], version_notes: list[str],
                    unattributed: list[str], vendored: list[str],
                    no_text: list[str], app: Path) -> None:
    def rel(p: str) -> str:
        try:
            return str(Path(p).relative_to(out.parent)).replace(BACKSLASH, "/")
        except ValueError:
            return p

    lines: list[str] = []
    add = lines.append
    add("# Third-party notices")
    add("")
    add("Every component listed here is actually inside the installed Attune application on "
        "Windows, with the license it is offered under. This file is generated by "
        "`tools/gen_third_party_notices.py`, which reads the built app and the build "
        "environment, so it cannot drift from what ships. Full license texts are in "
        "[`licenses/`](licenses/), one folder per component.")
    add("")
    add("This file is informational, not legal advice.")
    add("")
    add("[NOTICE.md](NOTICE.md) says which license governs the built program as a whole, and "
        "why.")
    add("")
    add("Regenerate with:")
    add("")
    add("```")
    add("python tools/gen_third_party_notices.py --app dist/Attune \\")
    add("    --venv ../mixer-ng/.venv-standalone")
    add("```")
    add("")
    add("## Python packages inside the two frozen programs")
    add("")
    add("`Attune.exe` is the window and the local web server. `AttuneAnalyzer.exe` is the "
        "separate program that reads and analyzes audio files. A package marked `both` is "
        "inside each of them.")
    add("")
    add("| Component | Version | License | In | License text |")
    add("|---|---|---|---|---|")
    for c in python_comps:
        where = "both" if len(c.programs) == 2 else (
            "app" if "Attune.exe" in c.programs else "analyzer")
        texts = ", ".join(f"[{Path(p).name}]({rel(p)})" for p in c.license_paths[:2]) or "-"
        if len(c.license_paths) > 2:
            texts += f" (+{len(c.license_paths) - 2} more)"
        name = f"[{c.name}]({c.url})" if c.url.startswith("http") else c.name
        if c.ambiguous_with:
            name += f" <br><sub>shares a namespace with {', '.join(c.ambiguous_with)}</sub>"
        add(f"| {name} | {c.version} | {c.license} | {where} | {texts} |")
    add("")
    notes = [(c.name, PYTHON_LICENSE_NOTES[c.name])
             for c in python_comps if c.name in PYTHON_LICENSE_NOTES]
    if notes:
        add("")
        add("Two of those labels come straight from the package's own metadata and are "
            "narrower than the real terms:")
        add("")
        for name, text in notes:
            add(f"- **{name}**: {text}")
        add("")
    add("## Native libraries and programs inside the bundle")
    add("")
    add("No Python metadata describes these. Each one names the file it was observed as, with "
        "its size and the first 16 characters of its SHA-256, so the claim can be checked "
        "against the bundle.")
    add("")
    for c in natives:
        add(f"### {c.name}")
        add("")
        add(f"- License: **{c.license}**")
        if c.url:
            add(f"- Upstream: {c.url}")
        for line in c.evidence:
            add(f"- Observed as: `{line}`")
        if c.note:
            add(f"- {c.note}")
        if c.license_paths:
            add("- License text: " + ", ".join(f"[{Path(p).name}]({rel(p)})"
                                               for p in c.license_paths))
        else:
            add("- License text: not shipped by the component. Follow the upstream link.")
        add("")
    add("## Third-party code ported into Attune's own files")
    add("")
    add("Attune's own code is MIT, but a few files carry a port of somebody else's work. "
        "They are listed here because a notices file that covered only packages and DLLs "
        "would miss them.")
    add("")
    for c in vendored_source:
        add(f"### {c.name}")
        add("")
        add(f"- License: **{c.license}**")
        if c.url:
            add(f"- Upstream: {c.url}")
        for line in c.evidence:
            add(f"- {line}")
        if c.note:
            add(f"- {c.note}")
        if c.license_paths:
            add("- License text: " + ", ".join(f"[{Path(p).name}]({rel(p)})"
                                               for p in c.license_paths))
        else:
            add("- License text: MISSING. Place it under licenses/ and re-run.")
        add("")
    add("## What the generator could not account for")
    add("")
    add("This section is written by the tool, not by hand. An empty list is a result, not an "
        "omission.")
    add("")
    if unmapped:
        add("Top-level names inside the frozen programs that no distribution in the build "
            "environment claims:")
        add("")
        for name in unmapped:
            add(f"- `{name}`")
    else:
        add("- Every top-level name inside the frozen programs was attributed to a "
            "distribution or to the Python standard library.")
    add("")
    if no_text:
        add("Components with no license text in this repository, each explained in its entry "
            "above: " + ", ".join(sorted(no_text)) + ".")
        add("")
    if version_notes:
        add("Version notes:")
        add("")
        for line in version_notes:
            add(f"- {line}")
        add("")
    if unattributed:
        add(f"Binary files ({len(unattributed)}) that neither an entry above nor the Python "
            "standard library accounts for. They are named rather than counted, so the gap "
            "is visible instead of assumed:")
        add("")
        for path in unattributed[:25]:
            add(f"- `{path}`")
        if len(unattributed) > 25:
            add(f"- ... and {len(unattributed) - 25} more")
        add("")
    else:
        add("- Every binary file in the bundle is accounted for by an entry above or by the "
            "Python standard library.")
        add("")
    if vendored:
        add("License texts found loose inside the bundle, which usually means a package "
            "vendored someone else's code. They stay where the build put them and are listed "
            "here so nobody has to go looking:")
        add("")
        for path in sorted(vendored)[:40]:
            add(f"- `{path}`")
        if len(vendored) > 40:
            add(f"- ... and {len(vendored) - 40} more")
        add("")
    add("---")
    add("")
    add(f"Generated from the built app at `{app.name}`. Re-run the generator after any change "
        "to what the build bundles, and commit the result with it.")
    add("")
    out.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
