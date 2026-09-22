"""Every script and stylesheet the Studio page loads must be in the desktop build's file
list. desktop/build.py names them one by one, and a file left off it is not a build
error: the built app opens with that file answering 404. It has happened once already
(a dead window, 2026-07-22), and a new page script was added on 2026-09-22 (tips.js),
which is exactly when a hand-kept list goes out of date.
"""
from __future__ import annotations
import ast
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _gui_data():
    src = open(os.path.join(ROOT, "desktop", "build.py"), encoding="utf-8").read()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.Assign) and any(getattr(t, "id", "") == "GUI_DATA"
                                                for t in node.targets):
            return {a.value for a, _ in (e.elts for e in node.value.elts)}
    raise AssertionError("GUI_DATA not found in desktop/build.py")


def test_every_file_the_page_loads_is_bundled():
    html = open(os.path.join(ROOT, "web", "static", "studio.html"), encoding="utf-8").read()
    refs = re.findall(r'<(?:script|link)[^>]+(?:src|href)="(/[^"]+\.(?:js|css))"', html)
    assert refs, "found no scripts in studio.html"
    wanted = {"web/static/" + r.rsplit("/", 1)[-1] for r in refs}
    missing = sorted(wanted - _gui_data())
    assert not missing, f"loaded by studio.html but not in desktop/build.py GUI_DATA: {missing}"
