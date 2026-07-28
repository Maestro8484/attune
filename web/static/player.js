/* Attune player — Web Audio playback engine.
   Dual <audio> elements A/B feed one graph, so tracks can crossfade / start gapless:

     mediaSource[A] → xfadeGain[A] → rgGain[A] ─┐
     mediaSource[B] → xfadeGain[B] → rgGain[B] ─┴→ preamp → EQ×10 → master → analyser → out

   Queue, shuffle/repeat, play-count + skip reporting (scrobble rules), ReplayGain from
   file tags, the spectrum/oscilloscope visualizer and the EQ panel all live here.
   State persists in localStorage (see `store` in studio.js). */
'use strict';

const Player = (() => {
  // Winamp's classic 10 bands.
  const FREQS = [60, 170, 310, 600, 1000, 3000, 6000, 12000, 14000, 16000];
  const PRESETS = {
    'Flat':         [0, 0, 0, 0, 0, 0, 0, 0, 0, 0],
    'Rock':         [5, 3, -4, -6, -2, 3, 6, 7, 7, 7],
    'Pop':          [-1, 3, 5, 5, 3, -1, -2, -2, -1, -1],
    'Jazz':         [3, 2, 1, 2, -2, -2, 0, 1, 2, 3],
    'Classical':    [0, 0, 0, 0, 0, 0, -6, -6, -6, -8],
    'Club':         [0, 0, 2, 4, 4, 4, 2, 0, 0, 0],
    'Dance':        [7, 5, 2, 0, 0, -4, -5, -5, 0, 0],
    'Full Bass':    [6, 6, 6, 4, 1, -3, -6, -8, -8, -8],
    'Full Treble':  [-8, -8, -8, -3, 2, 8, 10, 11, 11, 12],
    'Vocal':        [-2, -4, -3, 1, 4, 4, 3, 1, 0, -2],
  };

  const P = {
    els: [], cur: 0,               // two <audio>; els[cur] is the audible one
    actx: null, xg: [], rg: [], preamp: null, eq: [], master: null, analyser: null,
    q: [], pos: -1,
    shuffle: store.get('shuffle', false),
    repeat: store.get('repeat', 'off'),        // off | all | one
    autodj: store.get('autodj', false),        // infinite queue: refill with similar tracks
    radio: store.get('radio', false),          // Journey mode: refill via /api/radio/next
    radioArc: store.get('radioArc', 'flat'),   // flat | rise | fall | wave
    radioVariety: store.get('radioVariety', 3),
    radioPos: store.get('radioPos', 0),        // steps walked into the current energy arc
    djBusy: false,
    xfade: store.get('xfade', 0),
    rgOn: store.get('rg', false),
    vol: store.get('vol', 0.9),
    eqOn: store.get('eqOn', false),
    eqGains: store.get('eqGains', PRESETS.Flat.slice()),
    eqPreset: store.get('eqPreset', 'Flat'),
    visMode: store.get('vis', 'bars'),         // bars | scope | off
    rowCache: new Map(),                       // pool i -> row (title/artist/rating/...)
    played: false, startedAt: 0,               // per-track play/skip accounting
    fading: false, preloadedFor: -1,
    visPeaks: [],
  };

  /* ---------------------------------------------------------------- graph */
  function ensureGraph() {
    if (P.actx) return;
    const AC = window.AudioContext || window.webkitAudioContext;
    P.actx = new AC();
    P.preamp = P.actx.createGain();
    let node = P.preamp;
    P.eq = FREQS.map((f, k) => {
      const b = P.actx.createBiquadFilter();
      b.type = k === 0 ? 'lowshelf' : (k === FREQS.length - 1 ? 'highshelf' : 'peaking');
      b.frequency.value = f;
      if (b.type === 'peaking') b.Q.value = 1.1;
      b.gain.value = P.eqOn ? P.eqGains[k] : 0;
      node.connect(b); node = b;
      return b;
    });
    P.master = P.actx.createGain();
    P.master.gain.value = P.vol * P.vol;
    P.analyser = P.actx.createAnalyser();
    P.analyser.fftSize = 2048;
    P.analyser.smoothingTimeConstant = 0.82;
    node.connect(P.master); P.master.connect(P.analyser);
    P.analyser.connect(P.actx.destination);
    P.els.forEach((el, k) => {
      const src = P.actx.createMediaElementSource(el);
      P.xg[k] = P.actx.createGain();
      P.rg[k] = P.actx.createGain();
      P.xg[k].gain.value = k === P.cur ? 1 : 0;
      src.connect(P.xg[k]); P.xg[k].connect(P.rg[k]); P.rg[k].connect(P.preamp);
    });
    startVis();
  }

  function applyEq() {
    if (!P.actx) return;
    P.eq.forEach((b, k) => { b.gain.value = P.eqOn ? P.eqGains[k] : 0; });
  }

  async function applyReplayGain(elIdx, poolI) {
    if (!P.actx) return;
    let f = 1;
    if (P.rgOn) {
      try {
        const g = await jget('/api/track/gain?i=' + poolI);
        const db = (g.track_gain ?? g.album_gain);
        if (typeof db === 'number') f = Math.pow(10, Math.max(-20, Math.min(db, 15)) / 20);
      } catch { /* no gain info -> unchanged */ }
    }
    P.rg[elIdx].gain.value = f;
  }

  /* ---------------------------------------------------------------- rows */
  async function rowFor(i) {
    if (P.rowCache.has(i)) return P.rowCache.get(i);
    try {
      const j = await jget('/api/lib/rows?i=' + i);
      const r = j.rows[0];
      if (r) P.rowCache.set(i, r);
      return r;
    } catch { return null; }
  }

  /* ---------------------------------------------------------------- UI */
  function paintTransport() {
    $('tShuf').classList.toggle('on', P.shuffle);
    $('tRep').classList.toggle('on', P.repeat !== 'off');
    $('tRep').textContent = P.repeat === 'one' ? '🔂' : '🔁';
    $('tDj').classList.toggle('on', P.autodj);
    $('tEq').classList.toggle('on', P.eqOn);
    $('npN').textContent = P.q.length;
    // LCD status flags, Winamp-style
    $('lcdFlags').innerHTML =
      `<span class="${P.shuffle ? 'on' : ''}">SHUF</span> ` +
      `<span class="${P.repeat !== 'off' ? 'on' : ''}">REP${P.repeat === 'one' ? '1' : ''}</span> ` +
      `<span class="${P.autodj ? 'on' : ''}">DJ</span> ` +
      `<span class="${P.radio ? 'on' : ''}">RADIO</span> ` +
      `<span class="${P.eqOn ? 'on' : ''}">EQ</span>`;
  }
  // kbps / kHz readout — one tag read per track change, cached
  const techCache = new Map();
  async function paintTech(i) {
    let t = techCache.get(i);
    if (!t) {
      try {
        const j = await jget('/api/track/tags?i=' + i);
        t = j.info || {};
        techCache.set(i, t);
      } catch { t = {}; }
    }
    if (currentPool() !== i) return;             // user already skipped on
    $('lcdKbps').textContent = t.bitrate ? Math.round(t.bitrate / 1000) + ' kbps' : '--- kbps';
    $('lcdKhz').textContent = t.sample_rate ? (t.sample_rate / 1000) + ' kHz' : '-- kHz';
  }
  function toggleAutoDj() {
    P.autodj = !P.autodj; store.set('autodj', P.autodj);
    paintTransport();
    toast(P.autodj ? 'Auto-DJ on — the queue will keep itself full' : 'Auto-DJ off');
    if (P.autodj) maybeExtendQueue();
  }
  function toggleRadio() {
    P.radio = !P.radio; store.set('radio', P.radio);
    const on = $('radioOn'); if (on) on.checked = P.radio;
    paintTransport();
    toast(P.radio ? 'Radio on — an infinite journey from Now Playing' : 'Radio off');
    if (P.radio) { P.radioPos = 0; maybeExtendQueue(); }
  }
  // Radio mode (Journey): when fewer than 5 tracks remain upcoming, pull the next batch
  // from /api/radio/next -- the MusicIP-style variety walk + energy-arc corridor (see
  // hybrid.HybridEngine.radio_next()) -- seeded from Now Playing (falling back to the
  // last mix seed if nothing is playing yet), excluding the last 100 played/queued pool
  // indices so the walk doesn't loop back over what you just heard. Stateless server side:
  // this client owns `exclude` (from P.q) and `pos` (P.radioPos, how far into the energy
  // arc this session has walked) across repeated calls.
  async function extendQueueRadio() {
    if (P.q.length - P.pos >= 5) return;
    const seed = currentPool() >= 0 ? currentPool()
      : (typeof S !== 'undefined' && S.seed != null ? S.seed : -1);
    if (seed < 0) return;
    P.djBusy = true;
    try {
      const p = new URLSearchParams();
      p.set('seed', seed);
      p.set('n', 20);
      p.set('variety', P.radioVariety);
      p.set('arc', P.radioArc);
      p.set('pos', P.radioPos);
      const exclude = P.q.slice(-100);
      if (exclude.length) p.set('exclude', exclude.join(','));
      // ride the same mix dials (weights) Auto-DJ/Create Mix use -- never re-parsed here.
      if (typeof mixParams === 'function') {
        const mp = mixParams();
        for (const k of ['clap', 'lib', 'genre', 'bpm', 'era']) {
          if (mp.has(k)) p.set(k, mp.get(k));
        }
      }
      const j = await jget('/api/radio/next?' + p);
      const have = new Set(P.q);
      const fresh = (j.tracks || []).map(x => x.i).filter(i => i !== seed && !have.has(i));
      if (fresh.length) {
        P.q.push(...fresh);
        P.radioPos += fresh.length; store.set('radioPos', P.radioPos);
        paintTransport(); persist();
        if (S.view === 'nowplaying') showNowPlaying();
      }
    } catch { /* engine hiccup — try again on the next tick */ }
    finally { P.djBusy = false; }
  }
  // When the queue is within 3 of the end, append a fresh batch of tracks the engine
  // says sound like the current one — the "infinite radio" the recommendation engine is
  // actually for. De-dupes against what's already queued.
  async function extendQueueAutoDj() {
    if (P.q.length - P.pos > 3) return;
    const seed = currentPool();
    if (seed < 0) return;
    P.djBusy = true;
    try {
      const p = (typeof mixParams === 'function') ? mixParams() : new URLSearchParams();
      p.set('i', seed); p.set('size', 20);
      const j = await jget('/api/mix?' + p);
      const have = new Set(P.q);
      const fresh = (j.tracks || []).map(x => x.i).filter(i => i !== seed && !have.has(i));
      if (fresh.length) {
        P.q.push(...fresh);
        paintTransport(); persist();
        if (S.view === 'nowplaying') showNowPlaying();
      }
    } catch { /* engine hiccup — try again on the next tick */ }
    finally { P.djBusy = false; }
  }
  // Radio and Auto-DJ are alternative refill strategies for the same "queue is running
  // low" moment -- Radio (when on) takes priority; Auto-DJ's plain /api/mix refill is
  // the fallback for listeners who never touch Radio. Both no-op with nothing playing.
  async function maybeExtendQueue() {
    if (P.djBusy || P.pos < 0) return;
    if (P.radio) return extendQueueRadio();
    if (P.autodj) return extendQueueAutoDj();
  }
  function paintPlayBtn() {
    const playing = P.els[P.cur] && !P.els[P.cur].paused;
    $('tPlay').textContent = playing ? '⏸' : '▶';
    $('miniPlay').textContent = playing ? '⏸' : '▶';
  }
  async function paintNowPlaying(i) {
    const r = await rowFor(i);
    // LCD: "ARTIST - TITLE", marquee-scrolled when it overflows (the Winamp move)
    const lcdText = r ? `${r.artist || '?'} - ${r.title}` : 'ATTUNE :: ready';
    const track = $('lcdTrack');
    track.classList.remove('scroll');
    track.textContent = lcdText;
    requestAnimationFrame(() => {
      const wrap = track.parentElement;
      const over = track.scrollWidth - wrap.clientWidth;
      if (over > 4) {
        track.style.setProperty('--mq', `-${over + 12}px`);
        track.style.setProperty('--mqdur', `${Math.max(6, over / 18)}s`);
        track.classList.add('scroll');
      }
    });
    $('npArtistSmall').textContent = r ? (r.album || '') : '';
    $('miniTitle').textContent = r ? r.title : '—';
    $('miniArtist').textContent = r ? r.artist : '';
    paintNowPlayingMeta(r || {});
    paintTech(i);
    prepareWaveform(i);
    const img = new Image();
    img.onload = () => {
      $('art').innerHTML = ''; $('art').appendChild(img);
      const m = new Image(); m.src = img.src;
      m.onload = () => { $('miniArt').outerHTML = `<img id="miniArt" src="${img.src}">`; };
    };
    img.onerror = () => {
      $('art').innerHTML = '<span class="noart">♪</span>';
      $('miniArt').outerHTML = '<span class="noart" id="miniArt">♪</span>';
    };
    img.src = `/api/art?i=${i}`;
    if ('mediaSession' in navigator && r) {
      navigator.mediaSession.metadata = new MediaMetadata({
        title: r.title, artist: r.artist, album: r.album,
        artwork: [{ src: `/api/art?i=${i}`, sizes: '512x512' }],
      });
    }
    repaintRowState();
  }
  function paintNowPlayingMeta(r) {
    if ('rating' in r) {
      const cached = P.rowCache.get(currentPool());
      if (cached) cached.rating = r.rating;
      $('npStars').innerHTML = [1, 2, 3, 4, 5].map(n =>
        `<i data-s="${n}" class="${n <= (r.rating || 0) ? 'on' : ''}">${n <= (r.rating || 0) ? '★' : '☆'}</i>`).join('');
    }
    if ('loved' in r) {
      const cached = P.rowCache.get(currentPool());
      if (cached) cached.loved = r.loved;
      $('npHeart').classList.toggle('on', !!r.loved);
    }
    if ('plays' in r) $('npPlays').textContent = r.plays ? `${r.plays} plays` : '';
  }

  /* ---------------------------------------------------------------- core playback */
  function currentPool() { return P.pos >= 0 && P.pos < P.q.length ? P.q[P.pos] : -1; }

  function persist() {
    store.set('queue', P.q); store.set('qpos', P.pos);
    store.set('shuffle', P.shuffle); store.set('repeat', P.repeat);
  }

  async function loadInto(elIdx, poolI, { autoplay = true } = {}) {
    const el = P.els[elIdx];
    el.src = `/audio?i=${poolI}`;
    applyReplayGain(elIdx, poolI);
    if (autoplay) {
      try { await el.play(); } catch { /* autoplay blocked until gesture */ }
    }
  }

  async function playAt(qpos, { autoplay = true } = {}) {
    if (qpos < 0 || qpos >= P.q.length) return;
    ensureGraph();
    if (P.actx.state === 'suspended') P.actx.resume();
    // report a skip if we abandoned the previous track early
    reportSkipIfAbandoned();
    P.pos = qpos;
    const i = P.q[qpos];
    P.played = false; P.startedAt = Date.now(); P.fading = false; P.preloadedFor = -1;
    maybeExtendQueue();
    const el = P.els[P.cur];
    P.xg[P.cur].gain.cancelScheduledValues(P.actx.currentTime);
    P.xg[P.cur].gain.value = 1;
    const other = 1 - P.cur;
    P.els[other].pause();
    P.xg[other].gain.cancelScheduledValues(P.actx.currentTime);
    P.xg[other].gain.value = 0;
    await loadInto(P.cur, i, { autoplay });
    paintPlayBtn();
    paintNowPlaying(i);
    paintTransport();
    persist();
  }

  function playList(ids, at = 0) {
    P.q = ids.slice();
    if (P.shuffle && ids.length > 1) {
      // keep the chosen track first, shuffle the rest — what "shuffle on" means here
      const head = P.q.splice(at, 1);
      shuffleArr(P.q);
      P.q = head.concat(P.q);
      at = 0;
    }
    playAt(at);
  }

  function playTrack(i) {
    const at = P.q.indexOf(i);
    if (at >= 0) return playAt(at);
    P.q.splice(P.pos + 1, 0, i);
    playAt(P.pos + 1);
  }

  function queueAdd(ids, { next = false } = {}) {
    const at = next ? P.pos + 1 : P.q.length;
    P.q.splice(at, 0, ...ids);
    if (P.pos < 0) { playAt(0); return; }
    paintTransport(); persist();
  }
  function removeFromQueue(ids) {
    const set = new Set(ids);
    const curI = currentPool();
    const removedOnce = new Set();
    // Queue identity is the pool index, so a track queued twice shares one id. Drop only
    // ONE occurrence per requested id (never the playing entry) — not every occurrence.
    P.q = P.q.filter((x, k) => {
      if (k === P.pos) return true;
      if (set.has(x) && !removedOnce.has(x)) { removedOnce.add(x); return false; }
      return true;
    });
    P.pos = P.q.indexOf(curI);
    paintTransport(); persist();
  }
  function clearQueue() {
    const curI = currentPool();
    P.q = curI >= 0 ? [curI] : [];
    P.pos = curI >= 0 ? 0 : -1;
    paintTransport(); persist();
  }
  function moveInQueue(from, to) {
    if (from === to) return;
    const curI = currentPool();
    const [x] = P.q.splice(from, 1);
    P.q.splice(to, 0, x);
    P.pos = P.q.indexOf(curI);
    persist();
  }
  function shuffleArr(a) {
    for (let i = a.length - 1; i > 0; i--) {
      const j = Math.floor(Math.random() * (i + 1)); [a[i], a[j]] = [a[j], a[i]];
    }
  }
  function shuffleQueue() {          // reshuffle only what hasn't played yet
    const tail = P.q.splice(P.pos + 1);
    shuffleArr(tail);
    P.q = P.q.concat(tail);
    persist();
  }
  function toggleShuffle() {
    P.shuffle = !P.shuffle;
    if (P.shuffle) shuffleQueue();
    paintTransport(); persist();
    toast(P.shuffle ? 'Shuffle on (upcoming queue reshuffled)' : 'Shuffle off');
  }
  function cycleRepeat() {
    P.repeat = { off: 'all', all: 'one', one: 'off' }[P.repeat];
    paintTransport(); persist();
    toast('Repeat: ' + P.repeat);
  }

  function next() { advance(true); }
  function prev() {
    const el = P.els[P.cur];
    if (el.currentTime > 3) { el.currentTime = 0; return; }   // standard prev behaviour
    if (P.pos > 0) playAt(P.pos - 1);
    else el.currentTime = 0;
  }
  function advance(manual = false) {
    if (P.repeat === 'one' && !manual) { playAt(P.pos); return; }
    let nextPos = -1;
    if (P.pos + 1 < P.q.length) nextPos = P.pos + 1;
    else if (P.repeat === 'all' && P.q.length) nextPos = 0;
    if (nextPos < 0) { P.els[P.cur].pause(); paintPlayBtn(); return; }
    // Gapless: on a NATURAL end, if the next track is already preloaded in the other
    // element, switch to it instantly instead of reloading into P.cur (which would add
    // fetch+decode latency — the audible gap). Manual skips just playAt().
    const other = 1 - P.cur;
    if (!manual && P.preloadedFor === nextPos && P.els[other].readyState >= 3) {
      swapToPreloaded(nextPos);
    } else {
      playAt(nextPos);
    }
  }
  function swapToPreloaded(nextPos) {
    ensureGraph();
    reportSkipIfAbandoned();
    const oldIdx = P.cur, newIdx = 1 - P.cur, i = P.q[nextPos];
    const t = P.actx.currentTime;
    P.xg[newIdx].gain.cancelScheduledValues(t); P.xg[newIdx].gain.value = 1;
    P.xg[oldIdx].gain.cancelScheduledValues(t); P.xg[oldIdx].gain.value = 0;
    applyReplayGain(newIdx, i);
    P.els[newIdx].play().catch(() => {});
    P.els[oldIdx].pause();
    P.cur = newIdx; P.pos = nextPos;
    P.played = false; P.startedAt = Date.now(); P.preloadedFor = -1; P.fading = false;
    paintPlayBtn(); paintNowPlaying(i); paintTransport(); persist();
    maybeExtendQueue();
  }

  function stop() {
    // full stop, Winamp semantics: halt playback AND rewind to the track start.
    // Queue and position stay; Play resumes this track from 0:00.
    const el = P.els[P.cur];
    if (!el.src) return;
    el.pause();
    el.currentTime = 0;
    paintPlayBtn();
  }

  function togglePlay() {
    const el = P.els[P.cur];
    if (!el.src) {
      const i = firstSelected();
      if (i != null) {
        const rest = S.rows.filter(r => r.i >= 0).map(r => r.i);
        playList(rest, rest.indexOf(i));
      }
      return;
    }
    ensureGraph();
    if (P.actx.state === 'suspended') P.actx.resume();
    if (el.paused) el.play().catch(() => {}); else el.pause();
    paintPlayBtn();
  }

  /* ---------------------------------------------------------------- play/skip accounting */
  function reportPlayIfDue() {
    if (P.played) return;
    const el = P.els[P.cur], i = currentPool();
    if (i < 0 || !el.duration) return;
    if (el.currentTime >= Math.min(el.duration * 0.5, 240)) {
      P.played = true;
      jpost('/api/track/played', { i }).then(j => {
        const r = P.rowCache.get(i); if (r) r.plays = j.plays;
        for (const row of S.rows) if (row.i === i) row.plays = j.plays;
        if (currentPool() === i) paintNowPlayingMeta({ plays: j.plays });
      }).catch(() => {});
    }
  }
  function reportSkipIfAbandoned() {
    const el = P.els[P.cur], i = currentPool();
    if (i < 0 || P.played || !el.duration) return;
    if (el.currentTime > 3 && el.currentTime < el.duration * 0.33)
      jpost('/api/track/skipped', { i }).catch(() => {});
  }

  /* ---------------------------------------------------------------- crossfade / gapless */
  function maybePrepareNext() {
    const el = P.els[P.cur];
    if (!el.duration || P.fading) return;
    let nextPos = P.pos + 1;
    if (nextPos >= P.q.length) nextPos = (P.repeat === 'all' && P.q.length) ? 0 : -1;
    if (P.repeat === 'one') nextPos = P.pos;
    if (nextPos < 0) return;
    const remaining = el.duration - el.currentTime;
    const other = 1 - P.cur;
    if (remaining < 20 && P.preloadedFor !== nextPos) {
      P.els[other].src = `/audio?i=${P.q[nextPos]}`;
      P.els[other].load();
      P.preloadedFor = nextPos;
    }
    if (P.xfade > 0 && remaining <= P.xfade && nextPos !== P.pos) {
      startCrossfade(nextPos, Math.max(0.3, Math.min(P.xfade, remaining)));
    }
  }
  async function startCrossfade(nextPos, secs) {
    P.fading = true;
    reportPlayIfDue();
    const oldIdx = P.cur, newIdx = 1 - P.cur;
    const i = P.q[nextPos];
    const t = P.actx.currentTime;
    if (P.preloadedFor !== nextPos) P.els[newIdx].src = `/audio?i=${i}`;
    applyReplayGain(newIdx, i);
    try { await P.els[newIdx].play(); } catch { P.fading = false; return; }
    P.xg[newIdx].gain.cancelScheduledValues(t);
    P.xg[newIdx].gain.setValueAtTime(0, t);
    P.xg[newIdx].gain.linearRampToValueAtTime(1, t + secs);
    P.xg[oldIdx].gain.cancelScheduledValues(t);
    P.xg[oldIdx].gain.setValueAtTime(P.xg[oldIdx].gain.value, t);
    P.xg[oldIdx].gain.linearRampToValueAtTime(0, t + secs);
    P.cur = newIdx;
    P.pos = nextPos;
    P.played = false; P.startedAt = Date.now(); P.preloadedFor = -1;
    setTimeout(() => {
      P.els[oldIdx].pause();
      P.fading = false;
    }, secs * 1000 + 80);
    paintNowPlaying(i); paintTransport(); paintPlayBtn(); persist();
  }

  /* ---------------------------------------------------------------- waveform seekbar */
  const waveCache = new Map();     // pool i -> Float32Array peaks | 'pending'
  let waveAbort = null, decodeCtx = null;

  function drawWave() {
    const cv = $('wave');
    if (!cv) return;
    if (cv.clientWidth && cv.width !== cv.clientWidth) cv.width = cv.clientWidth;
    const g = cv.getContext('2d');
    const el = P.els[P.cur];
    const W = cv.width, H = cv.height;
    const frac = (el && el.duration) ? el.currentTime / el.duration : 0;
    const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
    const played = css('--accent') || '#6d8cff', rest = css('--dim') || '#666';
    g.clearRect(0, 0, W, H);
    const peaks = waveCache.get(currentPool());
    if (peaks && peaks !== 'pending') {
      const n = peaks.length, bw = W / n;
      for (let k = 0; k < n; k++) {
        const h = Math.max(1.5, peaks[k] * (H - 2));
        g.fillStyle = (k / n <= frac) ? played : rest;
        g.globalAlpha = (k / n <= frac) ? 1 : 0.45;
        g.fillRect(k * bw + 0.5, (H - h) / 2, Math.max(1, bw - 1), h);
      }
      g.globalAlpha = 1;
    } else {
      g.fillStyle = rest; g.globalAlpha = 0.35;
      g.fillRect(0, H / 2 - 1.5, W, 3);
      g.globalAlpha = 1; g.fillStyle = played;
      g.fillRect(0, H / 2 - 1.5, W * frac, 3);
    }
  }

  // Decode the current track into ~240 peak buckets, off the critical path: waits
  // 800ms after the track change (streaming start wins), aborts if the user skips on,
  // caches the last 12 tracks. The bar seeks fine before the waveform arrives.
  function prepareWaveform(i) {
    drawWave();
    if (waveCache.has(i)) return;
    if (waveAbort) waveAbort.abort();
    const ctrl = (waveAbort = new AbortController());
    setTimeout(async () => {
      if (ctrl.signal.aborted || currentPool() !== i) return;
      waveCache.set(i, 'pending');
      try {
        const buf = await (await fetch('/audio?i=' + i, { signal: ctrl.signal })).arrayBuffer();
        decodeCtx = decodeCtx || new (window.AudioContext || window.webkitAudioContext)();
        const audio = await decodeCtx.decodeAudioData(buf);
        const ch = audio.getChannelData(0);
        const N = 240, step = Math.max(1, Math.floor(ch.length / N));
        const peaks = new Float32Array(N);
        for (let k = 0; k < N; k++) {
          let m = 0;
          for (let s = k * step, e2 = Math.min(s + step, ch.length); s < e2; s += 32) {
            const v = Math.abs(ch[s]);
            if (v > m) m = v;
          }
          peaks[k] = m;
        }
        let max = 0.01;
        for (const v of peaks) if (v > max) max = v;
        for (let k = 0; k < N; k++) peaks[k] /= max;
        waveCache.set(i, peaks);
        while (waveCache.size > 12) waveCache.delete(waveCache.keys().next().value);
        if (currentPool() === i) drawWave();
      } catch {
        waveCache.delete(i);           // aborted or undecodable — plain bar remains
      }
    }, 800);
  }

  /* ---------------------------------------------------------------- visualizer */
  let visRAF = 0;
  function startVis() {
    cancelAnimationFrame(visRAF);
    const cv = $('vis'), g = cv.getContext('2d');
    const freq = new Uint8Array(P.analyser.frequencyBinCount);
    const wave = new Uint8Array(P.analyser.fftSize);
    const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
    const draw = () => {
      visRAF = requestAnimationFrame(draw);
      if (document.hidden || P.visMode === 'off') { g.clearRect(0, 0, cv.width, cv.height); return; }
      if (cv.width !== cv.clientWidth) cv.width = cv.clientWidth;
      const W = cv.width, H = cv.height;
      g.clearRect(0, 0, W, H);
      if (P.visMode === 'bars') {
        // Segmented LED meter — the classic Winamp spectrum: discrete 3px cells,
        // green through amber to red by height, with falling peak caps.
        P.analyser.getByteFrequencyData(freq);
        const N = 40, bw = W / N;
        const segH = 3, gap = 1, rows = Math.floor((H - 2) / (segH + gap));
        if (P.visPeaks.length !== N) P.visPeaks = new Array(N).fill(0);
        const cLow = css('--led-low') || css('--vis1');
        const cMid = css('--led-mid') || css('--vis2');
        const cHigh = css('--led-high') || '#ff4b3e';
        const cPeak = css('--vis-peak');
        for (let k = 0; k < N; k++) {
          // log-spaced bins so bass doesn't hog half the strip
          const lo = Math.floor(Math.pow(freq.length * 0.75, k / N));
          const hi = Math.max(lo + 1, Math.floor(Math.pow(freq.length * 0.75, (k + 1) / N)));
          let v = 0; for (let b = lo; b < hi; b++) v = Math.max(v, freq[b]);
          const lit = Math.round((v / 255) * rows);
          for (let s = 0; s < lit; s++) {
            const fracRow = s / rows;
            g.fillStyle = fracRow < 0.6 ? cLow : (fracRow < 0.85 ? cMid : cHigh);
            g.fillRect(k * bw + 1, H - 1 - (s + 1) * (segH + gap), bw - 2, segH);
          }
          // falling peak cap
          P.visPeaks[k] = Math.max(P.visPeaks[k] - 0.18, lit);
          const cap = Math.round(P.visPeaks[k]);
          if (cap > 0) {
            g.fillStyle = cPeak;
            g.fillRect(k * bw + 1, H - 1 - (cap + 1) * (segH + gap) - 1, bw - 2, 1.5);
          }
        }
      } else {
        P.analyser.getByteTimeDomainData(wave);
        g.strokeStyle = css('--vis1'); g.lineWidth = 1.5;
        g.beginPath();
        for (let x = 0; x < W; x++) {
          const v = wave[Math.floor(x / W * wave.length)] / 255;
          const y = v * H;
          x ? g.lineTo(x, y) : g.moveTo(x, y);
        }
        g.stroke();
      }
    };
    draw();
  }

  /* ---------------------------------------------------------------- EQ panel */
  function buildEqPanel() {
    $('eqBands').innerHTML = FREQS.map((f, k) => `
      <div class="eqBand">
        <span class="v" id="eqv${k}">${P.eqGains[k] > 0 ? '+' : ''}${P.eqGains[k]}</span>
        <input type="range" min="-12" max="12" step="1" value="${P.eqGains[k]}" data-band="${k}">
        <span class="f">${f >= 1000 ? (f / 1000) + 'k' : f}</span>
      </div>`).join('');
    $('eqPreset').innerHTML = Object.keys(PRESETS).map(n =>
      `<option ${n === P.eqPreset ? 'selected' : ''}>${n}</option>`).join('') +
      `<option ${P.eqPreset === 'Custom' ? 'selected' : ''}>Custom</option>`;
    $('eqOn').checked = P.eqOn;
    $('rgOn').checked = P.rgOn;
    $('xfade').value = P.xfade;
    $('xfadeV').textContent = P.xfade + 's';

    $('eqBands').addEventListener('input', e => {
      const inp = e.target.closest('input[data-band]'); if (!inp) return;
      const k = +inp.dataset.band;
      P.eqGains[k] = +inp.value;
      $('eqv' + k).textContent = (P.eqGains[k] > 0 ? '+' : '') + P.eqGains[k];
      P.eqPreset = 'Custom';
      $('eqPreset').value = 'Custom';
      P.eqOn = true; $('eqOn').checked = true;
      ensureGraph(); applyEq(); saveEq();
    });
    $('eqPreset').addEventListener('change', () => {
      const n = $('eqPreset').value;
      if (!PRESETS[n]) return;
      P.eqPreset = n;
      P.eqGains = PRESETS[n].slice();
      P.eqGains.forEach((v, k) => {
        $('eqv' + k).textContent = (v > 0 ? '+' : '') + v;
        $('eqBands').querySelector(`input[data-band="${k}"]`).value = v;
      });
      // Picking a preset means "use the EQ" — enable it so the choice is audible
      // instead of silently writing gains that applyEq() then zeroes out.
      P.eqOn = true; $('eqOn').checked = true;
      ensureGraph(); applyEq(); saveEq(); paintTransport();
    });
    $('eqOn').addEventListener('change', () => {
      P.eqOn = $('eqOn').checked; ensureGraph(); applyEq(); saveEq(); paintTransport();
    });
    $('eqFlat').onclick = () => { $('eqPreset').value = 'Flat';
      $('eqPreset').dispatchEvent(new Event('change')); };
    $('rgOn').addEventListener('change', () => {
      P.rgOn = $('rgOn').checked; store.set('rg', P.rgOn);
      const i = currentPool();
      if (i >= 0 && P.actx) applyReplayGain(P.cur, i);
    });
    $('xfade').addEventListener('input', () => {
      P.xfade = +$('xfade').value;
      $('xfadeV').textContent = P.xfade + 's';
      store.set('xfade', P.xfade);
    });
  }
  function saveEq() {
    store.set('eqOn', P.eqOn); store.set('eqGains', P.eqGains);
    store.set('eqPreset', P.eqPreset);
  }
  function toggleEqPanel() { $('eqPanel').hidden = !$('eqPanel').hidden; }

  /* ---------------------------------------------------------------- Radio controls
     Lives in the same Mix Options popover as the dial sliders (studio.html's
     #v2Controls), wired here (not studio.js) because it's queue/playback behaviour,
     the same split EQ/xfade/replaygain already follow. */
  function bindRadioControls() {
    const on = $('radioOn'), arc = $('radioArc'), variety = $('radioVariety');
    if (!on || !arc || !variety) return;      // options popover not present (e.g. classic view)
    on.checked = P.radio;
    arc.value = P.radioArc;
    variety.value = P.radioVariety;
    on.addEventListener('change', () => {
      P.radio = on.checked; store.set('radio', P.radio);
      if (P.radio) P.radioPos = 0;
      paintTransport();
      toast(P.radio ? 'Radio on — an infinite journey from Now Playing' : 'Radio off');
      if (P.radio) maybeExtendQueue();
    });
    arc.addEventListener('change', () => {
      P.radioArc = arc.value; store.set('radioArc', P.radioArc);
    });
    variety.addEventListener('input', () => {
      P.radioVariety = +variety.value; store.set('radioVariety', P.radioVariety);
    });
  }

  /* ---------------------------------------------------------------- wiring */
  function bind() {
    P.els = [$('audio'), $('audioB')];
    for (const el of P.els) {
      el.addEventListener('ended', () => {
        if (el !== P.els[P.cur]) return;
        if (P.fading) return;                     // crossfade already advanced us
        reportPlayIfDue();
        advance(false);
      });
      el.addEventListener('timeupdate', () => {
        if (el !== P.els[P.cur]) return;
        if (el.duration) {
          $('miniSeek').value = (el.currentTime / el.duration) * 1000;
          $('tCur').textContent = hms(el.currentTime);
          $('tDur').textContent = hms(el.duration);
          $('lcdTime').textContent = hms(el.currentTime);
          drawWave();
        }
        reportPlayIfDue();
        maybePrepareNext();
      });
      el.addEventListener('play', paintPlayBtn);
      el.addEventListener('pause', paintPlayBtn);
      el.addEventListener('error', () => {
        if (el !== P.els[P.cur] || !el.src) return;
        toast('Playback failed — skipping', true);
        setTimeout(() => advance(false), 400);
      });
    }
    $('tPlay').onclick = togglePlay;
    $('miniPlay').onclick = togglePlay;
    $('tStop').onclick = stop;
    $('tPrev').onclick = prev; $('miniPrev').onclick = prev;
    $('tNext').onclick = () => next(); $('miniNext').onclick = () => next();
    $('tShuf').onclick = toggleShuffle;
    $('tRep').onclick = cycleRepeat;
    $('tDj').onclick = toggleAutoDj;
    $('tEq').onclick = toggleEqPanel;
    bindRadioControls();
    const seekTo = v => {
      const el = P.els[P.cur];
      if (el.duration) el.currentTime = (v / 1000) * el.duration;
    };
    $('miniSeek').addEventListener('input', () => seekTo(+$('miniSeek').value));
    // waveform canvas seek: click or drag anywhere on it
    const waveSeek = e => {
      const el = P.els[P.cur];
      if (!el || !el.duration) return;
      const r = $('wave').getBoundingClientRect();
      const frac = Math.min(Math.max((e.clientX - r.left) / r.width, 0), 0.999);
      el.currentTime = frac * el.duration;
      drawWave();
    };
    $('wave').addEventListener('pointerdown', e => {
      e.preventDefault();
      waveSeek(e);
      const mv = ev => waveSeek(ev);
      const up = () => { removeEventListener('pointermove', mv); removeEventListener('pointerup', up); };
      addEventListener('pointermove', mv); addEventListener('pointerup', up);
    });
    $('tVol').addEventListener('input', () => {
      P.vol = +$('tVol').value; store.set('vol', P.vol);
      if (P.master) P.master.gain.value = P.vol * P.vol;
      else P.els.forEach(el => el.volume = P.vol);   // pre-graph fallback
    });
    // now-playing pane rating + heart
    $('npStars').addEventListener('click', e => {
      const st = e.target.closest('i'); if (!st) return;
      const i = currentPool(); if (i < 0) return;
      const cur = P.rowCache.get(i)?.rating || 0;
      const n = +st.dataset.s;
      rateTrack(i, n === cur ? 0 : n);
    });
    $('npHeart').addEventListener('click', () => {
      const i = currentPool(); if (i < 0) return;
      loveTrack(i, !(P.rowCache.get(i)?.loved));
    });
    $('vis').addEventListener('click', () => {
      P.visMode = { bars: 'scope', scope: 'off', off: 'bars' }[P.visMode];
      store.set('vis', P.visMode);
      toast('Visualizer: ' + P.visMode);
    });
    if ('mediaSession' in navigator) {
      navigator.mediaSession.setActionHandler('play', togglePlay);
      navigator.mediaSession.setActionHandler('pause', togglePlay);
      navigator.mediaSession.setActionHandler('previoustrack', prev);
      navigator.mediaSession.setActionHandler('nexttrack', () => next());
    }
    buildEqPanel();
  }

  /* ---------------------------------------------------------------- boot */
  function init() {
    bind();
    $('tVol').value = P.vol;
    P.els.forEach(el => el.volume = P.vol);      // real volume until the graph exists
    // restore the queue (paused — never blast audio at app start)
    P.q = store.get('queue', []);
    const pos = store.get('qpos', -1);
    if (P.q.length && pos >= 0 && pos < P.q.length) {
      P.pos = pos;
      const i = P.q[pos];
      paintNowPlaying(i);
      P.els[P.cur].src = `/audio?i=${i}`;
      P.els[P.cur].preload = 'metadata';
    }
    paintTransport();
  }

  return {
    // state the views read
    get q() { return P.q; },
    get pos() { return P.pos; },
    currentPool, init,
    // actions
    playAt, playList, playTrack, queueAdd, removeFromQueue, clearQueue,
    moveInQueue, shuffleQueue, toggleShuffle, cycleRepeat,
    togglePlay, stop, next, prev, toggleEqPanel, toggleAutoDj, toggleRadio,
    paintNowPlayingMeta,
  };
})();
