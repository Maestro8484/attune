#!/usr/bin/env python3
"""leak_check.py - pre-publish personal-data leak scanner.

Attune (this repo) is developed inside a private working tree that also holds a real,
personally-identifying music library and live databases. This script exists so none of that
reaches the public GitHub repo. Stdlib only, no network, no install.

    python tools/leak_check.py                  # tracked files plus anything not gitignored
    python tools/leak_check.py --all            # tree + history + author addresses
    python tools/leak_check.py --show-values    # same, but print the matching text (LOCAL ONLY)

WHAT IT LOOKS FOR

  RISKY FILENAMES - files that are, by name or location alone, almost certainly personal data:
    any `*.db`, `library.json`, anything matching `*.m3lib*`, and anything under a directory
    named `extracted` or `groundtruth`. A tracked `.leakpatterns` is itself fatal: that file is
    a list of the owner's private strings and must never be published.

  CONTENTS - three rules:
    - generic regexes with NO personal data baked in: any email address other than a
      `noreply` one, and any Windows per-user home path on any drive, written with either
      slash and with the doubled backslashes that JSON encoding produces.
    - machine-specific literal substrings from a `.leakpatterns` file (one per line, `#`
      comments allowed), matched case-insensitively and in the doubled-backslash and
      forward-slash spellings a JSON or a URL would produce. Keep it gitignored: it is checked
      locally and never published. In a worktree or CI checkout the file is absent, so pass
      `--patterns <abs path>`.
    - bulk media filenames: one `.mp3` named in a doc is an example, two hundred in a JSON is
      a dump of somebody's library. Counted, never printed.

  HISTORY (`--history`) - the tree can be clean while the same strings sit in old commits,
    which are just as public. Every reachable blob is read and matched with the SAME rules as
    the tree scan (not handed to git's pickaxe, whose regex dialect differs and whose default
    diff skips merge commits), plus every historical path name, every commit message and every
    annotated tag message. What it does NOT see: the contents of a Git LFS payload (only its
    pointer is in git), a blob over 8 MiB, and any blob whose only filenames carry a binary
    extension. Those are counted and reported as uninspected, which is not the same as clean.
    Fixing a history hit means rewriting history, which is the repo owner's call.

  AUTHORS (`--authors`) - counts author/committer addresses across all history and flags any
    that is not a GitHub `noreply` address. A public repo publishes those forever.

REPORTING RULE: by default this prints rule ids, paths, line numbers and counts, and NEVER the
matched value, so its output can be pasted into a report or an issue. `--show-values` opts in
to previews for fixing things locally.

Exit codes:  0 clean - 1 hit(s) found - 2 path missing/not a directory - 3 scan incomplete
"""

from __future__ import annotations

import argparse
import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

# --- risky filename patterns -----------------------------------------------------------------
RISKY_NAME_PATTERNS = ["*.db", "library.json", "*.m3lib*", "native.db"]
RISKY_DIR_NAMES = {"extracted", "groundtruth"}

# --- generic PII regexes (contain NO personal data - safe to publish) ------------------------
# The home-path rule is deliberately slash-agnostic, case-insensitive and drive-agnostic: a
# profile path written with forward slashes leaks exactly as much as one written with
# backslashes, JSON written by Python is full of both, and a profile can live on any drive.
LEAK_REGEXES = [
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("home-path", re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\r\n\"'<> ]+", re.IGNORECASE)),
]
# A `noreply` address is the deliberately non-identifying kind: GitHub's per-user alias and the
# bot addresses in commit trailers. Flagging them buries the one address that matters under
# dozens that do not. Anything else that matches the email rule is still a hit.
# A noreply address means the mailbox is literally "noreply", or the DOMAIN is a noreply one
# such as users.noreply.github.com. A plain substring test would also exempt an ordinary
# personal mailbox that merely has the word in its local part, which is somebody's real mail.
NOREPLY_RX = re.compile(r"(^noreply@)|(@[^@\s]*\bnoreply\b[^@\s]*$)|(@[^@\s]*\.noreply\.)",
                        re.IGNORECASE)
RULE_IGNORES = {"email": NOREPLY_RX}

# For MASKING, not detection: the detection rule stops at a space so it does not swallow the
# rest of a sentence, but plenty of Windows accounts are named "First Last", and a mask that
# stops at the space prints the surname it was supposed to hide.
HOME_MASK_RX = re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\r\n\"'<>]+", re.IGNORECASE)
EXTRA_PATTERNS_FILE = ".leakpatterns"


def literal_variants(literal: str) -> list[str]:
    """The spellings of one private literal that a file might actually contain.

    A share name lands in a Python-written JSON with its backslashes doubled, in a markdown
    file in whatever case somebody typed, and in a URL with forward slashes. Matching only the
    exact string the owner wrote in .leakpatterns is how a library dump walks straight past a
    scanner that reports clean. All comparisons are lowercased by the caller."""
    low = literal.lower()
    out = {low, low.replace("\\", "\\\\"), low.replace("\\", "/")}
    return sorted(v for v in out if v)

