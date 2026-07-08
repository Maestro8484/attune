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
