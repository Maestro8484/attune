/* Attune Studio boot — one place that decides init order:
   theme first (no flash), then core data, then the player and preferences UI. */
'use strict';

(async function boot() {
  Prefs.applyThemeEarly();
  try {
    await initCore();          // studio.js — stats, playlists, library view
  } catch {
    return;                    // initCore already toasted the failure
  }
  Player.init();               // player.js — transport, queue restore, EQ panel
  Prefs.init();                // prefs.js — modal, splitters, mini mode, scan poll
  Smart.init();                // smartlist.js — auto-playlists tree + rules editor
})();
