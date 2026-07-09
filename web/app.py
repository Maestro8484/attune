"""Attune Web — a tiny browser front-end for the V2 hybrid engine.

Search your library, click a song, get a playlist that sounds like it.
This talks straight to HybridEngine (attune/src/hybrid.py) — no MusicIP, no
external services. Everything runs locally.

Run (--db is required):
    python -m attune.web.app --db path/to/mixer.db
    # or, from the attune/ dir:
    python web/app.py --db ../mixer-ng/data/mixer.db

Then open http://127.0.0.1:8778 in a browser.

Point --db at a library that has been analyzed AND CLAP-embedded (see
scan.py / embed.py): the hybrid engine needs a populated `clap` table and
refuses to start without one. The library is loaded ONCE at startup, so
point --db at a database that is not being actively written (e.g. mid
analyze/embed) — newly-added tracks only appear after a restart.
"""
import argparse
import importlib.util
import os
import sqlite3

from flask import Flask, Response, jsonify, render_template_string, request

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")


def _load_hybrid():
    """Import hybrid.py by path so this works whether or not attune is installed."""
    spec = importlib.util.spec_from_file_location("hybrid", os.path.join(SRC, "hybrid.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _label(eng, path):
    m = eng.meta.get(path, {})
    return f"{m.get('artist') or '?'} - {m.get('title') or os.path.basename(path)}"


def _check_db(db_path):
    """Fail fast with an actionable message if the DB can't drive the V2 engine.

    HybridEngine needs a populated `clap` table; without one it SystemExits with
    a terse note. Catch the common cases here so a first-time run gets told what
    to do instead of a raw stack trace.
    """
    if not os.path.exists(db_path):
        raise SystemExit(
            f"No database at: {db_path}\n"
            f"Pass --db pointing at your analyzed library, e.g.\n"
            f"    python web/app.py --db ../mixer-ng/data/mixer.db")
    con = sqlite3.connect(db_path)
    try:
        clap = con.execute("SELECT COUNT(*) FROM clap WHERE vec IS NOT NULL").fetchone()[0]
    except sqlite3.OperationalError:
        clap = 0                       # no 'clap' table at all
    finally:
        con.close()
    if not clap:
        raise SystemExit(
            f"{db_path} has no CLAP embeddings (empty or missing 'clap' table).\n"
            f"The V2 web engine needs them. Embed first:\n"
            f"    python attune/src/embed.py --db {db_path}\n"
            f"or point --db at a library that already has CLAP vectors.")


def create_app(db_path):
    _check_db(db_path)
    hybrid = _load_hybrid()
    print(f"Loading library from {db_path} ...")
    eng = hybrid.HybridEngine(db_path)
    # precompute a lowercased label per pool index once; search is then a cheap scan
    labels = [_label(eng, p) for p in eng.paths]
    labels_lc = [s.lower() for s in labels]
    print(f"Ready — {len(eng.paths):,} songs in the mixable pool.")

    # export: path-map (+ optional Plex), config auto-discovered from a .env
    exp_spec = importlib.util.spec_from_file_location("attune_export", os.path.join(SRC, "export.py"))
    export = importlib.util.module_from_spec(exp_spec)
    exp_spec.loader.exec_module(export)
    cfg = export.load_env()
    mapper = export.mapper_from_env(cfg)
    plex_holder = {}                    # lazily-built PlexExporter (indexes Plex on first use)
    plex_configured = all(cfg.get(k) for k in
                          ("PLEX_URL", "PLEX_ACCOUNT_TOKEN", "PLEX_MACHINE_ID", "PLEX_LIBRARY_ROOT"))

    DEDUP_FIELDS = ("song", "title", "file")   # what counts as a "duplicate"

    def _dupkey(path, field):
        m = eng.meta.get(path, {})
        if field == "song":
            return f"{(m.get('artist') or '').lower()}|{(m.get('title') or '').lower()}"
        if field == "title":
            return (m.get("title") or os.path.basename(path)).lower()
        if field == "file":
            return os.path.basename(path).lower()
        return None

    def _build_mix(i, size, field=None):
        """Return (seed_path, picks). With a dedup field, over-fetch then collapse
        same-key tracks (and any that duplicate the seed), so you still get `size`."""
        seed = eng.paths[i]
        picks = eng.mix(seed, size=min(size * 4, 200) if field else size) or []
        if field:
            seen, uniq = {_dupkey(seed, field)}, []
            for p in picks:
                k = _dupkey(p, field)
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(p)
            picks = uniq
        return seed, picks[:size]

    def _mix_tracks(i, size, field=None):
        seed, picks = _build_mix(i, size, field)
        return [seed] + picks

    def _dedup_arg():
        f = request.args.get("dedup") or None
        return f if f in DEDUP_FIELDS else None

    app = Flask(__name__)

    @app.get("/")
    def index():
        return render_template_string(PAGE, count=len(eng.paths), plex=plex_configured)

    @app.get("/api/search")
    def search():
        q = (request.args.get("q") or "").strip().lower()
        if not q:
            return jsonify(results=[])
        field = _dedup_arg()
        out, seen = [], set()
        for i, lab in enumerate(labels_lc):
            if q in lab:
                if field:
                    k = _dupkey(eng.paths[i], field)
                    if k in seen:
                        continue
                    seen.add(k)
                out.append({"i": i, "label": labels[i]})
                if len(out) >= 50:
                    break
        return jsonify(results=out, truncated=len(out) >= 50)

    @app.get("/api/mix")
    def mix():
        try:
            i = int(request.args.get("i", ""))
            size = min(max(int(request.args.get("size", 15)), 1), 100)
        except ValueError:
            return jsonify(error="bad request"), 400
        if not (0 <= i < len(eng.paths)):
            return jsonify(error="unknown seed"), 404
        seed, picks = _build_mix(i, size, _dedup_arg())
        return jsonify(
            seed=labels[i],
            tracks=[_label(eng, p) for p in picks],
        )

    def _seed_index():
        """Parse + bounds-check the ?i / ?size args shared by the export routes."""
        i = int(request.args.get("i", ""))
        size = min(max(int(request.args.get("size", 25)), 1), 200)
        if not (0 <= i < len(eng.paths)):
            raise IndexError
        return i, size

    @app.get("/api/export/m3u")
    def export_m3u():
        try:
            i, size = _seed_index()
        except ValueError:
            return jsonify(error="bad request"), 400
        except IndexError:
            return jsonify(error="unknown seed"), 404
        flavor = request.args.get("flavor", "unc")
        if flavor not in ("local", "unc", "plex"):
            flavor = "unc"
        oneline = lambda s: (s or "").replace("\r", " ").replace("\n", " ")
        lines = ["#EXTM3U"]
        try:
            for p in _mix_tracks(i, size, _dedup_arg()):
                m = eng.meta.get(p, {})
                artist = oneline(m.get("artist") or "?")
                title = oneline(m.get("title") or os.path.basename(p))
                lines.append(f"#EXTINF:-1,{artist} - {title}")
                lines.append(mapper.convert(p, flavor))
        except ValueError as e:                     # a flavor whose root isn't configured
            return jsonify(error=str(e)), 400
        body = "\n".join(lines) + "\n"
        stem = eng.meta.get(eng.paths[i], {}).get("title") or "mix"
        safe = "".join(c for c in stem if c.isalnum() or c in " -_").strip()[:60] or "mix"
        return Response(body, mimetype="audio/x-mpegurl",
                        headers={"Content-Disposition": f'attachment; filename="Attune-{safe}.m3u8"'})

    @app.post("/api/export/plex")
    def export_plex():
        try:
            i, size = _seed_index()
        except ValueError:
            return jsonify(ok=False, error="bad request"), 400
        except IndexError:
            return jsonify(ok=False, error="unknown seed"), 404
        try:
            if "plex" not in plex_holder:
                plex_holder["plex"] = export.plex_from_env(cfg, mapper)
        except SystemExit as e:
            return jsonify(ok=False, error=str(e)), 400
        title = f"Attune — like {labels[i]}"
        try:
            res = plex_holder["plex"].create_playlist(title, _mix_tracks(i, size, _dedup_arg()))
        except Exception as e:                      # network / Plex API failure
            return jsonify(ok=False, error=f"Plex error: {e}"), 502
        # don't leak full local library paths in the HTTP response — basenames only
        if isinstance(res.get("missed"), list):
            res["missed"] = [os.path.basename(p) for p in res["missed"]]
        return jsonify(res)

    return app


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Attune</title>
<style>
  :root {
    --bg: #fafafa; --fg: #1a1a1a; --muted: #6b7280; --line: #e5e7eb;
    --accent: #4f46e5; --card: #ffffff; --hover: #f3f4f6;
  }
  @media (prefers-color-scheme: dark) {
    :root {
      --bg: #0f1115; --fg: #e8e8ea; --muted: #9095a0; --line: #262b36;
      --accent: #818cf8; --card: #161a22; --hover: #1c2230;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font: 15px/1.5 -apple-system, Segoe UI, Roboto, sans-serif;
    background: var(--bg); color: var(--fg);
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 32px 20px 80px; }
  h1 { font-size: 28px; margin: 0 0 4px; letter-spacing: -0.5px; }
  .sub { color: var(--muted); margin: 0 0 24px; font-size: 14px; }
  input[type=search] {
    width: 100%; padding: 12px 14px; font-size: 16px; border-radius: 10px;
    border: 1px solid var(--line); background: var(--card); color: var(--fg);
  }
  input[type=search]:focus { outline: 2px solid var(--accent); border-color: transparent; }
  .opts { display: flex; align-items: center; gap: 12px; margin-top: 10px; font-size: 13px; color: var(--muted); flex-wrap: wrap; }
  .opts .chk { display: flex; align-items: center; gap: 6px; cursor: pointer; }
  .opts select {
    font-size: 13px; padding: 4px 8px; border-radius: 6px;
    border: 1px solid var(--line); background: var(--card); color: var(--fg);
  }
  .opts select:disabled { opacity: .45; }
  .row {
    display: flex; align-items: center; justify-content: space-between; gap: 10px;
    padding: 10px 12px; border-radius: 8px; cursor: pointer; border: 1px solid transparent;
  }
  .row:hover { background: var(--hover); }
  .results { margin-top: 10px; }
  .results .row { border-bottom: 1px solid var(--line); border-radius: 0; }
  .go {
    font-size: 12px; color: var(--accent); border: 1px solid var(--line);
    padding: 3px 10px; border-radius: 20px; white-space: nowrap;
  }
  .mix { margin-top: 28px; }
  .mix h2 { font-size: 15px; color: var(--muted); font-weight: 600; margin: 0 0 8px; }
  .mix .seed { font-size: 18px; font-weight: 600; margin: 0 0 14px; }
  ol { margin: 0; padding: 0; list-style: none; counter-reset: t; }
  ol li {
    counter-increment: t; padding: 9px 12px 9px 40px; position: relative;
    border-bottom: 1px solid var(--line);
  }
  ol li::before {
    content: counter(t); position: absolute; left: 12px; color: var(--muted);
    font-variant-numeric: tabular-nums; font-size: 13px;
  }
  .hint { color: var(--muted); font-size: 13px; }
  .spin { color: var(--muted); font-size: 14px; padding: 12px; }
  .bar { display: flex; gap: 8px; margin: 14px 0 2px; flex-wrap: wrap; }
  .btn {
    font-size: 13px; padding: 7px 14px; border-radius: 8px; border: 1px solid var(--line);
    background: var(--card); color: var(--fg); cursor: pointer;
  }
  .btn:hover { border-color: var(--accent); color: var(--accent); }
  .btn:disabled { opacity: .5; cursor: default; }
  .status { font-size: 13px; color: var(--muted); margin: 8px 0 4px; min-height: 18px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Attune</h1>
  <p class="sub">{{ "{:,}".format(count) }} songs · type a song or artist, click one to hear-alike</p>
  <input type="search" id="q" placeholder="e.g. black dog, beatles, miles davis" autocomplete="off" autofocus>
  <div class="opts">
    <label class="chk"><input type="checkbox" id="dedup"> Filter duplicates</label>
    <select id="dedupField" disabled title="what counts as a duplicate">
      <option value="song">same song (artist + title)</option>
      <option value="title">same title</option>
      <option value="file">same file name</option>
    </select>
  </div>
  <div class="results" id="results"></div>
  <div class="mix" id="mix"></div>
</div>
<script>
const q = document.getElementById('q');
const results = document.getElementById('results');
const mixEl = document.getElementById('mix');
const PLEX = {{ 'true' if plex else 'false' }};
const dedupCk = document.getElementById('dedup');
const dedupSel = document.getElementById('dedupField');
let timer = null, seq = 0;
let cur = { i: null, size: 15 };

function dedupParam() { return dedupCk.checked ? '&dedup=' + dedupSel.value : ''; }

function refresh() {
  const term = q.value.trim();
  if (term) runSearch(term);
  if (cur.i !== null) makeMix(cur.i);
}
dedupCk.addEventListener('change', () => { dedupSel.disabled = !dedupCk.checked; refresh(); });
dedupSel.addEventListener('change', refresh);

q.addEventListener('input', () => {
  clearTimeout(timer);
  const term = q.value.trim();
  if (!term) { results.innerHTML = ''; return; }
  timer = setTimeout(() => runSearch(term), 180);
});

async function runSearch(term) {
  const mine = ++seq;
  const r = await fetch('/api/search?q=' + encodeURIComponent(term) + dedupParam());
  if (mine !== seq) return;               // a newer keystroke already fired
  const data = await r.json();
  if (!data.results.length) { results.innerHTML = '<div class="spin">No matches.</div>'; return; }
  results.innerHTML = data.results.map(x =>
    `<div class="row" data-i="${x.i}"><span>${esc(x.label)}</span><span class="go">make mix →</span></div>`
  ).join('') + (data.truncated ? '<div class="hint" style="padding:8px 12px">Showing first 50 — narrow your search.</div>' : '');
  results.querySelectorAll('.row').forEach(el =>
    el.addEventListener('click', () => makeMix(el.dataset.i, el.querySelector('span').textContent)));
}

async function makeMix(i, label) {
  cur.i = i;
  mixEl.innerHTML = '<div class="spin">Building a playlist that sounds like it…</div>';
  const r = await fetch('/api/mix?i=' + i + '&size=' + cur.size + dedupParam());
  const data = await r.json();
  if (data.error || !data.tracks || !data.tracks.length) {
    mixEl.innerHTML = '<div class="spin">Couldn\\'t build a mix for that one.</div>'; return;
  }
  let bar = '<button class="btn" id="dlM3u">⬇ Download .m3u8</button>';
  if (PLEX) bar += '<button class="btn" id="toPlex">＋ Send to Plex</button>';
  mixEl.innerHTML = '<div class="mix">'
    + '<h2>Sounds like</h2>'
    + '<p class="seed">' + esc(data.seed) + '</p>'
    + '<div class="bar">' + bar + '</div>'
    + '<div class="status" id="xstatus"></div>'
    + '<ol>' + data.tracks.map(t => '<li>' + esc(t) + '</li>').join('') + '</ol></div>';
  document.getElementById('dlM3u').addEventListener('click', exportM3u);
  if (PLEX) document.getElementById('toPlex').addEventListener('click', sendPlex);
  mixEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function exportM3u() {
  if (cur.i === null) return;
  window.location = '/api/export/m3u?i=' + cur.i + '&size=' + cur.size + dedupParam();
}

async function sendPlex() {
  if (cur.i === null) return;
  const btn = document.getElementById('toPlex'), st = document.getElementById('xstatus');
  btn.disabled = true;
  st.textContent = 'Sending to Plex (indexing the library on first use — a few seconds)…';
  try {
    const r = await fetch('/api/export/plex?i=' + cur.i + '&size=' + cur.size + dedupParam(), { method: 'POST' });
    const d = await r.json();
    st.textContent = d.ok
      ? '✓ Added to Plex — ' + d.matched + ' tracks'
        + (d.missed && d.missed.length ? ' (' + d.missed.length + ' not in Plex)' : '') + '.'
      : '✗ ' + (d.error || 'Plex export failed');
  } catch (e) {
    st.textContent = '✗ ' + e;
  } finally {
    btn.disabled = false;
  }
}

function esc(s) {
  return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
</script>
</body>
</html>"""


def main():
    ap = argparse.ArgumentParser(description="Attune web front-end for the V2 hybrid engine.")
    ap.add_argument("--db", required=True,
                    help="path to an analyzed+CLAP-embedded mixer SQLite DB (see scan.py / embed.py)")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1; use 0.0.0.0 for LAN)")
    ap.add_argument("--port", type=int, default=8778, help="port (default: 8778)")
    args = ap.parse_args()
    app = create_app(args.db)
    print(f"Serving on http://{args.host}:{args.port}")
    app.run(host=args.host, port=args.port, debug=False)


if __name__ == "__main__":
    main()
