"""Undo and redo (web/static/history.js, 2026-10-01) run under node, without a browser.

What is held here is the part every undoable action depends on: a step undoes and redoes
in order, a new step after an undo throws the redo away, a step that changed nothing is
not recorded, a step whose undo fails stays where it was so it can be tried again, and an
undo pressed while a step is still running waits for it instead of undoing the one before.

The window has no JavaScript test runner, so this drives node directly. The Windows CI
runner has node installed; a machine without it skips locally, but CI fails instead of
skipping, because a silent skip there would be a green run that proved nothing.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
HISTORY_JS = os.path.join(os.path.dirname(HERE), "web", "static", "history.js")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const toasts = [];
const ctx = { toast: (m, err) => toasts.push((err ? 'ERR ' : '') + m), console, Promise, JSON, setTimeout };
vm.createContext(ctx);
vm.runInContext(fs.readFileSync(process.argv[1], 'utf8') + '\nthis.History = History;', ctx);
const H = ctx.History;
const out = {};
(async () => {
  // a plain value changed by snap steps
  let v = 0;
  const snap = () => ({ v });
  const restore = s => { v = s.v; return true; };
  await H.snapStep('one', snap, restore, () => { v = 1; });
  await H.snapStep('two', snap, restore, async () => { await new Promise(r => setTimeout(r, 5)); v = 2; });
  await H.snapStep('nothing', snap, restore, () => {});          // no change: not recorded
  out.afterTwo = v; out.undoLabel = H.undoLabel;
  await H.undo(); out.afterUndo1 = v;
  await H.undo(); out.afterUndo2 = v;
  out.canUndoAtStart = H.canUndo;
  await H.redo(); out.afterRedo = v; out.redoLabel = H.redoLabel;
  await H.snapStep('three', snap, restore, () => { v = 3; });     // throws the redo away
  out.canRedoAfterNew = H.canRedo;

  // an undo pressed while a step is still running waits for that step
  const slow = H.snapStep('slow', snap, restore, async () => { await new Promise(r => setTimeout(r, 30)); v = 9; });
  const u = H.undo();
  await Promise.all([slow, u]);
  out.afterSlowUndo = v;            // the slow step undone, back to 3

  // a failed undo keeps the step on the stack
  let w = 'a';
  await H.act('flaky', async () => { w = 'b'; return { undo: async () => false, redo: async () => true }; });
  await H.undo();
  out.flakyStillUndoable = H.undoLabel === 'flaky';
  out.toasts = toasts;
  console.log(JSON.stringify(out));
})();
"""


def test_undo_redo_order_and_rules():
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH on the CI runner; the undo test cannot run")
        pytest.skip("node not installed on this machine")
    r = subprocess.run([node, "-e", HARNESS, HISTORY_JS], capture_output=True, text=True,
                       timeout=60)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["afterTwo"] == 2
    assert out["undoLabel"] == "two", "a step that changed nothing must not be recorded"
    assert out["afterUndo1"] == 1
    assert out["afterUndo2"] == 0
    assert out["canUndoAtStart"] is False
    assert out["afterRedo"] == 1 and out["redoLabel"] == "two"
    assert out["canRedoAfterNew"] is False, "a new step must throw the redo away"
    assert out["afterSlowUndo"] == 3, "undo must wait for the step still running"
    assert out["flakyStillUndoable"], "a failed undo must leave the step where it was"
    assert any(t.startswith("ERR Could not undo: flaky") for t in out["toasts"])
