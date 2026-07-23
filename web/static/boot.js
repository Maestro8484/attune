/* Attune Studio boot — one place that decides init order:
   theme first (no flash), then core data, then the player and preferences UI.

   FAULT ISOLATION (this is the point of this file): every step below runs
   independently. Before, boot did `try { await initCore() } catch { return }`,
   so ONE slow/failing dependency -- a network playlist folder on M:, a library
   fetch, a server still finishing its load -- aborted the whole sequence and
   left the window permanently half-built: no transport, no EQ, no preferences,
   no auto-playlists, no first-run wizard. A subsystem that fails now costs you
   that subsystem and nothing else. */
'use strict';

(async function boot() {
  try { Prefs.applyThemeEarly(); } catch (e) { console.error('[boot] theme', e); }

  // Core = server stats. It retries (the server may still be loading the
  // library) and degrades instead of throwing; see initCore in studio.js.
  const core = await initCore();

  // Independent subsystems. Each is isolated: a throw here is logged and the
  // remaining ones still initialize.
  const steps = [
    ['player', () => Player.init()],            // transport, queue restore, EQ panel
    ['preferences', () => Prefs.init()],        // modal, splitters, mini mode, scan poll
    ['first-run', () => Prefs.checkFirstRun()], // "point at your music" wizard
    ['auto-playlists', () => Smart.init()],     // tree + rules editor
  ];
  for (const [name, fn] of steps) {
    try { fn(); } catch (e) { console.error(`[boot] ${name} failed`, e); }
  }

  if (!core.ok) {
    toast('Attune started with limited data: ' + core.error +
          ' — the rest of the app still works.', true);
  }
})();
