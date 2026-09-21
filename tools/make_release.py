#!/usr/bin/env python3
"""make_release.py - turn a built Attune folder into the three files a release is made of.

    python tools/make_release.py --dist <path to dist/Attune> --out <staging folder>

Produces, in the staging folder:
    AttuneSetup-<version>.exe        the Windows installer (Inno Setup)
    Attune-<version>-win64.zip       the same app as a portable folder, no install
    SHA256SUMS.txt                   checksums for both, in sha256sum's own format

Deliberately does NOTHING else. No network, no git, no tagging, no uploading, no
publishing. Creating a tag and a GitHub Release is a human step with a human's
credentials; this script only makes the files that step uploads. See docs/RELEASING.md
for the exact publish commands.

The staging folder should live OUTSIDE the repository. dist/ and build/ are gitignored
but a staging folder full of 400 MB artifacts has no business inside a source tree at
all; the default is a sibling of the repo.

Version comes from the VERSION file at the repo root and from nowhere else.

SAFETY: refuses to package a build folder containing any library database (*.db and
friends). See desktop/package.py, which makes a private test bundle on purpose.

Prior art, not invention: the compile is Inno Setup's own ISCC.exe driven by
desktop/build_installer.py, the zip is 7-Zip or the standard library, and the checksum
file follows GNU coreutils' sha256sum format so `sha256sum -c SHA256SUMS.txt` works as
published everywhere else. Nothing here reimplements any of those.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))     # <repo>/tools
REPO = os.path.dirname(HERE)                          # <repo>
DESKTOP = os.path.join(REPO, "desktop")

# desktop/ is a plain script folder, not an installed package: put it on the path so the
# installer driver and its safety checks are reused rather than copied.
if DESKTOP not in sys.path:
    sys.path.insert(0, DESKTOP)

import build_installer  # noqa: E402  (path set above, on purpose)


def sha256_file(path, chunk=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(chunk), b""):
            h.update(block)
    return h.hexdigest()


def human(n):
    return f"{n / (1 << 20):,.1f} MiB"


def main(argv=None):
    ap = argparse.ArgumentParser(description="Build the Attune release artifacts. No publishing.")
    ap.add_argument("--dist", default=os.path.join(REPO, "dist", "Attune"),
                    help="The built PyInstaller one-dir folder to package "
                         "(default: this repo's dist/Attune)")
    ap.add_argument("--out", default=None,
                    help="Staging folder for the artifacts, outside the repo "
                         "(default: ../attune-release/v<version> next to the repo)")
    ap.add_argument("--iscc", default=None, help="Explicit path to ISCC.exe")
    ap.add_argument("--no-7zip", action="store_true",
                    help="Use Python's zipfile even when 7-Zip is installed")
    ap.add_argument("--allow-extra", action="store_true",
                    help="Permit files at the top of the build folder that build.py "
                         "did not put there. Read the refusal before reaching for this.")
    ap.add_argument("--skip-zip", action="store_true",
                    help="Installer and checksums only (the zip is the slow part)")
    args = ap.parse_args(argv)

    version = build_installer.read_version()
    dist = os.path.abspath(args.dist)
    if not os.path.isdir(dist):
        raise SystemExit(f"[make_release] build folder not found: {dist}\n"
                         f"Build one first with: python desktop/build.py")

    out = os.path.abspath(args.out) if args.out else os.path.join(
        os.path.dirname(REPO), "attune-release", f"v{version}")

    print(f"[make_release] Attune {version}")
    print(f"[make_release] source: {dist}")
    print(f"[make_release] staging: {out}")

    # Every refusal runs before anything at all is produced, the staging folder
    # included: a refused run should leave no trace behind.
    build_installer.assert_out_is_outside_dist(dist, out)
    build_installer.assert_expected_layout(dist, allow_extra=args.allow_extra)
    build_installer.assert_no_database(dist)
    os.makedirs(out, exist_ok=True)

    artifacts = []
    t0 = time.time()

    setup = build_installer.compile_installer(dist, out, version, iscc=args.iscc)
    artifacts.append(setup)
    print(f"[make_release] installer done in {time.time() - t0:.0f}s")

    if not args.skip_zip:
        t1 = time.time()
        artifacts.append(build_installer.make_zip(dist, out, version,
                                                  use_7zip=not args.no_7zip))
        print(f"[make_release] zip done in {time.time() - t1:.0f}s")

    # SHA256SUMS.txt in GNU coreutils format: <hex>, space, '*' for binary mode, then
    # the BARE file name (no path), so it verifies from inside the folder it describes:
    #     sha256sum -c SHA256SUMS.txt
    # Windows without Git Bash: certutil -hashfile <file> SHA256, compare by eye, or
    #     (Get-FileHash <file> -Algorithm SHA256).Hash
    sums_path = os.path.join(out, "SHA256SUMS.txt")
    lines = []
    for path in artifacts:
        digest = sha256_file(path)
        lines.append(f"{digest} *{os.path.basename(path)}")
    header = [
        f"# Attune {version} - SHA-256 checksums",
        "# Verify (Git Bash, WSL, macOS, Linux):  sha256sum -c SHA256SUMS.txt",
        "# Verify (Windows PowerShell):           (Get-FileHash <file> -Algorithm SHA256).Hash",
        "# Lines beginning with # are ignored by sha256sum.",
    ]
    with open(sums_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(header + lines) + "\n")

    print()
    print(f"[make_release] {out}")
    for path in artifacts:
        print(f"  {os.path.basename(path):<34} {human(os.path.getsize(path)):>12}")
    print(f"  {'SHA256SUMS.txt':<34} {human(os.path.getsize(sums_path)):>12}")
    print()
    for line in lines:
        print(f"  {line}")
    print()
    print("[make_release] nothing was published. Next steps: docs/RELEASING.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
