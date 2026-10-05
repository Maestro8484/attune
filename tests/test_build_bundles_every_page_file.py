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


def test_every_web_module_is_bundled():
    """Every module under web/ is loaded by path at runtime (app.py and the modules it
    pulls in import by file, never by package), so one left off GUI_DATA is not a build
    error: the built app opens, says "Couldn't start the engine: No such file or
    directory ... tagfile.py", and stops. That happened on 2026-10-05 with the tag
    editor's file layer, three days after it was added (ISSUES.md row 98). There is no
    dev-only module under web/, so the rule is simple: all of them, by name."""
    have = _gui_data()
    web = sorted("web/" + f for f in os.listdir(os.path.join(ROOT, "web")) if f.endswith(".py"))
    assert web, "found no modules under web/"
    missing = [f for f in web if f not in have]
    assert not missing, f"under web/ but not in desktop/build.py GUI_DATA: {missing}"
