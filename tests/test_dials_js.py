"""The mix dials and fit lines start from the engine's numbers (web/static/studio.js, 2026-10-02).

The window sends its five dials and its two fit lines with every mix, and the server applies
what it is sent. Until 2026-10-02 they started at the V2 numbers written into the page, so a
library under the sound profile was mixed with V2's numbers in the window. Held here, by
running the window's own functions under node:

  1. a V2 library: nothing is written, so every request is what it always was;
  2. a sound-profile library: the dials and lines are the engine's, the name says so, the V2
     presets give way to the sound profile's, and the Timbre dial (which the engine ignores
     under that profile) is switched off;
  3. a line typed under one recipe is dropped when the library comes up under the other, even
     when that happened while the window was closed, and kept when it is the same recipe;
  4. a library brought across while a recipe is chosen keeps the recipe's own numbers and
     takes the rest from the new engine.

The functions are lifted out of studio.js by name and run against stub elements, so a change
to their bodies is what is tested, not a copy. Same node rule as test_history_js.py: a
machine without node skips, CI fails instead.
"""
from __future__ import annotations
import json
import os
import shutil
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
STUDIO_JS = os.path.join(os.path.dirname(HERE), "web", "static", "studio.js")

HARNESS = r"""
const fs = require('fs'), vm = require('vm');
const src = fs.readFileSync(process.argv[1], 'utf8');
function grab(name) {
  const at = src.search(new RegExp('\\nfunction ' + name + '[(]'));
  if (at < 0) throw new Error('studio.js has no function ' + name);
  let j = src.indexOf('{', src.indexOf(')', at)), depth = 0;
  for (; j < src.length; j++) {
    if (src[j] === '{') depth++;
    else if (src[j] === '}' && --depth === 0) break;
  }
  return src.slice(at, j + 1);
}
const names = ['captureEngineDefaults', 'applyEngineDefaults', 'paintEngineName', 'findRecipe',
               'applyRecipe', 'selectRecipe', 'paintRecipeActions', 'applyFitDefaults', 'fitParams'];
const code = names.map(grab).join('\n');
function el(v) {
  return { value: v, _attr: v, hidden: false, checked: false, disabled: false, title: '',
           textContent: '', dataset: {}, getAttribute() { return this._attr; },
           dispatchEvent() {}, closest() { return { hidden: false }; } };
}
function boot(stats, ls, recipes) {
  const els = { clap: el('1'), lib: el('0.4'), genre: el('0.3'), bpm: el('0.3'), era: el('0.1'),
    fitLine: el('0.70'), clapLine: el('0.990'), fitMax: el(''), mmr: el(''), flow: el(''),
    recipeSel: el(''), recipeActions: el(''), mixSize: el('100'), dedupOn: el(''),
    dedupField: el(''), engineName: el('') };
  els.fitMax.checked = true;
  const chips = ['sound', 'v2', 'nobpm'].map(p => { const b = el(''); b.dataset.preset = p; b.hidden = p === 'sound'; return b; });
  const writes = [];
  for (const k of ['clap', 'lib', 'genre', 'bpm', 'era']) {
    const e = els[k]; let v = e.value;
    Object.defineProperty(e, 'value', { get: () => v, set: x => { writes.push(k); v = String(x); } });
  }
  const store = { get(k, d) { return ('attune.' + k) in ls ? JSON.parse(ls['attune.' + k]) : d; },
                  set(k, v) { ls['attune.' + k] = JSON.stringify(v); } };
  const ctx = { $: id => els[id], S: { stats }, store, RECIPES: recipes || [], curRecipe: '',
    ENGINE_DEFAULTS: null, curCollection: '', Event: function () {},
    localStorage: { removeItem(k) { delete ls[k]; } },
    document: { querySelectorAll: () => chips }, String, Number, Math, parseFloat, Object };
  vm.createContext(ctx);
  vm.runInContext(code + '\nthis.api = {captureEngineDefaults, applyEngineDefaults, paintEngineName, selectRecipe, applyFitDefaults, fitParams};', ctx);
  const a = ctx.api;
  a.paintEngineName(stats); a.captureEngineDefaults(); a.applyEngineDefaults(); a.applyFitDefaults(false);
  return { ctx, els, chips, ls, writes };
}
function state(b) {
  const e = b.els;
  return { dials: ['clap', 'lib', 'genre', 'bpm', 'era'].map(k => +e[k].value),
           fit: b.ctx.api.fitParams(false), clap: b.ctx.api.fitParams(true),
           name: e.engineName.textContent, libOff: e.lib.disabled,
           shown: b.chips.filter(c => !c.hidden).map(c => c.dataset.preset), writes: b.writes.slice() };
}
const v2 = { engine: 'v2', profile: 'v2', weights: { clap: 1.0, lib: 0.4, genre: 0.3, bpm: 0.3, era: 0.1 }, fit_line: 0.7, clap_line: 0.99 };
const snd = { engine: 'v2', profile: 'sound', weights: { clap: 1.0, lib: 0.0, genre: 0.2, bpm: 0.0, era: 1.5 }, fit_line: 0.54, clap_line: 0.79 };
const out = {};
out.v2 = state(boot(v2, {}));
out.sound = state(boot(snd, {}));
out.typedForV2OnSound = state(boot(snd, { 'attune.fitLine': '"0.75"', 'attune.clapLine': '"0.995"' }));
out.typedForSoundOnSound = state(boot(snd, { 'attune.fitLine': '"0.60"', 'attune.clapLine': '"0.800"', 'attune.fitLineProfile': '"sound"' }));
out.typedForV2OnV2 = state(boot(v2, { 'attune.fitLine': '"0.75"' }));
const b = boot(v2, {}, [{ name: 'Same-Era Deep Cuts', params: { era: 0.2, variety: 1 } }]);
b.ctx.api.selectRecipe('Same-Era Deep Cuts', { persist: false });
b.ctx.S.stats = snd;                       // what pollReload does when the recipe changed
b.ctx.api.paintEngineName(snd); b.ctx.api.captureEngineDefaults();
b.ctx.api.selectRecipe('Same-Era Deep Cuts', { persist: false }); b.ctx.api.applyFitDefaults(true);
out.recipeAcross = state(b);
console.log(JSON.stringify(out));
"""


