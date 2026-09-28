"""No program the app starts may open a black console window on Windows.

Attune.exe is a windowed program with no console of its own. Any console program it
starts (ffmpeg, ffprobe, a Python analyzer) gets a fresh console window unless the call
passes CREATE_NO_WINDOW. Found 2026-09-28: every call did, except the ffprobe fallback in
src/scan.py. This test reads the source rather than keeping a list, so a call added
tomorrow is checked the day it is added.

Build tools under desktop/ run from a console on purpose and are left out. explorer is a
windowed program, so the "show in folder" call needs no flag.
"""
from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCANNED = ("src", "web", "desktop", "bridge")
BUILD_TOOLS = {"build.py", "build_installer.py", "package.py"}
LAUNCHERS = {"run", "Popen", "call", "check_call", "check_output"}
WINDOWED_PROGRAMS = {"explorer"}


def _first_program(call):
    if call.args and isinstance(call.args[0], (ast.List, ast.Tuple)) and call.args[0].elts:
        head = call.args[0].elts[0]
        if isinstance(head, ast.Constant) and isinstance(head.value, str):
            return head.value.lower()
    return None


def _offenders():
    out = []
    for folder in SCANNED:
        for path in sorted((ROOT / folder).rglob("*.py")):
            if path.name in BUILD_TOOLS or "__pycache__" in path.parts:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
            for node in ast.walk(tree):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if not (isinstance(node.func.value, ast.Name)
                        and node.func.value.id == "subprocess"
                        and node.func.attr in LAUNCHERS):
                    continue
                if any(k.arg == "creationflags" for k in node.keywords):
                    continue
                if _first_program(node) in WINDOWED_PROGRAMS:
                    continue
                out.append(f"{path.relative_to(ROOT)}:{node.lineno}")
    return out


def test_every_launched_program_is_windowless():
    assert _offenders() == [], "starts a program without CREATE_NO_WINDOW"
