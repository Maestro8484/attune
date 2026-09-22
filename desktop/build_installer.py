"""Compile the Attune Windows installer with Inno Setup, and optionally the portable zip.

    python desktop/build_installer.py
    python desktop/build_installer.py --dist path\\to\\dist\\Attune --out path\\to\\output --zip

Thin driver: finds ISCC.exe, shells it at desktop\\installer\\attune.iss with the
/DSourceDir, /DOutputDirOverride, /DAppVersion and /DLongestRelPath defines, then prints
the resulting installer's real path and size. With --zip it also produces the portable
Attune-<version>-win64.zip from the same folder, and appends the repository's own licence
texts to it (LICENSE_TEXTS below) so the zip carries the same paperwork the installer
lays down beside Attune.exe.

Prior art, not invention: Inno Setup's own command-line compiler (ISCC.exe) already
does everything a build script would otherwise reinvent (dependency-ordered file
copy, compression, uninstaller generation) -- this script only locates it and passes
through what varies between machines and checkouts. The zip likewise prefers 7-Zip
when it is installed and falls back to the standard library's zipfile.

The version comes from ONE place: the VERSION file at the repo root. Nothing here
hardcodes a version number, and the .iss reads the same file when compiled by hand.

SAFETY: this script refuses to package a folder that contains a library database.
desktop\\package.py exists to fold the operator's personal mixer.db into a local test
bundle; a release must never carry one, and running the two scripts in the obvious
order must not be able to produce one.
"""
import argparse
import os
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))          # .../desktop
ATT = os.path.dirname(HERE)                                 # .../<repo root>
ISS = os.path.join(HERE, "installer", "attune.iss")
VERSION_FILE = os.path.join(ATT, "VERSION")

# Same three locations winget and the Inno Setup 6 installer are known to use.
ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
]

SEVENZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
]

# Anything matching these must never appear inside a folder we are about to ship.
# The library database is a SQLite file, and SQLite leaves siblings beside it: -wal and
# -shm while a connection is open, -journal in the older rollback mode. Those siblings
# hold pages of the same database, so they carry the same library paths and track names
# and are just as private as the file itself. The base name varies by convention (.db,
# .sqlite, .sqlite3), so every base gets every sibling. Built rather than typed out, so
# adding a base name cannot leave a sibling behind.
# (The -journal and .sqlite-family siblings were missed on the first pass and found by
# the Codex audit, 2026-09-20.)
_DB_BASES = (".db", ".sqlite", ".sqlite3")
_DB_SIDECARS = ("", "-wal", "-shm", "-journal")
DB_SUFFIXES = tuple(base + side for base in _DB_BASES for side in _DB_SIDECARS)

# A name check is not enough. A renamed copy of the library (mixer.bak, library.dat)
# passes every suffix list there is, so every file in the tree also gets its first 16
# bytes read: that is SQLite's own file header, which is the same whatever the file is
# called. About 4,000 reads on a real build, under a second. Raised by the Fable audit,
# 2026-09-20.
SQLITE_MAGIC = b"SQLite format 3\x00"

# What desktop\build.py produces at the top level of dist\Attune, and nothing else.
# Anything extra is either the operator's own files (a Playlists folder holds .m3u8
# playlists full of their real music paths) or leftovers from desktop\package.py (its
# README.txt names their NAS share and their .env). Neither belongs in a release, and
# neither has a database extension, so the checks above would wave both through.
# Raised by the Fable audit, 2026-09-20, which proved it by running the old guard over a
# folder holding exactly those two things and watching it pass.
EXPECTED_TOP_LEVEL = {"Attune.exe", "_internal", "analyzer"}


def read_version(version_file=VERSION_FILE):
    """The single source of truth for the release version."""
    try:
        with open(version_file, "r", encoding="utf-8") as f:
            v = f.read().strip()
    except OSError as e:
        raise SystemExit(f"[build_installer] cannot read {version_file}: {e}")
    if not v:
        raise SystemExit(f"[build_installer] {version_file} is empty")
    if any(c in v for c in ' \t\r\n"\''):
        raise SystemExit(f"[build_installer] {version_file} must hold one bare version, got {v!r}")
    return v


