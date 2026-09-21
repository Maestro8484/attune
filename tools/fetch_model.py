#!/usr/bin/env python3
"""fetch_model.py - download the CLAP audio model this repo does not ship.

`src/models/clap_music.onnx` is 263 MiB. Keeping it in the repository meant every clone paid
for it, needed the git-lfs add-on installed to get anything but a 134-byte stub, and drew on a
shared monthly bandwidth allowance. So it is published once as a release asset instead, and
this script fetches it and checks it before anything trusts it.

    python tools/fetch_model.py              # fetch if missing, verify, put it in place
    python tools/fetch_model.py --check      # verify what is already there, download nothing
    python tools/fetch_model.py --force      # fetch again even if a good copy is present

This is for people running Attune from source. The frozen Windows application carries the
model inside its installer, so an installed copy never runs this script. Note for whoever
builds that installer: the build copies the model out of `src/models/`, so run this script
(or `--check` it) before building from a fresh clone, or the build will happily bundle a
134-byte stub.

Standard library only, on purpose: the runtime environment has neither `requests` nor `pooch`,
and the one thing that must work on a stranger's bare Python is the step that gets the model.
The discipline (publish the asset, pin the URL, verify a hash before use) is borrowed from
rembg, which does the same thing through pooch.

Exit codes:
  0  a verified copy is in place
  1  nothing usable is there and nothing is wrong with the file either: it is simply absent,
     or the destination cannot be written to, or --dest names a directory
  2  something IS there and it is not the model: wrong hash, wrong size, or an LFS stub
  3  the download failed: no network, a bad URL, an unpublished asset, a transfer cut short
"""

from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

# --- the pinned artifact ----------------------------------------------------------------------
# Publish the file under this tag once, and this URL is stable forever after:
#   gh release create model-clap-music-v1 src/models/clap_music.onnx --title ... --notes ...
MODEL_URL = ("https://github.com/Maestro8484/attune/releases/download/"
             "model-clap-music-v1/clap_music.onnx")
MODEL_SHA256 = "02679faef2868832d051e4fa9c97f3be197887987b8b1cdd21b3a1b8a51a70ff"
MODEL_BYTES = 275930879

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_DEST = os.path.join(REPO_ROOT, "src", "models", "clap_music.onnx")

LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"
CHUNK = 1024 * 1024
NET_TIMEOUT = 60          # seconds, per connect and per read
SLACK_BYTES = 4096        # allow a hair over the expected size before calling it runaway


def _runnable_path() -> str:
    """How to name this script in a fix-it sentence. Relative when the reader is standing in or
    under the repo, absolute otherwise, because a line of `..\\..\\..` helps nobody."""
    try:
        rel = os.path.relpath(os.path.abspath(__file__), os.getcwd())
    except ValueError:
        return os.path.abspath(__file__)   # different Windows drives have no relative path
    return rel if not rel.startswith("..") else os.path.abspath(__file__)


def public_source(url: str) -> str:
    """The URL with any credentials and query string dropped. A pinned public release URL is
    harmless to print, but `--url` and ATTUNE_MODEL_URL can carry a token or a signed query,
    and this string goes into error messages people paste into issues."""
    try:
        p = urllib.parse.urlsplit(url)
    except ValueError:
        return "(unparseable url)"
    host = p.hostname or ""
    if p.port:
        host = f"{host}:{p.port}"
    return f"{p.scheme}://{host}{p.path}" + (" (query dropped)" if p.query else "")