# --- bulk media filenames ---------------------------------------------------------------------
MEDIA_NAME_RX = re.compile(
    r"[^\s\"'<>|/\\]+\.(?:mp3|flac|m4a|ogg|opus|wav|wma|aac|aiff|m3u|m3u8)\b", re.IGNORECASE)
MEDIA_NAME_THRESHOLD = 10

SKIP_DIR_NAMES = {".git", "__pycache__", ".venv", "venv", ".mypy_cache",
                  ".pytest_cache", ".ruff_cache", "node_modules"}

TEXT_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".cfg", ".ini", ".toml", ".yml", ".yaml",
    ".sh", ".ps1", ".bat", ".html", ".htm", ".css", ".js", ".ts", ".csv",
    ".gitignore", ".gitattributes", ".rst",
}

# Never content-scan these, whatever their first bytes look like. A 263 MiB ONNX model has no
# NUL byte early on, so the old "no NULs in the first 4 KB" sniff called it text and the email
# regex matched 40 times inside the weights. Extension first, sniff only as a fallback. These
# files are UNINSPECTED, not clean, and the summary says so out loud.
BINARY_EXTENSIONS = {
    ".onnx", ".pt", ".pth", ".npy", ".npz", ".bin", ".safetensors", ".pkl", ".joblib",
    ".so", ".dll", ".dylib", ".pyd", ".exe", ".lib", ".obj", ".o",
    ".zip", ".7z", ".gz", ".bz2", ".xz", ".tar", ".whl", ".jar",
    ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bmp", ".webp", ".pdf",
    ".mp3", ".flac", ".m4a", ".ogg", ".opus", ".wav", ".wma", ".ttf", ".otf", ".woff", ".woff2",
}

_TAG_SENTINEL = "__leakcheck_tag__ "

# Third-party licence texts are full of their authors' addresses, and the licences require
# them to be reproduced verbatim. Flagging those buries the owner's own address under dozens
# that must stay. Only the email rule is relaxed, and only for these; everything else, private
# literals included, still applies, so a personal path hidden in a licence file is still found.
LICENCE_PATH_RX = re.compile(
    r"(^|[\\/])(licenses?|licences?|third[_-]?party)([\\/]|$)|"
    r"(^|[\\/])(LICEN[SC]E|COPYING|COPYRIGHT|NOTICE|AUTHORS|THIRD[_-]?PARTY)"
    r"[^\\/]*$", re.IGNORECASE)

MAX_BLOB_BYTES = 8 * 1024 * 1024   # history: anything larger is reported as uninspected
LFS_POINTER_PREFIX = b"version https://git-lfs.github.com/spec/v1"

SHOW_VALUES = False                # flipped by --show-values; see _preview()
_LOWER_LITERALS: list[str] = []    # every spelling of every private literal, lowercased


def _force_utf8_stdout() -> None:
    """A report naming a path can carry any character. cp1252 consoles raise on most of them,
    and a scanner that dies halfway through its own findings is worse than no scanner."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError, OSError):
            pass


def safe_path(text: str) -> str:
    """A path with the home-directory part replaced. The scan root, the patterns file and any
    historical path this tool prints are themselves paths, and a path is one of the two things
    this tool exists to keep out of a pasteable report."""
    if SHOW_VALUES:
        return text
    out = HOME_MASK_RX.sub("<home>", text)
    out = LEAK_REGEXES[0][1].sub("<email>", out)   # a path can BE an address
    for lit in _LOWER_LITERALS:
        # one left-to-right pass per literal, never rescanning what was just inserted: a
        # literal that is a substring of its own placeholder (say, "private") otherwise
        # replaces itself forever and the tool hangs instead of printing anything.
        low = out.lower()
        if lit not in low:
            continue
        pieces, i = [], 0
        while True:
            j = low.find(lit, i)
            if j < 0:
                pieces.append(out[i:])
                break
            pieces.append(out[i:j])
            pieces.append("<private>")
            i = j + len(lit)
        out = "".join(pieces)
    return out


def safe_name(path: str, matched: bool) -> str:
    """A path to print. When the NAME itself is what matched a rule, the name is the personal
    value, and safe_path cannot help: it only knows about home folders and listed literals, not
    about somebody's surname in a filename. Print the folder and withhold the name."""
    if SHOW_VALUES or not matched:
        return safe_path(path)
    sep = max(path.rfind("/"), path.rfind("\\"))
    folder = path[:sep + 1] if sep >= 0 else ""
    return safe_path(folder) + "<filename withheld>"