def find_iscc():
    for c in ISCC_CANDIDATES:
        if c and os.path.isfile(c):
            return c
    raise SystemExit(
        "[build_installer] ISCC.exe not found. Checked:\n  " +
        "\n  ".join(c for c in ISCC_CANDIDATES if c) +
        "\nInstall Inno Setup 6 (e.g. `winget install JRSoftware.InnoSetup`) or pass "
        "its ISCC.exe path with --iscc."
    )


def find_7zip():
    for c in SEVENZIP_CANDIDATES:
        if os.path.isfile(c):
            return c
    return None


def assert_out_is_outside_dist(dist, out_dir):
    """The staging folder must not sit inside the folder being packaged.

    If it did, the zip writer would walk over its own growing archive and the compiled
    installer would become part of the next build's payload. Raised by the Codex audit,
    2026-09-20.
    """
    dist_real = os.path.realpath(dist)
    out_real = os.path.realpath(out_dir)
    if out_real == dist_real or out_real.startswith(dist_real + os.sep):
        raise SystemExit(
            f"[build_installer] the output folder is inside the folder being packaged:\n"
            f"  packaging: {dist_real}\n"
            f"  output:    {out_real}\n"
            f"Point --out somewhere outside the build, ideally outside the repository."
        )


def _looks_like_sqlite(path):
    try:
        with open(path, "rb") as f:
            return f.read(16) == SQLITE_MAGIC
    except OSError:
        return False


def assert_no_database(dist):
    """Refuse to package a folder holding a library database. See the module docstring.

    Two passes: by name, and by SQLite's own file header so a renamed copy cannot slip
    through.
    """
    found = []
    for dp, _dn, fns in os.walk(dist):
        for fn in fns:
            full = os.path.join(dp, fn)
            if fn.lower().endswith(DB_SUFFIXES):
                found.append((full, "database file name"))
            elif _looks_like_sqlite(full):
                found.append((full, "SQLite database, whatever it is named"))
    if found:
        listing = "\n  ".join(f"{p}   ({why})" for p, why in found[:10])
        more = f"\n  ... and {len(found) - 10} more" if len(found) > 10 else ""
        raise SystemExit(
            "[build_installer] REFUSING to package: this folder contains a library "
            "database.\n  " + listing + more +
            "\n\nA released Attune must ship with no database at all; the person who "
            "installs it\nanalyzes their own music and their database is created in "
            "their own user folder.\nA database here is almost certainly the packager's "
            "personal library: it carries every\nfile path, album and track name in it. "
            "desktop\\package.py makes that bundle on\npurpose, for local testing only."
            "\n\nDo not just delete the file and carry on: the same folder may hold "
            "other traces of\nthat run. Rebuild with desktop\\build.py, or point --dist "
            "at a build that has never\nbeen through package.py."
        )


def assert_expected_layout(dist, allow_extra=False):
    """Refuse a build folder with anything unexpected sitting at its top level.

    The top level of a freshly built dist\\Attune holds three entries and nothing else.
    Anything extra arrived some other way: desktop\\package.py's README.txt, a Playlists
    folder the app created next to the exe, a stray log. None of those carry a database
    extension, so the checks above pass them, and a Playlists folder is full of the
    packager's real music paths. Refuse by name and say what to do about it.
    """
    if allow_extra:
        return
    try:
        present = set(os.listdir(dist))
    except OSError as e:
        raise SystemExit(f"[build_installer] cannot list {dist}: {e}")
    extra = sorted(present - EXPECTED_TOP_LEVEL)
    if extra:
        raise SystemExit(
            "[build_installer] REFUSING to package: this build folder holds files that "
            "desktop\\build.py\ndid not put there.\n  " + "\n  ".join(extra) +
            "\n\nExpected exactly: " + ", ".join(sorted(EXPECTED_TOP_LEVEL)) +
            "\n\nExtra files here are usually the packager's own: a Playlists folder is "
            "full of real\nmusic file paths, and desktop\\package.py leaves a README.txt "
            "naming their machine.\nRebuild with desktop\\build.py, or point --dist at a "
            "clean build. If the extra file is\ngenuinely meant to ship, pass "
            "--allow-extra and say so in the release notes."
        )


