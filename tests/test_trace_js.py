"""The trace's "what changed" arithmetic (web/static/trace.js Trace.diff), under node.

Kept, gone and new between two lists of pool indices, and how many of the kept songs
changed place relative to each other. Same node rule as test_history_js.py: a machine
without node skips, CI fails instead.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
TRACE_JS = os.path.join(os.path.dirname(HERE), "web", "static", "trace.js")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const ctx = { console, JSON, Math, Number, Array, Map, Set, Object, URLSearchParams,
  store: { get: (k, d) => d, set() {} }, S: { view: 'mix', trace: null, rows: [] },
  $: () => null, esc: s => String(s), toast() {}, jget: async () => ({}), placeFloating() {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') + '\nthis.Trace = Trace;', ctx);
const d = ctx.Trace.diff;
const out = {
  same: d([1, 2, 3], [1, 2, 3]),
  swap: d([1, 2, 3], [3, 2, 1]),
  grow: d([1, 2], [1, 2, 9, 8]),
  churn: d([5, 6, 7, 8], [8, 6, 10]),
  empty: d(null, [1]),
};
process.stdout.write(JSON.stringify(out));
"""


@pytest.mark.skipif(shutil.which("node") is None and not os.environ.get("CI"),
                    reason="node is not installed here")
def test_diff_counts_kept_gone_new_and_moved(tmp_path):
    assert shutil.which("node"), "node is required on CI"
    h = tmp_path / "h.js"
    h.write_text(HARNESS, encoding="utf-8")
    r = subprocess.run(["node", str(h), TRACE_JS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    assert o["same"] == {"kept": [1, 2, 3], "gone": [], "added": [], "moved": 0}
    assert o["swap"]["kept"] == [1, 2, 3] and o["swap"]["moved"] == 2
    assert o["grow"] == {"kept": [1, 2], "gone": [], "added": [9, 8], "moved": 0}
    assert o["churn"] == {"kept": [6, 8], "gone": [5, 7], "added": [10], "moved": 2}
    assert o["empty"] == {"kept": [], "gone": [], "added": [1], "moved": 0}