def _preview(text: str) -> str:
    """The matching line, or a placeholder. Default is the placeholder: this tool's own output
    is meant to be pasteable into a report, and a scanner that prints the secret it found has
    simply moved the leak."""
    if not SHOW_VALUES:
        return "(value withheld; re-run with --show-values to see it locally)"
    preview = text.strip()
    return preview[:157] + "..." if len(preview) > 160 else preview


def load_extra_patterns(root: Path, override: str | None = None) -> tuple[list[str], str]:
    """Machine-specific literal substrings. Returns (literals, where-they-came-from).

    `override` (or ATTUNE_LEAKPATTERNS) names the file instead of <root>/.leakpatterns. That
    matters in a git worktree or a CI checkout, where the file is absent because it is ignored
    on purpose: without it the scan silently drops to generic rules and looks cleaner than
    it is."""
    chosen = override or os.environ.get("ATTUNE_LEAKPATTERNS")
    f = Path(chosen).expanduser() if chosen else root / EXTRA_PATTERNS_FILE
    out: list[str] = []
    if f.is_file():
        try:
            # utf-8-sig, because Notepad's "UTF-8 with BOM" glues ﻿ to the first line and
            # strip() does not remove it, which silently kills the first private literal.
            for line in f.read_text(encoding="utf-8-sig", errors="ignore").splitlines():
                s = line.strip().lstrip("﻿").strip()
                if s and not s.startswith("#"):
                    out.append(s)
        except OSError:
            return [], f"{f} unreadable"
    else:
        return [], f"{f} not found"
    # returned raw; the caller masks it AFTER the literals are loaded, or the patterns file's
    # own path (which often sits on the private share) prints unmasked
    return out, str(f)


def _git(root: Path, args: list[str], timeout: int = 300, binary: bool = False):
    """Run a git command under `root`. Returns CompletedProcess, or None if git is unusable.
    core.quotePath=false so non-ASCII paths come back as UTF-8 rather than \\NNN escapes."""
    cmd = ["git", "-C", str(root), "-c", "core.quotePath=false"] + args
    try:
        if binary:
            return subprocess.run(cmd, capture_output=True, timeout=timeout)
        return subprocess.run(cmd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.SubprocessError):
        return None


def publishable_files(root: Path) -> tuple[set[Path] | None, int]:
    """(paths that would reach a public remote, count of those not yet committed), or
    (None, 0) if this is not a git repo.

    Tracked files, PLUS untracked files that no ignore rule covers. The second half matters:
    a scanner that only looks at what is already committed says "clean" about a file you are
    one `git add` away from publishing. Gitignored local data (databases, validation dumps)
    is still excluded, because it genuinely never reaches the remote and scanning it cries
    wolf."""
    tracked = _git(root, ["ls-files", "-z"], timeout=30)
    if tracked is None or tracked.returncode != 0:
        return None, 0
    paths = {(root / rel).resolve() for rel in tracked.stdout.split("\0") if rel}
    others = _git(root, ["ls-files", "-z", "--others", "--exclude-standard"], timeout=30)
    new = set()
    if others is not None and others.returncode == 0:
        new = {(root / rel).resolve() for rel in others.stdout.split("\0") if rel}
    return paths | new, len(new)


def is_risky_name(filename: str) -> str | None:
    lower = filename.lower()
    for pattern in RISKY_NAME_PATTERNS:
        if fnmatch.fnmatch(lower, pattern.lower()):
            return pattern
    return None


def looks_like_text(path: Path, chunk_size: int = 4096) -> bool:
    try:
        with open(path, "rb") as fh:
            return b"\x00" not in fh.read(chunk_size)
    except OSError:
        return False


# ---------------------------------------------------------------------------------------------
# the one matcher, used by BOTH the tree scan and the history scan
# ---------------------------------------------------------------------------------------------

def match_text(text: str, extra_literals: list[str],
               path: str = "") -> list[tuple[int, str, str]]:
    """Return (line_number, rule_id, preview) for every hit in a blob of text.
    Line 0 means the whole file, used by the bulk-media rule which has no single line.
    `extra_literals` is the raw list from .leakpatterns; every spelling of each is tried."""
    variants = [(i, literal_variants(lit)) for i, lit in enumerate(extra_literals, start=1)]
    is_licence = bool(path) and bool(LICENCE_PATH_RX.search(path))
    hits: list[tuple[int, str, str]] = []
    media_names: set[str] = set()
    for lineno, line in enumerate(text.splitlines(), start=1):
        low = line.lower()
        rules = []
        for rule_id, rx in LEAK_REGEXES:
            if rule_id == "email" and is_licence:
                continue
            ignore = RULE_IGNORES.get(rule_id)
            if any(ignore is None or not ignore.search(m.group(0)) for m in rx.finditer(line)):
                rules.append(rule_id)
        for i, spellings in variants:
            if any(v in low for v in spellings):
                rules.append(f"literal-{i}")
        if rules:
            hits.append((lineno, ", ".join(rules), _preview(line)))
        media_names.update(m.group(0) for m in MEDIA_NAME_RX.finditer(line))
    if len(media_names) >= MEDIA_NAME_THRESHOLD:
        hits.append((0, "bulk-media",
                     f"{len(media_names)} distinct audio/playlist filenames "
                     f"(threshold {MEDIA_NAME_THRESHOLD}); names withheld on purpose"))
    return hits


