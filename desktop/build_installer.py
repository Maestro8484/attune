"""Compile the Attune Windows installer with Inno Setup.

    python attune/desktop/build_installer.py
    python attune/desktop/build_installer.py --dist path\\to\\dist\\Attune --out path\\to\\output

Thin driver: finds ISCC.exe, shells it at desktop\\installer\\attune.iss with the
/DSourceDir and /DOutputDirOverride defines, prints the resulting installer's path
and size. No new dependencies -- just subprocess + the compiler.

Prior art, not invention: Inno Setup's own command-line compiler (ISCC.exe) already
does everything a build script would otherwise reinvent (dependency-ordered file
copy, compression, uninstaller generation) -- this script only locates it and
passes through the two paths that vary between machines/checkouts.
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))          # .../desktop
ATT = os.path.dirname(HERE)                                 # .../attune (this checkout)
ISS = os.path.join(HERE, "installer", "attune.iss")

# Same three locations winget/the Inno Setup 6 installer are known to use, in the
# order the task that installed it suggested checking.
ISCC_CANDIDATES = [
    r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
    r"C:\Program Files\Inno Setup 6\ISCC.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Programs", "Inno Setup 6", "ISCC.exe"),
]


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


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dist", default=None,
                     help="PyInstaller one-dir build to package "
                          "(default: attune.iss's own default, the main checkout's "
                          "dist\\Attune -- see the comment at the top of attune.iss)")
    ap.add_argument("--out", default=None,
                     help="Directory to write the compiled installer into "
                          "(default: desktop\\installer\\Output)")
    ap.add_argument("--iscc", default=None, help="Explicit path to ISCC.exe")
    ap.add_argument("--quiet", action="store_true", help="Pass /Q to ISCC (errors only)")
    args = ap.parse_args()

    if not os.path.isfile(ISS):
        raise SystemExit(f"[build_installer] script not found: {ISS}")

    iscc = args.iscc or find_iscc()
    print(f"[build_installer] ISCC: {iscc}")

    cmd = [iscc]
    if args.quiet:
        cmd.append("/Q")
    if args.dist:
        dist = os.path.abspath(args.dist)
        if not os.path.isdir(dist):
            raise SystemExit(f"[build_installer] --dist not found: {dist}")
        cmd.append(f"/DSourceDir={dist}")
    if args.out:
        out = os.path.abspath(args.out)
        os.makedirs(out, exist_ok=True)
        cmd.append(f"/DOutputDirOverride={out}")
    cmd.append(ISS)

    print("[build_installer] " + " ".join(cmd))
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SystemExit(f"[build_installer] ISCC FAILED (rc={rc})")

    out_dir = os.path.abspath(args.out) if args.out else os.path.join(HERE, "installer", "Output")
    exe = os.path.join(out_dir, "AttuneSetup-0.1.0.exe")
    if os.path.exists(exe):
        size_mb = os.path.getsize(exe) / 1e6
        print(f"[build_installer] output: {exe}  ({size_mb:.1f} MB)")
    else:
        print(f"[build_installer] NOTE: expected output not found at {exe} "
              f"-- check {out_dir} for the actual filename")


if __name__ == "__main__":
    main()
