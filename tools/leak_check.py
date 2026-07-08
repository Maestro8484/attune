#!/usr/bin/env python3
"""leak_check.py — pre-publish personal-data leak scanner.

Attune (this repo) may be exported/derived from a private working tree that also contains a
real, personally-identifying music library and live databases (see ../../SYNC.md). This script
helps make sure none of that reaches a public GitHub repo. It walks a directory tree (default:
this repo) and looks for two categories of leak:

  (a) RISKY FILENAMES — files that are, by name/location alone, almost certainly personal data:
        - any `*.db` file (sqlite databases of a real library)
        - `library.json` (a captured catalog)
        - anything matching `*.m3lib*` (MusicIP's native library file / snapshots)
        - any file located under a directory literally named `extracted` or `groundtruth`

  (b) LEAKED PATHS / PII in file *contents* — via two mechanisms:
        - GENERIC built-in regexes with NO personal data baked in: any email address, and any
          Windows per-user home path (`C:\\Users\\<name>`). These are safe to publish.
        - OPTIONAL machine-specific literals loaded from a `.leakpatterns` file at the scan
          root (one substring per line, `#` comments allowed). Put your NAS name, mapped
          drive letters, exact email, etc. there. **Keep `.leakpatterns` gitignored** so those
          literals are checked locally but never published themselves.

Prints a plain-text report and exits non-zero if ANY hit was found, zero if clean. Stdlib only:

    python leak_check.py [path]

Exit codes:  0 clean · 1 hit(s) found · 2 path missing/not a directory
"""

from __future__ import annotations

import fnmatch
import os
import re
import subprocess
import sys
from pathlib import Path

# --- (a) risky filename patterns -------------------------------------------------------------
RISKY_NAME_PATTERNS = ["*.db", "library.json", "*.m3lib*", "native.db"]
RISKY_DIR_NAMES = {"extracted", "groundtruth"}

# --- (b) generic PII regexes (contain NO personal data — safe to publish) --------------------
LEAK_REGEXES = [
    ("email address", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    ("windows user home", re.compile(r"[Cc]:\\Users\\[^\\/\r\n\"'<> ]+")),
]
# Machine-specific literal substrings are loaded at runtime from a gitignored `.leakpatterns`
# at the scan root, so this published file stays free of anyone's personal strings.
EXTRA_PATTERNS_FILE = ".leakpatterns"

SKIP_DIR_NAMES = {".git", "__pycache__", ".venv", "venv", ".mypy_cache",
                  ".pytest_cache", ".ruff_cache", "node_modules"}
# never content-scan the extras file itself (it legitimately contains the literals)
SKIP_FILE_NAMES = {EXTRA_PATTERNS_FILE}

TEXT_EXTENSIONS = {
    ".py", ".md", ".txt", ".json", ".cfg", ".ini", ".toml", ".yml", ".yaml",
    ".sh", ".ps1", ".bat", ".html", ".htm", ".css", ".js", ".ts", ".csv",
    ".gitignore", ".gitattributes", ".rst",
}


def load_extra_patterns(root: Path) -> list[str]:
    """Optional machine-specific literal substrings from <root>/.leakpatterns (gitignored)."""
    f = root / EXTRA_PATTERNS_FILE
    out: list[str] = []
    if f.is_file():
        try:
            for line in f.read_text(encoding="utf-8", errors="ignore").splitlines():
                s = line.strip()
                if s and not s.startswith("#"):
                    out.append(s)
        except OSError:
            pass
    return out


def git_tracked_files(root: Path) -> set[Path] | None:
    """Absolute paths of files git actually tracks under `root`, or None if not a git repo.
    A pre-publish scanner should only look at what WOULD be published — gitignored local
    data (databases, validation dumps) never reaches the remote, so scanning it cries wolf."""
    try:
        out = subprocess.run(["git", "-C", str(root), "ls-files", "-z"],
                             capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return {(root / rel).resolve() for rel in out.stdout.split("\0") if rel}


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


def scan_file_contents(path: Path, extra_literals: list[str]) -> list[tuple[int, str, str]]:
    """Return (line_number, matched_pattern_label, line_preview) for every hit."""
    hits: list[tuple[int, str, str]] = []
    if path.suffix.lower() not in TEXT_EXTENSIONS and not looks_like_text(path):
        return hits
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as fh:
            for lineno, line in enumerate(fh, start=1):
                labels = []
                for label, rx in LEAK_REGEXES:
                    if rx.search(line):
                        labels.append(label)
                for lit in extra_literals:
                    if lit in line:
                        labels.append(f"literal:{lit}")
                if labels:
                    preview = line.strip()
                    if len(preview) > 160:
                        preview = preview[:157] + "..."
                    hits.append((lineno, ", ".join(labels), preview))
    except OSError:
        pass
    return hits


def scan_tree(root: Path, extra_literals: list[str], only: set[Path] | None = None):
    name_hits: list[tuple[Path, str]] = []
    content_hits: list[tuple[Path, int, str, str]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIR_NAMES]
        dir_parts = {p.lower() for p in Path(dirpath).relative_to(root).parts}
        under_risky_dir = bool(dir_parts & {d.lower() for d in RISKY_DIR_NAMES})
        for filename in filenames:
            file_path = Path(dirpath) / filename
            if only is not None and file_path.resolve() not in only:
                continue  # not git-tracked -> won't publish -> skip
            if under_risky_dir:
                name_hits.append((file_path, "under 'extracted/groundtruth'-type directory"))
                continue
            matched = is_risky_name(filename)
            if matched:
                name_hits.append((file_path, f"filename matches '{matched}'"))
                continue
            if filename in SKIP_FILE_NAMES:
                continue
            for lineno, label, preview in scan_file_contents(file_path, extra_literals):
                content_hits.append((file_path, lineno, label, preview))
    return name_hits, content_hits


def main(argv: list[str]) -> int:
    target = Path(argv[1]).resolve() if len(argv) > 1 else Path(__file__).resolve().parent.parent
    if not target.is_dir():
        print(f"leak_check: ERROR — not a directory: {target}")
        return 2

    extras = load_extra_patterns(target)
    tracked = git_tracked_files(target)
    scope = f"git-tracked files only ({len(tracked)})" if tracked is not None else "all files (not a git repo)"
    print(f"leak_check: scanning {target}")
    print(f"leak_check: scope = {scope}")
    print(f"leak_check: {len(LEAK_REGEXES)} generic regex(es)"
          + (f" + {len(extras)} literal(s) from {EXTRA_PATTERNS_FILE}" if extras else
             f" (no {EXTRA_PATTERNS_FILE} found — generic checks only)"))
    print("-" * 70)

    name_hits, content_hits = scan_tree(target, extras, only=tracked)

    if name_hits:
        print(f"\n[RISKY FILENAMES] {len(name_hits)} hit(s):")
        for path, reason in name_hits:
            print(f"  - {path.relative_to(target)}  ({reason})")
    if content_hits:
        print(f"\n[LEAKED PATH/PII] {len(content_hits)} hit(s):")
        for path, lineno, label, preview in content_hits:
            print(f"  - {path.relative_to(target)}:{lineno}  [{label}]")
            print(f"      {preview}")

    total = len(name_hits) + len(content_hits)
    print("-" * 70)
    if total:
        print(f"leak_check: FAIL — {total} potential leak(s). Do NOT publish until resolved.")
        return 1
    print("leak_check: OK — no risky filenames or leaked path/PII patterns found.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