def decode_bytes(raw: bytes) -> str | None:
    """Text of a file, or None when it is not text. Honours the byte-order marks Windows tools
    leave behind: a UTF-16 file decoded as UTF-8 yields nonsense that matches nothing, which
    is a silent miss rather than a visible failure."""
    for bom, enc in ((b"\xff\xfe\x00\x00", "utf-32-le"), (b"\x00\x00\xfe\xff", "utf-32-be"),
                     (b"\xff\xfe", "utf-16-le"), (b"\xfe\xff", "utf-16-be"),
                     (b"\xef\xbb\xbf", "utf-8-sig")):
        if raw.startswith(bom):
            try:
                return raw.decode(enc, errors="ignore")
            except (UnicodeDecodeError, LookupError):
                return None
    if b"\x00" in raw[:4096]:
        return None
    return raw.decode("utf-8", errors="ignore")


def scan_file_contents(path: Path, extra_literals: list[str]) -> tuple[list, bool]:
    """(hits, inspected). `inspected` is False when the file was not read, for any reason.
    Not-read is reported in the summary: it is not the same as clean."""
    if path.suffix.lower() in BINARY_EXTENSIONS:
        return [], False
    try:
        if path.stat().st_size > MAX_BLOB_BYTES:
            return [], False    # a multi-gigabyte file would be a MemoryError, not a finding
    except OSError:
        return [], False
    if path.suffix.lower() not in TEXT_EXTENSIONS and not looks_like_text(path):
        return [], False
    try:
        raw = path.read_bytes()
    except (OSError, MemoryError):
        return [], False
    text = decode_bytes(raw)
    if text is None:
        return [], False
    return match_text(text, extra_literals, path=str(path)), True


def scan_tree(root: Path, extra_literals: list[str], only: set[Path] | None = None):
    name_hits: list[tuple[Path, str]] = []
    content_hits: list[tuple[Path, int, str, str]] = []
    uninspected: list[Path] = []
    in_repo = only is not None
    for dirpath, dirnames, filenames in os.walk(root):
        if not in_repo:
            # outside a repo there is no authority on what would be published, so the noisy
            # directories are pruned. Inside one, git has already said what is in scope, and
            # a force-added file under node_modules is published like any other.
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        else:
            dirnames[:] = [d for d in dirnames if d != ".git"]
        dir_parts = {p.lower() for p in Path(dirpath).relative_to(root).parts}
        under_risky_dir = bool(dir_parts & {d.lower() for d in RISKY_DIR_NAMES})
        for filename in filenames:
            file_path = Path(dirpath) / filename
            if only is not None and file_path.resolve() not in only:
                continue  # gitignored -> won't publish -> skip
            if filename == EXTRA_PATTERNS_FILE:
                # reaching here means the ignore rule is missing, so every private literal is
                # one commit away from being published verbatim
                name_hits.append((file_path, "the private patterns file is in scope for "
                                             "publication" + (" (git does not ignore it)"
                                                              if in_repo else "")))
                continue
            if under_risky_dir:
                name_hits.append((file_path, "under 'extracted/groundtruth'-type directory"))
                continue
            matched = is_risky_name(filename)
            if matched:
                name_hits.append((file_path, f"filename matches '{matched}'"))
                continue
            # the whole relative path, not just the last component: a personal string is just
            # as personal when it is the name of the folder the file sits in
            rel_posix = file_path.relative_to(root).as_posix()
            for lineno, rule_id, _ in match_text(rel_posix, extra_literals):
                if rule_id != "bulk-media":
                    name_hits.append((file_path, f"the FILENAME itself matches [{rule_id}]"))
            hits, inspected = scan_file_contents(file_path, extra_literals)
            if not inspected:
                uninspected.append(file_path)
            for lineno, rule_id, preview in hits:
                content_hits.append((file_path, lineno, rule_id, preview))
    return name_hits, content_hits, uninspected


# ---------------------------------------------------------------------------------------------
# history: every reachable blob, read directly
# ---------------------------------------------------------------------------------------------

def _reachable_blobs(root: Path) -> dict[str, set[str]] | None:
    """{blob sha: {path, ...}} for every object reachable from any ref. Walking objects rather
    than diffs is what makes merge commits, deleted files and renames all fall out for free;
    `git log -S` skips merge diffs by default and would miss anything introduced in one."""
    out = _git(root, ["rev-list", "--objects", "--all"])
    if out is None or out.returncode != 0:
        return None
    found: dict[str, set[str]] = {}
    for line in out.stdout.splitlines():
        sha, _, path = line.partition(" ")
        if path:
            found.setdefault(sha, set()).add(path)
    return found


