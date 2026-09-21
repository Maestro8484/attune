#!/usr/bin/env python3
"""check_links.py - every relative link and image in the repo's Markdown actually resolves.

    python tools/check_links.py            # the repo this script lives in
    python tools/check_links.py <path>     # somewhere else
    python tools/check_links.py --http     # also check that http(s) links answer

Written because "every relative link resolves" is a claim, and a claim checked by eye on a
dozen files is a claim nobody checked. Exits 1 if anything is broken, so it can gate a
release.

What it checks:
  - [text](path)      and [text](path#anchor)   -> the file exists
  - ![alt](path)                                -> the image file exists
  - <a href="path">   and <img src="path">      -> same, for the HTML blocks a README uses
  - (#anchor) on its own                        -> a heading in the SAME file matches it

What it deliberately does not check:
  - http(s) links, unless --http is passed. A network failure is not a broken document, and
    a release check that needs the internet is a release check that fails on a train.
  - mailto:, and any other scheme.
  - Anchors into ANOTHER file. GitHub's heading-slug rules are its own; guessing them would
    produce false alarms, which are worse than no check.

Skipped directories: .git, licenses (third-party text, not ours), dist, build, node_modules,
.venv and anything beginning with a dot.

Stdlib only.
"""
from __future__ import annotations
import os
import re
import sys
import argparse

SKIP_DIRS = {".git", "licenses", "dist", "build", "node_modules", "__pycache__"}

# [text](target), including ![alt](target). Targets with spaces must be <bracketed> or
# quoted in Markdown, so stopping at whitespace or ')' is right.
MD_LINK = re.compile(r"!?\[[^\]]*\]\(\s*([^)\s]+)")
HTML_HREF = re.compile(r"""<(?:a|img)\b[^>]*?\b(?:href|src)\s*=\s*["']([^"']+)["']""",
                       re.IGNORECASE)
# ATX headings only. Setext headings are not used anywhere in this repo.
HEADING = re.compile(r"^#{1,6}\s+(.*?)\s*#*\s*$", re.MULTILINE)
# Inside an HTML comment. A commented-out screenshot slot is not a broken link.
COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
FENCE = re.compile(r"^```.*?^```", re.DOTALL | re.MULTILINE)


def slug(text):
    """GitHub's heading slug, near enough for same-file anchors.

    Lowercase, strip anything that is not a word character, a space or a hyphen, then
    spaces to hyphens. Inline markdown (`code`, **bold**, links) is stripped first because
    GitHub slugs the rendered text, not the source.
    """
    text = re.sub(r"!?\[([^\]]*)\]\([^)]*\)", r"\1", text)   # links -> their text
    text = re.sub(r"[`*_~]", "", text)
    text = text.strip().lower()
    text = re.sub(r"[^\w\s-]", "", text, flags=re.UNICODE)
    return re.sub(r"\s+", "-", text)


def markdown_files(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in SKIP_DIRS and not d.startswith(".")]
        for name in sorted(filenames):
            if name.lower().endswith(".md"):
                yield os.path.join(dirpath, name)


def targets_in(text):
    """Every link target, with the 1-based line it sits on."""
    stripped = FENCE.sub(lambda m: "\n" * m.group(0).count("\n"), text)
    stripped = COMMENT.sub(lambda m: "\n" * m.group(0).count("\n"), stripped)
    for pattern in (MD_LINK, HTML_HREF):
        for m in pattern.finditer(stripped):
            line = stripped.count("\n", 0, m.start()) + 1
            yield m.group(1).strip(), line


def check_file(path, root, anchors_cache, do_http):
    text = open(path, encoding="utf-8", errors="replace").read()
    anchors = {slug(h) for h in HEADING.findall(COMMENT.sub("", FENCE.sub("", text)))}
    here = os.path.dirname(path)
    problems = []

    for target, line in targets_in(text):
        if target.startswith(("mailto:", "tel:", "data:")):
            continue
        if target.startswith(("http://", "https://", "//")):
            if do_http:
                problems.extend(_check_http(target, path, line))
            continue
        if target.startswith("#"):
            want = target[1:].lower()
            if want and want not in anchors:
                problems.append((path, line, target, "no heading in this file matches it"))
            continue

        rel, _, anchor = target.partition("#")
        rel = rel.split("?", 1)[0]
        if not rel:
            continue
        dest = os.path.normpath(os.path.join(here, rel.replace("/", os.sep)))
        if not os.path.exists(dest):
            problems.append((path, line, target, "no such file: " + os.path.relpath(dest, root)))
            continue
        if anchor and dest.lower().endswith(".md"):
            key = os.path.abspath(dest)
            if key not in anchors_cache:
                other = open(dest, encoding="utf-8", errors="replace").read()
                anchors_cache[key] = {
                    slug(h) for h in HEADING.findall(COMMENT.sub("", FENCE.sub("", other)))}
            if anchor.lower() not in anchors_cache[key]:
                problems.append((path, line, target,
                                 "file exists, but no heading in it matches the anchor"))
    return problems


def _check_http(url, path, line):
    import urllib.request
    import urllib.error
    req = urllib.request.Request(url, method="HEAD",
                                 headers={"User-Agent": "attune-check-links/1"})
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            if r.status >= 400:
                return [(path, line, url, "HTTP %s" % r.status)]
    except urllib.error.HTTPError as e:
        if e.code in (403, 405, 501):        # HEAD refused; not evidence of a dead page
            return []
        return [(path, line, url, "HTTP %s" % e.code)]
    except Exception as e:                                          # noqa: BLE001
        return [(path, line, url, "%s: %s" % (type(e).__name__, e))]
    return []


def main(argv=None):
    ap = argparse.ArgumentParser(prog="check_links.py",
                                 description="Check relative links in this repo's Markdown.")
    ap.add_argument("path", nargs="?",
                    default=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    help="directory to scan (default: the repo this script lives in)")
    ap.add_argument("--http", action="store_true",
                    help="also check http(s) links. Needs the network and is slow.")
    args = ap.parse_args(argv)

    root = os.path.abspath(args.path)
    files = list(markdown_files(root))
    anchors_cache = {}
    problems = []
    for path in files:
        problems.extend(check_file(path, root, anchors_cache, args.http))

    print("check_links: %d markdown files under %s" % (len(files), root))
    if not problems:
        print("check_links: OK, every link resolves")
        return 0
    for path, line, target, why in problems:
        print("  %s:%d  %s  ->  %s" % (os.path.relpath(path, root), line, target, why))
    print("check_links: %d BROKEN" % len(problems))
    return 1


if __name__ == "__main__":
    sys.exit(main())
