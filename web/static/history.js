/* Undo and redo for the changes a listener makes while building and refining a mix.
   Asked for on 2026-10-01, in his words: undo "the action of selecting more like this
   artist/song or block this artist and any other actions through the right-click menu".

   The standard answer is the command pattern behind every editor's Edit menu: each step
   leaves behind its own way back and its own way forward, on two stacks. Libraries such
   as undo-manager do exactly this; one is not used because the window is plain scripts
   with no bundler and the whole mechanism is the sixty lines below.

   How a step is recorded is up to the caller:
     - a mix step stores the mix as it was before and after (seed songs, list, votes,
       removals), so undo puts back the exact list that was on screen. It never asks the
       engine to pick again, which could give a different list.
     - a rating, love or tag step stores the old and new value and writes the old one back.
     - a queue step stores the queue before and after.
   Steps run one at a time, in the order they were asked for, so an undo pressed while a
   re-mix is still on its way waits for it instead of undoing the step before it.
   The history lasts as long as the window is open; it is not saved across restarts.
   Nothing here touches the database except through the same calls the step itself used. */
'use strict';

const History = (() => {
  const CAP = 100;
  const done = [], undone = [];
  let chain = Promise.resolve();
  const say = (m, err) => { if (typeof toast === 'function') toast(m, err); };

  // run jobs strictly one after another; a failed job never jams the ones behind it
  function run(job) {
    const p = chain.then(job, job);
    chain = p.catch(() => {});
    return p;
  }

  function push(step) {
    done.push(step);
    if (done.length > CAP) done.shift();
    undone.length = 0;
    paint();
  }

  /* act(label, fn): fn does the change and returns {undo, redo} (each an async function
     returning false when it could not be applied), or nothing when there is nothing to
     record. label may be a function, read after fn ran. */
  function act(label, fn) {
    return run(async () => {
      const rec = await fn();
      if (rec && rec.undo && rec.redo) {
        push({ label: typeof label === 'function' ? label() : label, undo: rec.undo, redo: rec.redo });
      }
      return rec;
    });
  }

  /* snapStep(label, snap, restore, fn): record the state before and after fn, when they
     differ. snap() returns a plain copy; restore(copy) puts it back and returns false
     when it could not. */
  function snapStep(label, snap, restore, fn) {
    return act(label, async () => {
      const before = snap();
      // read after fn, success or not: a failed re-mix can still have changed the votes,
      // and that half-step must be undoable too
      try { await fn(); } catch (e) { say(e.message, true); }
      const after = snap();
      if (JSON.stringify(before) === JSON.stringify(after)) return null;
      return { undo: () => restore(before), redo: () => restore(after) };
    });
  }

  function move(from, to, verb) {
    return run(async () => {
      const step = from.pop();
      if (!step) { say(`Nothing to ${verb.toLowerCase()}`, true); return false; }
      let ok;
      try { ok = await (verb === 'Undo' ? step.undo() : step.redo()); }
      catch (e) { ok = false; say(e.message, true); }
      if (ok === false) {
        from.push(step);                       // left where it was, so it can be tried again
        say(`Could not ${verb.toLowerCase()}: ${step.label}`, true);
      } else {
        to.push(step);
        say(`${verb === 'Undo' ? 'Undone' : 'Redone'}: ${step.label}`);
      }
      paint();
      return ok !== false;
    });
  }
  const undo = () => move(done, undone, 'Undo');
  const redo = () => move(undone, done, 'Redo');

  // the two toolbar buttons and the two lines at the top of the right-click menu
  function paint() {
    if (typeof document === 'undefined') return;
    const u = done[done.length - 1], r = undone[undone.length - 1];
    const set = (id, step, verb, keys) => {
      const el = document.getElementById(id);
      if (!el) return;
      el.disabled = !step;
      el.classList.toggle('off', !step);
      const text = step ? `${verb}: ${step.label} (${keys})` : `Nothing to ${verb.toLowerCase()}`;
      el.title = text; el.dataset.tip = text;
      const lbl = el.querySelector('.hlbl');
      if (lbl) lbl.textContent = step ? `${verb}: ${step.label}` : `${verb}`;
    };
    set('btnUndo', u, 'Undo', 'Ctrl+Z'); set('btnRedo', r, 'Redo', 'Ctrl+Y');
    set('ctxUndo', u, 'Undo', 'Ctrl+Z'); set('ctxRedo', r, 'Redo', 'Ctrl+Y');
  }

  return {
    act, snapStep, undo, redo, paint,
    get canUndo() { return done.length > 0; },
    get canRedo() { return undone.length > 0; },
    get undoLabel() { return done.length ? done[done.length - 1].label : ''; },
    get redoLabel() { return undone.length ? undone[undone.length - 1].label : ''; },
    idle: () => chain,
  };
})();