def _batch_sizes(root: Path, shas: list[str]) -> tuple[dict[str, int], set[str]] | None:
    """({sha: size} for the blobs among these objects, {sha} git said it could not produce),
    without reading any content. Asking first is what keeps a huge blob out of memory and lets
    a 134-byte LFS pointer be read even when its filename says .onnx. Trees and commits are
    neither: they are known, and simply not blobs."""
    if not shas:
        return {}, set()
    try:
        proc = subprocess.run(["git", "-C", str(root), "cat-file", "--batch-check"],
                              input=("\n".join(shas) + "\n").encode("ascii"),
                              capture_output=True, timeout=300)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    out: dict[str, int] = {}
    missing: set[str] = set()
    for line in proc.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[1] == "blob":
            out[parts[0]] = int(parts[2])
        elif len(parts) >= 2 and parts[1] == "missing":
            missing.add(parts[0])
    return out, missing


def _batch_read(root: Path, shas: list[str]) -> dict[str, bytes] | None:
    """One `git cat-file --batch` for the lot; {sha: raw bytes} for the blobs among them.
    None means the read FAILED. An empty dict and a failure must never look alike here: the
    whole point of the history sweep is that a silent nothing reads as "clean"."""
    if not shas:
        return {}
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "cat-file", "--batch"],
            input=("\n".join(shas) + "\n").encode("ascii"),
            capture_output=True, timeout=600)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    data, out, i = proc.stdout, {}, 0
    while i < len(data):
        nl = data.find(b"\n", i)
        if nl < 0:
            break
        header = data[i:nl].decode("utf-8", "replace").split()
        i = nl + 1
        if len(header) < 3:          # "<sha> missing"
            continue
        sha, kind, size = header[0], header[1], int(header[2])
        if kind == "blob":
            out[sha] = data[i:i + size]
        i += size + 1                # payload plus its trailing newline
    return out


def _risky_path_reason(path: str) -> str | None:
    """Why this historical path is suspect by name alone, or None. Paths from git always use
    forward slashes, whatever platform wrote them."""
    base = path.rsplit("/", 1)[-1]
    if base == EXTRA_PATTERNS_FILE:
        return "the private patterns file was COMMITTED at some point"
    matched = is_risky_name(base)
    if matched:
        return f"filename matches '{matched}'"
    if any(part.lower() in RISKY_DIR_NAMES for part in path.split("/")[:-1]):
        return "under 'extracted/groundtruth'-type directory"
    return None


def _all_paths_are_binary(paths: set[str]) -> bool:
    """True when every name this blob was ever stored under carries a binary extension, so
    reading it as text would only produce noise. A blob with no extension anywhere is read."""
    extensions = ["." + p.rsplit(".", 1)[-1].lower()
                  for p in paths if "." in p.rsplit("/", 1)[-1]]
    return bool(extensions) and all(e in BINARY_EXTENSIONS for e in extensions)


