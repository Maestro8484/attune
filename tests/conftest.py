"""Shared pytest setup for the Attune regression suite.

Adds attune/src to sys.path so tests can `import hybrid`, `import mixer`,
`import db`, `import features` etc. against the REAL modules, without
touching any existing source file.
"""
from __future__ import annotations
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ATTUNE_ROOT = os.path.dirname(HERE)                       # .../attune
ATTUNE_SRC = os.path.join(ATTUNE_ROOT, "src")              # .../attune/src
BRIDGE_DIR = os.path.join(ATTUNE_ROOT, "bridge")           # .../attune/bridge

if ATTUNE_SRC not in sys.path:
    sys.path.insert(0, ATTUNE_SRC)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """Say out loud how many checks were switched off, and why.

    A suite that skips a third of itself still prints "passed" and exits 0, so
    a green line reads as a full pass when it is not one. On 2026-09-21 a
    release branch had been running 97 of its 137 checks for weeks and nothing
    said so. The count is the only thing that says so.
    """
    skipped = terminalreporter.stats.get("skipped", [])
    if not skipped:
        return

    reasons = {}
    for report in skipped:
        longrepr = getattr(report, "longrepr", None)
        if isinstance(longrepr, tuple) and len(longrepr) == 3:
            reason = str(longrepr[2])
        else:
            reason = str(longrepr)
        reason = reason.replace("Skipped: ", "").strip().replace("\n", " ")
        if len(reason) > 150:
            reason = reason[:147] + "..."
        reasons[reason] = reasons.get(reason, 0) + 1

    ran = len(terminalreporter.stats.get("passed", []))
    ran += len(terminalreporter.stats.get("failed", []))
    total = ran + len(skipped)
    terminalreporter.write_sep(
        "-", f"{len(skipped)} of {total} checks were switched off, not run")
    for reason, count in sorted(reasons.items(), key=lambda kv: (-kv[1], kv[0])):
        terminalreporter.write_line(f"  {count:>4}  {reason}")