def file_sha256(path: str) -> tuple[str, int] | None:
    """(hex digest, bytes), or None if it cannot be read at all - a directory, a permission
    problem, a file another process has locked."""
    h = hashlib.sha256()
    total = 0
    try:
        with open(path, "rb") as fh:
            while True:
                chunk = fh.read(CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                h.update(chunk)
    except (OSError, MemoryError):
        return None
    return h.hexdigest(), total


def is_lfs_pointer(path: str) -> bool:
    """A checkout made without git-lfs leaves a small text stub where the model should be.
    Reads a bounded prefix: the file may be a quarter of a gigabyte of binary."""
    try:
        with open(path, "rb") as fh:
            return fh.read(len(LFS_POINTER_PREFIX)) == LFS_POINTER_PREFIX
    except OSError:
        return False


def describe_existing(path: str, want_sha: str, want_bytes: int) -> str:
    """One word for what is sitting at `path`: ok, absent, lfs-stub, or mismatch."""
    if not os.path.exists(path):
        return "absent"
    if os.path.isdir(path):
        return "is-a-directory"
    if is_lfs_pointer(path):
        return "lfs-stub"
    read = file_sha256(path)
    if read is None:
        return "unreadable"
    got_sha, got_bytes = read
    if got_sha == want_sha and got_bytes == want_bytes:
        return "ok"
    return "mismatch"


def download(url: str, dest: str, want_sha: str, want_bytes: int) -> int:
    """Stream to a temp file beside `dest`, verify, then rename into place. Returns an exit code.

    An unverified temp file is always removed: a half-downloaded model sitting where a model
    belongs is the failure this whole script exists to prevent. The single exception is a
    download that verified but could not be renamed, usually because something else has the
    destination open; that one is kept and its path is printed, because it is correct and it
    is a quarter of a gigabyte."""
    dest_dir = os.path.dirname(dest) or "."
    try:
        os.makedirs(dest_dir, exist_ok=True)
    except OSError as e:
        print(f"fetch_model: cannot create {dest_dir}: {e}")
        return 1

    source = public_source(url)
    print(f"fetch_model: source {source}")
    print(f"fetch_model: expecting {want_bytes:,} bytes, sha256 {want_sha}")

    # outside the download try on purpose: "cannot write here" and "cannot reach the server"
    # are different problems, and reporting the first as the second sends people to check
    # their network when the answer is a read-only folder.
    try:
        fd, tmp_path = tempfile.mkstemp(prefix=".clap_music.", suffix=".part", dir=dest_dir)
    except OSError as e:
        print(f"fetch_model: FAILED - cannot write into {dest_dir}: {e}")
        print("            That is a local permissions or disk problem, not a download one.")
        return 1

    placed = False
    kept_tmp = None
    interrupted = False
    stream_done = False
    h = hashlib.sha256()
    got = 0
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "attune-fetch-model/1"})
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(request,
                                                                timeout=NET_TIMEOUT) as resp:
            declared = resp.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) != want_bytes:
                print(f"fetch_model: server offers {int(declared):,} bytes, not {want_bytes:,}. "
                      f"Continuing; the hash decides.")
            next_mark = 10
            while True:
                chunk = resp.read(CHUNK)
                if not chunk:
                    break
                if got + len(chunk) > want_bytes + SLACK_BYTES:
                    print(f"fetch_model: FAILED - the response is larger than the expected "
                          f"{want_bytes:,} bytes. Stopped at {got + len(chunk):,}.")
                    return 2
                # write FIRST, then count it. The other order means a failing write (disk
                # full, quota) leaves the running hash and byte count describing bytes that
                # never reached the file, and the cleanup below then promotes a short file
                # over a good model because the two agree with each other.
                out.write(chunk)
                got += len(chunk)
                h.update(chunk)
                pct = got * 100 // max(want_bytes, 1)
                if pct >= next_mark:
                    print(f"  {pct:3d}%  {got:,} bytes", flush=True)
                    next_mark = pct - (pct % 10) + 10
        # only here, with the read loop finished AND the file object closed by the `with`,
        # is the temp file known to hold exactly the bytes the hash describes
        stream_done = True
    except urllib.error.HTTPError as e:
        print(f"fetch_model: FAILED - HTTP {e.code} from {source}")
        if e.code == 404:
            print("            That release asset does not exist yet. If you are the repo "
                  "owner, publish it;\n            otherwise check the project's install "
                  "instructions for the current link.")
        return 3
    except KeyboardInterrupt:
        interrupted = True
        return 3
    except Exception as e:
        # deliberately broad: http.client.IncompleteRead, ssl errors, proxy oddities and
        # decompression failures are none of them OSError, and a raw traceback from a
        # download tool is the wrong face to show a stranger.
        print(f"fetch_model: FAILED - could not download from {source}: "
              f"{e.__class__.__name__}: {e}")
        return 3
    finally:
        if tmp_path and os.path.exists(tmp_path):
            got_sha = h.hexdigest()
            if stream_done and got == want_bytes and got_sha == want_sha:
                try:
                    os.replace(tmp_path, dest)
                    tmp_path, placed = None, True
                except OSError as e:
                    # Windows refuses to replace a file another process holds open. The
                    # download is verified and this may be a quarter of a gigabyte: keep it
                    # and say where, rather than making them fetch it twice.
                    kept_tmp = tmp_path
                    tmp_path = None
                    print(f"fetch_model: could not move the file into place: {e}")
                    print(f"            the verified download was KEPT at {kept_tmp}")
                    print(f"            close whatever is using the model, then rename it to "
                          f"{dest}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
        if interrupted:
            print("\nfetch_model: interrupted."
                  + (" The verified file was still put in place." if placed
                     else " The partial file was removed."))

    got_sha = h.hexdigest()
    if got < want_bytes:
        print(f"fetch_model: FAILED - the download stopped early at {got:,} of the expected "
              f"{want_bytes:,} bytes.\n            Nothing was written. Run it again.")
        return 3
    if got != want_bytes or got_sha != want_sha:
        print("fetch_model: FAILED - what arrived is not the model this repo expects. "
              "Nothing was written.")
        print(f"            expected {want_bytes:,} bytes, sha256 {want_sha}")
        print(f"            got      {got:,} bytes, sha256 {got_sha}")
        return 2
    if not placed:
        return 1

    print(f"fetch_model: OK - verified and in place: {dest}")
    return 0


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="fetch_model.py",
        description="Download and verify the CLAP audio model (src/models/clap_music.onnx).")
    ap.add_argument("--url", default=None,
                    help="download from here instead of the pinned release asset "
                         "(also: ATTUNE_MODEL_URL)")
    ap.add_argument("--dest", default=DEFAULT_DEST, help=f"where to put it (default: {DEFAULT_DEST})")
    ap.add_argument("--sha256", default=MODEL_SHA256, help="expected sha256 (default: the pinned one)")
    ap.add_argument("--bytes", type=int, default=MODEL_BYTES, dest="want_bytes",
                    help="expected size in bytes (default: the pinned one)")
    ap.add_argument("--check", action="store_true",
                    help="verify whatever is already there and download nothing")
    ap.add_argument("--force", action="store_true",
                    help="download again even if a verified copy is present")
    args = ap.parse_args(argv[1:])

    url = args.url or os.environ.get("ATTUNE_MODEL_URL") or MODEL_URL
    dest = os.path.abspath(args.dest)

    state = describe_existing(dest, args.sha256, args.want_bytes)
    if state == "is-a-directory":
        print(f"fetch_model: ERROR - {dest} is a directory. --dest names the FILE to write, "
              f"for example\n            --dest \"{os.path.join(dest, 'clap_music.onnx')}\"")
        return 1
    if args.check:
        if state == "ok":
            print(f"fetch_model: OK - verified: {dest}")
            return 0
        print({
            "absent": f"fetch_model: absent - nothing at {dest}",
            "lfs-stub": f"fetch_model: NOT the model - {dest} is a Git LFS pointer stub, which "
                        f"is what a clone made without git-lfs leaves behind",
            "mismatch": f"fetch_model: MISMATCH - {dest} exists but is not the expected file",
            "unreadable": f"fetch_model: UNREADABLE - {dest} exists but could not be read",
        }[state])
        print(f"            fix: python {_runnable_path()}"
              + ("" if dest == DEFAULT_DEST else f" --dest \"{dest}\""))
        return 2 if state in ("mismatch", "lfs-stub") else 1

    if state == "ok" and not args.force:
        print(f"fetch_model: already present and verified: {dest}")
        return 0
    if state == "lfs-stub":
        print(f"fetch_model: {dest} is a Git LFS pointer stub; replacing it.")
    elif state in ("mismatch", "unreadable"):
        print(f"fetch_model: {dest} is not the expected file; replacing it.")

    return download(url, dest, args.sha256, args.want_bytes)


if __name__ == "__main__":
    sys.exit(main(sys.argv))