def scan_history(root: Path, extra_literals: list[str]):
    """Returns (content_hits, name_hits, message_hits, stats). No matched value is returned
    unless --show-values is on; paths and commit ids are always safe to print."""
    objects = _reachable_blobs(root)
    if objects is None:
        return None, None, None, None

    name_hits: list[tuple[str, str, str]] = []
    candidates: list[str] = []
    wanted: list[str] = []
    skipped_ext = 0

    # `rev-list --objects` names each object ONCE, so a file committed under a personal name
    # and later renamed is only ever seen under its final name. Every path that has ever
    # existed has to be collected separately, or the rename hides the name that mattered.
    paths_seen: set[str] = set()
    hist_paths = _git(root, ["log", "--all", "--format=", "--name-only", "-m", "--no-renames"])
    if hist_paths is not None and hist_paths.returncode == 0:
        paths_seen.update(p.strip() for p in hist_paths.stdout.splitlines() if p.strip())

    for sha, paths in objects.items():
        paths_seen.update(paths)
        for p in sorted(paths):
            reason = _risky_path_reason(p)
            if reason is None:
                # a path NAME can itself be the personal thing: a playlist file named after a
                # private share, a folder named after a person. Same rules, applied to the name.
                rules = [r for _, r, _ in match_text(p, extra_literals) if r != "bulk-media"]
                reason = f"the PATH matches [{', '.join(rules)}]" if rules else None
            if reason:
                name_hits.append((sha, p, reason))
                break
        candidates.append(sha)

    # every path that ever existed, whether or not its blob is still reachable under it
    flagged_paths = {p for _sha, p, _r in name_hits}
    for p in sorted(paths_seen - flagged_paths):
        reason = _risky_path_reason(p)
        if reason is None:
            rules = [r for _, r, _ in match_text(p, extra_literals) if r != "bulk-media"]
            reason = f"the PATH matches [{', '.join(rules)}]" if rules else None
        if reason:
            name_hits.append(("-", p, reason))

    # git exits 0 and answers "<sha> missing" for an object it cannot produce. Dropping those
    # silently is how a partial history scan comes back looking clean.
    probe = _batch_sizes(root, candidates)
    if probe is None:
        return None, None, None, None
    sizes, missing_set = probe
    missing = set(missing_set)
    too_big = 0
    for sha in candidates:
        size = sizes.get(sha)
        if size is None:
            continue
        if size > MAX_BLOB_BYTES:
            too_big += 1
            continue
        # a small blob is read whatever its filename claims: that is how the 134-byte LFS
        # pointer standing in for a .onnx model gets looked at instead of waved through
        if size > 4096 and _all_paths_are_binary(objects[sha]):
            skipped_ext += 1
            continue
        wanted.append(sha)

    blobs = _batch_read(root, wanted)
    if blobs is None:
        return None, None, None, None
    missing |= {s for s in wanted if s not in blobs}

    content_hits: list[tuple[str, str, int, str, str]] = []
    lfs_pointers = 0
    not_text = 0
    for sha, raw in blobs.items():
        if raw.startswith(LFS_POINTER_PREFIX):
            lfs_pointers += 1       # the pointer is scanned; the payload lives outside git
        text = decode_bytes(raw)
        if text is None:
            not_text += 1
            continue
        for path in sorted(objects.get(sha, {"(unknown path)"})):
            for lineno, rule_id, preview in match_text(text, extra_literals, path=path):
                content_hits.append((sha, path, lineno, rule_id, preview))

    message_hits: list[tuple[str, str]] = []
    messages_read = True
    out = _git(root, ["log", "--all", "--format=%H%x1f%B%x1e"])
    if out is None or out.returncode != 0:
        messages_read = False      # a failed read must not look like a clean read
    else:
        for chunk in out.stdout.split("\x1e"):
            commit, _, body = chunk.strip().partition("\x1f")
            if not commit:
                continue
            for _, rule_id, _ in match_text(body, extra_literals):
                message_hits.append((commit[:9], rule_id))
    # annotated tag messages are not commits and git log never prints them
    # for-each-ref does NOT understand git log's %x1f: it prints it literally, which glues a
    # tag's whole message onto its name and then prints that message as if it were the name.
    # It does understand %0a, so a sentinel line separates the records instead.
    tags = _git(root, ["for-each-ref", "refs/tags",
                       f"--format={_TAG_SENTINEL}%(refname:short)%0a%(contents)"])
    if tags is None or tags.returncode != 0:
        messages_read = False
    else:
        for chunk in tags.stdout.split(_TAG_SENTINEL):
            name, _, body = chunk.strip().partition("\n")
            if not name:
                continue
            for _, rule_id, _ in match_text(f"{name}\n{body}", extra_literals):
                message_hits.append((f"tag {safe_path(name)}", rule_id))

    stats = {"objects": len(objects), "read": len(blobs), "skipped_by_extension": skipped_ext,
             "skipped_too_big": too_big, "lfs_pointers": lfs_pointers, "not_text": not_text,
             "messages_read": messages_read, "missing": len(missing),
             "paths_checked": len(paths_seen)}
    return content_hits, name_hits, message_hits, stats


def commits_touching(root: Path, sha: str) -> list[str]:
    """Short ids of the commits that added or removed this exact blob."""
    out = _git(root, ["log", "--all", "--format=%h", f"--find-object={sha}"], timeout=120)
    if out is None or out.returncode != 0:
        return []
    return [c for c in out.stdout.split() if c]


def scan_authors(root: Path):
    """((domain, count), ...), flagged, distinct_name_count. Local parts and names are counted,
    never printed: a personally owned domain already narrows things enough."""
    out = _git(root, ["log", "--all", "--format=%ae%n%ce"])
    names = _git(root, ["log", "--all", "--format=%an%n%cn"])
    if out is None or out.returncode != 0:
        return None, None, 0
    counts: dict[str, int] = {}
    flagged: dict[str, int] = {}
    for line in out.stdout.splitlines():
        addr = line.strip().lower()
        if not addr or "@" not in addr:
            continue
        domain = addr.rsplit("@", 1)[1]
        counts[domain] = counts.get(domain, 0) + 1
        if not NOREPLY_RX.search(addr):
            flagged[domain] = flagged.get(domain, 0) + 1
    n_names = len({n.strip() for n in names.stdout.splitlines() if n.strip()}) if names else 0
    return (sorted(counts.items(), key=lambda kv: -kv[1]),
            sorted(flagged.items(), key=lambda kv: -kv[1]), n_names)