def longest_relative_path(dist):
    """Length of the deepest path inside the build, relative to its root.

    Windows still refuses most file operations past 259 characters, and this app is a
    PyInstaller tree whose deepest entry is about 120 characters on its own. Add a long
    install directory and the install aborts partway with "MoveFile failed; code 3"
    (observed 2026-09-20 installing into a 173-character folder). Measured here, at the
    folder actually being packaged, and handed to the installer so it can refuse a
    too-long destination up front instead of failing halfway. Measuring beats hardcoding:
    the number moves whenever a dependency adds a deeper module.
    """
    longest = 0
    for dp, _dn, fns in os.walk(dist):
        rel = os.path.relpath(dp, dist)
        prefix = 0 if rel == "." else len(rel) + 1
        for fn in fns:
            longest = max(longest, prefix + len(fn))
    return longest


def compile_installer(dist, out_dir, version, iscc=None, quiet=False):
    """Run ISCC over attune.iss and return the path to the installer it produced.

    Shared by this script's own main() and by tools/make_release.py, so there is one
    invocation of the compiler in the codebase and not two that can drift apart.
    """
    if not os.path.isfile(ISS):
        raise SystemExit(f"[build_installer] script not found: {ISS}")
    # Guard here as well as in the callers. The caller's check runs minutes earlier, and
    # anything that imports this function directly would otherwise bypass it entirely.
    # A walk of the tree costs under a second against a 200-second compile.
    assert_no_database(dist)
    iscc = iscc or find_iscc()
    os.makedirs(out_dir, exist_ok=True)

    cmd = [iscc]
    if quiet:
        cmd.append("/Q")
    deepest = longest_relative_path(dist)
    print(f"[build_installer] deepest path inside the build: {deepest} chars")
    cmd += [
        f"/DSourceDir={dist}",
        f"/DOutputDirOverride={out_dir}",
        f"/DAppVersion={version}",
        f"/DLongestRelPath={deepest}",
        ISS,
    ]
    print(f"[build_installer] ISCC: {iscc}")
    print("[build_installer] " + " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"[build_installer] ISCC FAILED (rc={rc})")

    exe = os.path.join(out_dir, f"AttuneSetup-{version}.exe")
    if not os.path.exists(exe):
        raise SystemExit(
            f"[build_installer] ISCC reported success but {exe} is not there. "
            f"Check {out_dir} for what it actually wrote."
        )
    return exe


ZIP_ROOT = "Attune"

# The project's own licence paperwork, which the build folder never contains: it lives at
# the repository root and the installer picks it up from there at compile time (the four
# "Third-party licence texts" lines in desktop\installer\attune.iss). The portable zip
# used to ship without any of it -- opened on 2026-09-21: 4,502 entries, 132 licence
# files, every one belonging to a bundled Python package, and nothing for Attune itself
# or for the GPL it is conveyed under. This list mirrors the .iss lines one for one, as
# (path relative to the repository root, path relative to the zip's Attune\ folder).
# Keep the two in step: a text the installer carries and the zip does not is the
# defect this exists to close. Added to the archive AFTER it is built rather than copied
# into the build folder, so dist\Attune stays exactly what desktop\build.py produced
# and assert_expected_layout keeps its three-entry rule with no exemption.
LICENSE_TEXTS = [
    ("licenses",                "licenses"),
    ("THIRD_PARTY_NOTICES.md",  "THIRD_PARTY_NOTICES.md"),
    ("NOTICE.md",               "NOTICE.md"),
    ("LICENSE",                 "LICENSE.txt"),
]


def assert_license_sources(repo=ATT, zip_path=None):
    """Refuse when any of the four licence sources is missing from the repository root.

    Called BEFORE the archive is written (make_zip) so a refusal leaves nothing behind,
    and again from add_license_texts as the belt to that braces. If a partly built
    archive exists when this fires, it is removed and the refusal says so: a zip that
    holds the build folder and none of the texts is exactly the file this exists to
    stop, and it must not be left where a person could pick it up. (Cold audit,
    2026-09-21: the first version refused after the archive was already on disk.)
    The installed app is conveyed under GPL-3.0-or-later (NOTICE.md section 3) and the
    GPL wants the texts beside the binary, not only in the source tree.
    """
    missing = [src for src, _dest in LICENSE_TEXTS
               if not os.path.exists(os.path.join(repo, src))]
    if not missing:
        return
    removed = ""
    if zip_path and os.path.exists(zip_path):
        os.remove(zip_path)
        removed = f"\nThe partly built archive was deleted: {zip_path}"
    raise SystemExit(
        "[build_installer] REFUSING to make the zip: licence text(s) missing from "
        "the repository root:\n  " + "\n  ".join(missing) +
        f"\n  looked in: {repo}\nThe installer ships these four beside Attune.exe and "
        "the portable zip must carry the same. Run tools/gen_third_party_notices.py "
        "against the build first if THIRD_PARTY_NOTICES.md or licenses/ is missing."
        + removed)


def add_license_texts(zip_path, root_name=ZIP_ROOT, repo=ATT):
    """Append the repository's licence texts to an existing archive, under root_name."""
    assert_license_sources(repo, zip_path)
    added = 0
    with zipfile.ZipFile(zip_path, "a", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for src, dest in LICENSE_TEXTS:
            full = os.path.join(repo, src)
            if os.path.isdir(full):
                for dp, _dn, fns in os.walk(full):
                    for fn in fns:
                        f = os.path.join(dp, fn)
                        rel = os.path.relpath(f, full).replace("\\", "/")
                        zf.write(f, f"{root_name}/{dest}/{rel}")
                        added += 1
            else:
                zf.write(full, f"{root_name}/{dest}")
                added += 1
    print(f"[build_installer] licence texts added to the zip: {added} files under "
          f"{root_name}/ ({', '.join(d for _s, d in LICENSE_TEXTS)})")
    return added


def _ensure_license_texts(zip_path, root_name=ZIP_ROOT):
    """Verify the four licence entries are really in the archive, after the fact."""
    with zipfile.ZipFile(zip_path) as zf:
        names = set(n.replace("\\", "/") for n in zf.namelist())
    absent = []
    for _src, dest in LICENSE_TEXTS:
        want = f"{root_name}/{dest}"
        if want not in names and not any(n.startswith(want + "/") for n in names):
            absent.append(want)
    if absent:
        raise SystemExit(
            f"[build_installer] zip is missing licence texts it was meant to carry: "
            f"{', '.join(absent)}. Delete {zip_path} and rerun.")


def make_zip(dist, out_dir, version, use_7zip=True, repo=ATT):
    """Portable Attune-<version>-win64.zip, one top-level folder named Attune.

    Unpacking must give the reader a single folder, never 4,000 loose files in their
    Downloads folder, so the layout is checked after the fact rather than assumed.
    The project's own licence texts (LICENSE_TEXTS above) are appended from `repo`
    once the build folder is in, and their presence is checked the same way.
    """
    assert_no_database(dist)   # same reason as in compile_installer
    assert_license_sources(repo)   # before anything is written, so a refusal leaves nothing
    os.makedirs(out_dir, exist_ok=True)
    zip_path = os.path.join(out_dir, f"Attune-{version}-win64.zip")
    if os.path.exists(zip_path):
        os.remove(zip_path)

    dist = os.path.normpath(dist)
    parent, leaf = os.path.split(dist)
    sevenzip = find_7zip() if use_7zip else None

    # 7-Zip stores the named directory as the archive's own top level, but only the
    # folder's real name -- it cannot rename it. So it is used only when the build
    # folder is already called Attune, which is what desktop\build.py produces.
    if sevenzip and leaf == ZIP_ROOT:
        print(f"[build_installer] zipping with 7-Zip: {sevenzip}")
        rc = subprocess.call([sevenzip, "a", "-tzip", "-mx=9", "-bso0", "-bsp0",
                              zip_path, leaf], cwd=parent)
        if rc != 0:
            raise SystemExit(f"[build_installer] 7z FAILED (rc={rc})")
    else:
        if sevenzip:
            print(f"[build_installer] build folder is '{leaf}', not '{ZIP_ROOT}': "
                  f"using Python's zipfile so the archive root can be renamed")
        else:
            print("[build_installer] 7-Zip not found, using Python's zipfile (slower)")
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
            for dp, _dn, fns in os.walk(dist):
                for fn in fns:
                    full = os.path.join(dp, fn)
                    rel = os.path.relpath(full, dist)
                    zf.write(full, (ZIP_ROOT + "/" + rel.replace("\\", "/")))

    add_license_texts(zip_path, ZIP_ROOT, repo)
    _ensure_zip_root(zip_path, ZIP_ROOT)
    _ensure_license_texts(zip_path, ZIP_ROOT)
    size = os.path.getsize(zip_path) / (1 << 20)
    print(f"[build_installer] zip: {zip_path}  ({size:.1f} MiB)")
    return zip_path


def _ensure_zip_root(zip_path, root_name):
    """Verify the archive has exactly one top-level entry, named `root_name`."""
    with zipfile.ZipFile(zip_path) as zf:
        names = zf.namelist()
        tops = {n.replace("\\", "/").split("/")[0] for n in names}
    if tops != {root_name}:
        raise SystemExit(
            f"[build_installer] zip layout wrong: expected one top-level folder "
            f"'{root_name}', got {sorted(tops)[:5]} across {len(names)} entries. "
            f"Delete {zip_path} and rerun with --no-7zip."
        )


def main():
    ap = argparse.ArgumentParser(description="Compile the Attune installer (and optionally the portable zip).")
    ap.add_argument("--dist", default=None,
                    help="PyInstaller one-dir build to package "
                         "(default: attune.iss's own default, this repo's dist\\Attune)")
    ap.add_argument("--out", default=None,
                    help="Directory to write the installer into "
                         "(default: ..\\attune-release\\v<version>, beside the repo)")
    ap.add_argument("--iscc", default=None, help="Explicit path to ISCC.exe")
    ap.add_argument("--zip", action="store_true",
                    help="Also produce the portable Attune-<version>-win64.zip")
    ap.add_argument("--no-7zip", action="store_true",
                    help="Use Python's zipfile even when 7-Zip is installed")
    ap.add_argument("--allow-extra", action="store_true",
                    help="Permit files at the top of the build folder that build.py "
                         "did not put there. Read the refusal before reaching for this.")
    ap.add_argument("--quiet", action="store_true", help="Pass /Q to ISCC (errors only)")
    args = ap.parse_args()

    version = read_version()
    print(f"[build_installer] version {version} (from {VERSION_FILE})")

    dist = os.path.abspath(args.dist) if args.dist else os.path.join(ATT, "dist", "Attune")
    if not os.path.isdir(dist):
        raise SystemExit(f"[build_installer] build folder not found: {dist}")
    assert_expected_layout(dist, allow_extra=args.allow_extra)
    assert_no_database(dist)

    # Default staging sits BESIDE the repository, never inside it. The old default,
    # desktop\installer\Output, is inside the tree and is not gitignored, so a bare run
    # dropped a 470 MB untracked file into the source. (Fable audit, 2026-09-20.)
    out_dir = (os.path.abspath(args.out) if args.out
               else os.path.join(os.path.dirname(ATT), "attune-release", f"v{version}"))
    assert_out_is_outside_dist(dist, out_dir)
    exe = compile_installer(dist, out_dir, version, iscc=args.iscc, quiet=args.quiet)
    print(f"[build_installer] installer: {exe}  ({os.path.getsize(exe) / (1 << 20):.1f} MiB)")

    if args.zip:
        make_zip(dist, out_dir, version, use_7zip=not args.no_7zip)

    return 0


if __name__ == "__main__":
    sys.exit(main())
