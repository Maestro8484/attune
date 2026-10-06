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


# The connections (trace.js constSvg, 2026-10-06): the drawing over a fresh mix and in Show
# the magic. A synthetic trace: 30 songs looked at, 5 kept, 5 held back for the artist, 10
# cut for the count, 5 filled in past the cut-off, 5 beyond where the walk stopped.
CONST_HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const esc = s => String(s).replace(/&/g, '&amp;').replace(/"/g, '&quot;').replace(/</g, '&lt;');
const ctx = { console, JSON, Math, Number, Array, Map, Set, Object, URLSearchParams,
  store: { get: (k, d) => d, set() {} }, S: { view: 'mix', trace: null, rows: [] },
  $: () => null, esc, toast() {}, jget: async () => ({}), placeFloating() {} };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[2], 'utf8') + '\nthis.Trace = Trace;', ctx);
const kinds = [].concat(Array(5).fill('kept'), Array(5).fill('artist'), Array(10).fill('count'),
                        Array(5).fill('kept-past'), Array(5).fill('next'));
const field = kinds.map((st, k) => ({ i: 100 + k, rank: k + 2, fit: +(0.95 - k * 0.02).toFixed(3), status: st, label: `A${k} - T${k}` }));
const list = field.filter(r => r.status === 'kept' || r.status === 'kept-past').map(r => r.i);
const t = { kind: 'mix', pool: 1000, line: 0.6, seeds: [{ i: 1, label: 'S - Seed' }], field,
  order: [1].concat(list), transitions: list.map((_, k) => 0.9 - k * 0.01),
  funnel: [{ id: 'pool', n: 1000 }, { id: 'reach', n: 25 }], songs: {}, verdict: { like: 'Seed' } };
ctx.S.trace = t;
process.stdout.write(JSON.stringify({ svg: ctx.Trace.constSvg(t, { size: 400 }), field, list,
  mini: ctx.Trace.constSvg(t, { size: 26, mini: true }), none: ctx.Trace.constSvg({ field: [] }) }));
"""


@pytest.mark.skipif(shutil.which("node") is None and not os.environ.get("CI"),
                    reason="node is not installed here")
def test_connections_place_closer_songs_nearer_and_thread_the_list(tmp_path):
    import math
    import re
    assert shutil.which("node"), "node is required on CI"
    h = tmp_path / "c.js"
    h.write_text(CONST_HARNESS, encoding="utf-8")
    r = subprocess.run(["node", str(h), TRACE_JS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    o = json.loads(r.stdout)
    svg, field, order = o["svg"], {f["i"]: f for f in o["field"]}, o["list"]
    dots = {int(m.group(4)): (m.group(1), float(m.group(2)), float(m.group(3)))
            for m in re.finditer(r'<circle class="nd ([^"]*)" cx="([-\d.]+)" cy="([-\d.]+)" r="[\d.]+" data-i="(\d+)"', svg)}
    assert set(dots) == set(field), "every song looked at is one dot"
    assert len(re.findall(r'<line class="lk ', svg)) == 25, "a line from the seed to every song looked at, none to the five beyond"
    assert len(re.findall(r'<path class="th"', svg)) == len(order), "one thread segment per hand-off, seed included"
    # nearer the middle sounds more like the seed
    by_fit = sorted(field.values(), key=lambda f: -f["fit"])
    dist = [math.hypot(dots[f["i"]][1], dots[f["i"]][2]) for f in by_fit]
    assert all(a <= b + 0.15 for a, b in zip(dist, dist[1:]))
    # the list goes round clockwise from the top, in play order
    for k, i in enumerate(order):
        a = math.atan2(dots[i][2], dots[i][1])
        want = -math.pi / 2 + 2 * math.pi * k / len(order)
        assert abs(math.remainder(a - want, 2 * math.pi)) < 0.02, (k, i)
    # "sound enough like it" lights the songs taken at or over the cut-off, as web/app.py counts them
    lit = {i for i, d in dots.items() if "s-fit" in d[0].split()}
    taken = {"kept", "kept-past", "carried", "count", "duplicate", "near-twin", "removed", "blocked"}
    assert lit == {i for i, f in field.items() if f["status"] in taken and f["fit"] >= 0.6}
    assert "the cut-off 60%" in svg and "1,000 scored" in svg
    assert 'class="cst mini"' in o["mini"] and "<text" not in o["mini"], "the small one beside the headline has no words"
    assert o["none"] == "", "no drawing from a trace with too few songs"