# ---------------------------------------------------------------------------------------------


def main(argv: list[str]) -> int:
    global SHOW_VALUES
    _force_utf8_stdout()
    ap = argparse.ArgumentParser(
        prog="leak_check.py",
        description="Pre-publish personal-data leak scanner for this repo (stdlib only).")
    ap.add_argument("path", nargs="?", default=None,
                    help="directory to scan (default: the repo this script lives in)")
    ap.add_argument("--history", action="store_true",
                    help="also read every reachable blob, path and commit message in history")
    ap.add_argument("--authors", action="store_true",
                    help="also count author/committer email domains across all history")
    ap.add_argument("--all", action="store_true", help="tree + history + authors")
    ap.add_argument("--no-tree", action="store_true", help="skip the working-tree scan")
    ap.add_argument("--show-values", action="store_true",
                    help="print the matching text. LOCAL USE ONLY: the output then carries the "
                         "very strings this tool exists to keep private.")
    ap.add_argument("--patterns", default=None, metavar="FILE",
                    help=f"read the machine-specific literals from FILE instead of "
                         f"<path>/{EXTRA_PATTERNS_FILE} (also: ATTUNE_LEAKPATTERNS). Needed in a "
                         f"worktree or CI checkout, where the ignored file is absent.")
    args = ap.parse_args(argv[1:])

    SHOW_VALUES = args.show_values
    do_history = args.history or args.all
    do_authors = args.authors or args.all
    do_tree = not args.no_tree

    if not (do_tree or do_history or do_authors):
        print("leak_check: ERROR - --no-tree without --history or --authors runs no checks at "
              "all.\n            Refusing to exit 0 on a scan that did not happen.")
        return 2

    target = Path(args.path).resolve() if args.path else Path(__file__).resolve().parent.parent
    if not target.is_dir():
        # masked even here: a typo'd path is exactly the output somebody pastes into an issue
        print(f"leak_check: ERROR - not a directory: {safe_path(str(target))}")
        return 2

    extras, extras_from = load_extra_patterns(target, args.patterns)
    _LOWER_LITERALS[:] = sorted({v for lit in extras for v in literal_variants(lit)},
                                key=len, reverse=True)
    extras_from = safe_path(extras_from)      # only now can it mask the literals themselves
    total = 0
    skipped_total = 0
    incomplete = False

    print(f"leak_check: scanning {safe_path(str(target))}")
    print(f"leak_check: {len(LEAK_REGEXES)} generic rule(s), bulk-media rule at "
          f"{MEDIA_NAME_THRESHOLD} distinct names, "
          + (f"{len(extras)} literal(s) from {extras_from}" if extras
             else f"NO literals ({extras_from}) - generic rules only, this scan is weaker "
                  f"than it looks"))
    if not SHOW_VALUES:
        print("leak_check: values withheld from this report (use --show-values locally)")

    if do_tree:
        publishable, n_new = publishable_files(target)
        scope = (f"{len(publishable)} file(s) git would publish, {n_new} of them not yet "
                 f"committed" if publishable is not None else "all files (not a git repo)")
        print(f"leak_check: tree scope = {scope}; working tree as it stands, not the index")
        print("-" * 70)

        name_hits, content_hits, uninspected = scan_tree(target, extras, only=publishable)

        if name_hits:
            print(f"\n[RISKY FILENAMES] {len(name_hits)} hit(s):")
            for path, reason in name_hits:
                # every name-based finding withholds the name: `*.db` matched a file that may
                # itself be called after a person, and safe_path cannot know that
                print(f"  - {safe_name(str(path.relative_to(target)), True)}  ({reason})")
        if content_hits:
            print(f"\n[TREE CONTENTS] {len(content_hits)} hit(s):")
            for path, lineno, rule_id, preview in content_hits:
                where = f":{lineno}" if lineno else " (whole file)"
                print(f"  - {safe_path(str(path.relative_to(target)))}{where}  [{rule_id}]")
                print(f"      {preview}")
        if uninspected:
            print(f"\n[UNINSPECTED] {len(uninspected)} file(s) in scope were NOT read "
                  f"(binary extension, not text, or unreadable). Not the same as clean:")
            for path in uninspected[:20]:
                print(f"  - {safe_path(str(path.relative_to(target)))}")
            if len(uninspected) > 20:
                print(f"  ... and {len(uninspected) - 20} more")
        skipped_total += len(uninspected)
        if not (name_hits or content_hits):
            print("\n[TREE] clean.")
        total += len(name_hits) + len(content_hits)

    if do_history:
        print("\n" + "-" * 70)
        print("leak_check: history sweep - every reachable blob, path and commit message.")
        print("            Hits here are already public: fixing them means rewriting history,")
        print("            which is the repo owner's call, not this script's.")
        content_hits, name_hits, message_hits, stats = scan_history(target, extras)
        if content_hits is None:
            print("  ERROR - git unavailable, not a repository, or the object read failed;")
            print("          history NOT scanned. Treat as unknown, never as clean.")
            incomplete = True
        else:
            print(f"  read {stats['read']} blob(s) of {stats['objects']} object(s); "
                  f"skipped {stats['skipped_by_extension']} by extension, "
                  f"{stats['skipped_too_big']} over {MAX_BLOB_BYTES // (1024 * 1024)} MiB, "
                  f"{stats['not_text']} not text")
            skipped_total += (stats["skipped_by_extension"] + stats["skipped_too_big"]
                              + stats["not_text"])
            if stats["lfs_pointers"]:
                print(f"  NOTE {stats['lfs_pointers']} Git LFS pointer blob(s) were scanned as "
                      f"text. Their PAYLOADS live outside git and are not inspected here.")
            print(f"  checked {stats['paths_checked']} path name(s) that have ever existed")
            if not stats["messages_read"]:
                print("  ERROR - commit or tag messages could not be read; that part of the")
                print("          history sweep did NOT run.")
                incomplete = True
            if stats["missing"]:
                print(f"  ERROR - git could not produce {stats['missing']} object(s) it had "
                      f"listed;\n          their contents were NOT scanned.")
                incomplete = True
            if name_hits:
                print(f"\n[HISTORY FILENAMES] {len(name_hits)} object(s):")
                for sha, path, reason in sorted(name_hits, key=lambda r: r[1]):
                    where = f"blob {sha[:9]}" if sha != "-" else "path only, blob unreachable"
                    print(f"  - {safe_name(path, True)}  ({where}, {reason})")
            if content_hits:
                by_path: dict[tuple[str, str], set[str]] = {}
                for sha, path, _lineno, rule_id, _preview in content_hits:
                    by_path.setdefault((path, rule_id), set()).add(sha)
                print(f"\n[HISTORY CONTENTS] {len(by_path)} path/rule combination(s):")
                budget = 200   # one full history walk per blob; cap it on a huge repo
                for (path, rule_id), shas in sorted(by_path.items()):
                    walk = sorted(shas)[:max(0, budget)]
                    budget -= len(walk)
                    commits = sorted({c for s in walk for c in commits_touching(target, s)})
                    print(f"  - {safe_path(path)}  [{rule_id}]  {len(shas)} blob version(s)")
                    print(f"      commits: {' '.join(commits[:20])}"
                          + (f" ... +{len(commits) - 20}" if len(commits) > 20 else "")
                          + ("  (commit lookup capped)" if len(walk) < len(shas) else ""))
            if message_hits:
                by_rule: dict[str, set[str]] = {}
                for commit, rule_id in message_hits:
                    by_rule.setdefault(rule_id, set()).add(commit)
                print(f"\n[HISTORY MESSAGES] {len(message_hits)} hit(s) in commit messages:")
                for rule_id, commits in sorted(by_rule.items()):
                    print(f"  - [{rule_id}] {' '.join(sorted(commits)[:20])}")
            if not (name_hits or content_hits or message_hits):
                print("\n[HISTORY] clean.")
            total += len(name_hits) + len(content_hits) + len(message_hits)

    if do_authors:
        print("\n" + "-" * 70)
        print("leak_check: author/committer addresses across all history (domains only).")
        domains, flagged, n_names = scan_authors(target)
        if domains is None:
            print("  ERROR - git unavailable; authors NOT checked.")
            incomplete = True
        elif not domains:
            print("  (no commits)")
        else:
            for domain, n in domains:
                print(f"  - {domain}: {n} author/committer stamp(s)")
            print(f"  - {n_names} distinct author/committer name(s), also published")
            if flagged:
                print(f"\n[AUTHORS] {len(flagged)} non-noreply domain(s). Every commit in a "
                      f"public repo\n          publishes the full address, and the name, "
                      f"forever. A domain you own\n          identifies you on its own. "
                      f"GitHub's per-user noreply address is the\n          private "
                      f"alternative:")
                print("            git config user.email <id>+<user>@users.noreply.github.com")
                total += len(flagged)
            else:
                print("\n[AUTHORS] clean - every address is a noreply address.")

    print("-" * 70)
    if incomplete and not total:
        print("leak_check: INCOMPLETE - a check could not run. Treat as unknown, not as clean.")
        return 3
    if total:
        print(f"leak_check: FAIL - {total} potential leak(s). Do NOT publish until resolved."
              + ("  (and one or more checks could not run)" if incomplete else ""))
        return 1
    print("leak_check: OK - nothing found by the checks that ran." + (
        f" {skipped_total} file(s) were not inspected (see above); that is not the same as "
        f"clean, and no exit code can tell you what is inside them."
        if skipped_total else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