def _run():
    node = shutil.which("node")
    if node is None:
        if os.environ.get("CI"):
            pytest.fail("node is not on PATH on the CI runner; the dials test cannot run")
        pytest.skip("node not installed on this machine")
    r = subprocess.run([node, "-e", HARNESS, STUDIO_JS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout.strip().splitlines()[-1])


def test_the_dials_and_lines_start_from_the_engine():
    out = _run()
    v2 = out["v2"]
    assert v2["writes"] == [], "a V2 library must not have a single dial written"
    assert v2["dials"] == [1, 0.4, 0.3, 0.3, 0.1]
    assert v2["fit"]["min_fit"] == "0.7" and v2["clap"]["min_fit"] == "0.99"
    assert v2["name"] == "Attune V2" and v2["shown"] == ["v2", "nobpm"] and not v2["libOff"]

    s = out["sound"]
    assert s["dials"] == [1, 0, 0.2, 0, 1.5]
    assert s["fit"]["min_fit"] == "0.54" and s["clap"]["min_fit"] == "0.79"
    assert s["name"] == "Attune sound profile" and s["shown"] == ["sound"]
    assert s["libOff"], "the Timbre dial does nothing under the sound profile and must say so"


def test_a_typed_line_belongs_to_the_recipe_it_was_typed_under():
    out = _run()
    assert out["typedForV2OnSound"]["fit"]["min_fit"] == "0.54"
    assert out["typedForV2OnSound"]["clap"]["min_fit"] == "0.79"
    assert out["typedForSoundOnSound"]["fit"]["min_fit"] == "0.6"
    assert out["typedForSoundOnSound"]["clap"]["min_fit"] == "0.8"
    assert out["typedForV2OnV2"]["fit"]["min_fit"] == "0.75"


def test_a_recipe_keeps_its_own_numbers_across_a_crossing():
    r = _run()["recipeAcross"]
    assert r["dials"] == [1, 0, 0.2, 0, 0.2], "the recipe's era, the sound profile's other dials"
    assert r["fit"]["min_fit"] == "0.54"
    assert r["name"] == "Attune sound profile"
