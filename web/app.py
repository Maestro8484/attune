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
import mimetypes
import os
import sqlite3
import threading

from flask import (Flask, Response, jsonify, redirect, render_template_string,
                   request, send_file)

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(os.path.dirname(HERE), "src")

# weights exposed as sliders in the UI (a subset of HybridEngine's full weight dict --
# "key" stays fixed at its configured/default value, not user-tunable here). V2-only.
SLIDER_KEYS = ("clap", "lib", "genre", "bpm", "era")

# MusicIP-native controls exposed instead of the weight sliders when --engine musicip.
# Defaults match musicip_engine.MIX_DEFAULTS (style=40, variety=5).
MUSICIP_STYLE_DEFAULT = 40
MUSICIP_VARIETY_DEFAULT = 5

# The style dial is NOT 0-100. Measured against the live engine (extracted/TEACHER_MECHANICS.md
# B.6): the eligible pool is flat (~14,810 tracks) from style 0 to ~400 while the ORDERING keeps
# changing, then a hard threshold bites between 845 and 846 and the pool collapses -- at style
# >= 950 the only survivors are the seed's own artist. Clamping to 100 discarded ~88% of the
# usable range. 845 is the last value that still returns a workable pool.
MUSICIP_STYLE_MAX = 845
MUSICIP_VARIETY_MAX = 9

# extensions the library actually contains (mp3/flac); fall back to mimetypes.guess_type
# for anything else so /audio still serves a sane Content-Type.
_AUDIO_MIME_OVERRIDES = {
    ".mp3": "audio/mpeg", ".flac": "audio/flac", ".m4a": "audio/mp4",
    ".ogg": "audio/ogg", ".wav": "audio/wav", ".aac": "audio/aac",
}


def _audio_mimetype(path):
    ext = os.path.splitext(path)[1].lower()
    if ext in _AUDIO_MIME_OVERRIDES:
        return _AUDIO_MIME_OVERRIDES[ext]
    guessed, _ = mimetypes.guess_type(path)
    return guessed or "application/octet-stream"


def _load_hybrid():
    """Import hybrid.py by path so this works whether or not attune is installed."""
    spec = importlib.util.spec_from_file_location("hybrid", os.path.join(SRC, "hybrid.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_engine_iface():
    """Import engine.py (src/engine.py) by path -- the common Engine/V2Engine/MusicIPAdapter
    interface search() and mix() route through, so the shell doesn't care which concrete
    engine produced a result (see engine.py's module docstring)."""
    import sys
    spec = importlib.util.spec_from_file_location("engine", os.path.join(SRC, "engine.py"))
    mod = importlib.util.module_from_spec(spec)
    sys.modules["engine"] = mod    # engine.py's @dataclass needs its module registered
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


def _load_studio():
    path = os.path.join(HERE, "studio.py")
    spec = importlib.util.spec_from_file_location("attune_studio", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _load_config():
    """Import config.py (src/config.py) by path — the one settings store every entry
    point (web, desktop, analyzer) reads. See config.py's module docstring."""
    spec = importlib.util.spec_from_file_location("attune_config", os.path.join(SRC, "config.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def create_app(db_path, engine_name="musicip", musicip_url="http://localhost:10002",
               playlist_dir=None):
    _check_db(db_path)
    hybrid = _load_hybrid()
    print(f"Loading library from {db_path} ...")
    # HybridEngine loads regardless of --engine: playback (/audio), .m3u8/Plex export, and
    # dedup all key off of ITS pool indices (eng.paths/eng.meta) -- MusicIPAdapter resolves
    # its own results into that same pool, so meta/playback/export don't have to know which
    # engine picked the tracks. hybrid.py never imports torch (it only reads precomputed CLAP
    # vectors already in the DB), so this stays torch-free either way.
    eng = hybrid.HybridEngine(db_path)
    # precompute a lowercased label per pool index once; search is then a cheap scan
    labels = [_label(eng, p) for p in eng.paths]
    labels_lc = [s.lower() for s in labels]
    print(f"Ready — {len(eng.paths):,} songs in the mixable pool.")

    # search + mix are routed through the common engine interface (src/engine.py) so the
    # concrete engine (our CLAP/librosa hybrid, or a live MusicIP Mixer) is swappable.
    eng_iface = _load_engine_iface()
    if engine_name == "musicip":
        active = eng_iface.MusicIPAdapter(eng.paths, url=musicip_url, log=print)
        active.attach_meta(eng.meta)
        if not active.mip.alive():
            raise SystemExit(
                f"--engine musicip requires a live MusicIP Mixer at {musicip_url}; "
                f"it did not respond. Start MusicIP Mixer's web API, or run with --engine v2.")
        print(f"MusicIP engine ready at {musicip_url}.")
    elif engine_name == "learned":
        # the distilled-metric head (onnxruntime, still torch-free). SELECTABLE ONLY --
        # per LAW 1 it cannot become the default before a blind ear test (eval/abtest.py).
        active = eng_iface.LearnedEngine(db_path, hybrid_engine=eng, log=print)
    else:
        active = eng_iface.V2Engine(hybrid_engine=eng)
    is_musicip = engine_name == "musicip"

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

    def _build_mix(i, size, field=None, variety=False, flow=False):
        """Return (seed_path, picks). With a dedup field, over-fetch then collapse
        same-key tracks (and any that duplicate the seed), so you still get `size`.

        variety=True: over-fetch a much larger candidate pool (up to 200; a 4x
        over-fetch like dedup uses isn't enough headroom for MMR to substitute
        genuinely different picks -- verified empirically) and pick the final
        `size` via eng.mmr() (MMR diversity re-ranking, lambda_=0.3 favors
        diversity over plain relevance) instead of plain top-N, trusting mix()'s
        relevance order (mmr's query=None mode). Composes with dedup: the
        field-collapse above runs first, so mmr only ever sees already-deduped
        candidates.

        flow=True: reorder the final `size` picks via eng.order_by_flow() (greedy
        nearest-neighbour on CLAP vectors) for smoother track-to-track transitions.
        Same set of tracks, different order -- applied last so it doesn't fight mmr.
        """
        seed = eng.paths[i]
        if variety:
            pool_size = 200
        elif field:
            pool_size = min(size * 4, 200)
        else:
            pool_size = size
        picks = eng.mix(seed, size=pool_size) or []
        if field:
            seen, uniq = {_dupkey(seed, field)}, []
            for p in picks:
                k = _dupkey(p, field)
                if k in seen:
                    continue
                seen.add(k)
                uniq.append(p)
            picks = uniq
        if variety:
            picks = eng.mmr(picks, k=size, lambda_=0.3)
        else:
            picks = picks[:size]
        if flow:
            picks = eng.order_by_flow(picks)
        return seed, picks

    def _active_mix_indices(i, size, field=None):
        """Pool indices of the mix picks for seed index `i` via the ACTIVE engine, applying
        the same controls /api/mix parses from the request. Does NOT include the seed.
        Raises ValueError on bad control args (caller returns 400).

        This is the single source of truth both /api/mix and the export routes build their
        track list from, so an exported playlist always equals the mix the user is viewing
        under whichever engine is active (musicip or v2)."""
        if is_musicip:
            style = min(max(int(request.args.get("style", MUSICIP_STYLE_DEFAULT)), 0), MUSICIP_STYLE_MAX)
            variety = min(max(int(request.args.get("variety", MUSICIP_VARIETY_DEFAULT)), 0), MUSICIP_VARIETY_MAX)
            pool_size = min(size * 4, 200) if field else size
            seed_ref = eng_iface.Ref(pool_i=i, label=labels[i])
            refs = active.similar(seed_ref, size=pool_size, style=style, variety=variety)
            if field:
                seen, uniq = {_dupkey(eng.paths[i], field)}, []
                for r in refs:
                    k = _dupkey(eng.paths[r.pool_i], field)
                    if k in seen:
                        continue
                    seen.add(k)
                    uniq.append(r)
                refs = uniq
            return [r.pool_i for r in refs[:size]]
        overrides = _weight_overrides()
        variety = (request.args.get("variety") or "").lower() in ("1", "true", "on")
        flow = (request.args.get("flow") or "").lower() in ("1", "true", "on")
        _seed, picks = _with_weights(
            overrides, lambda: _build_mix(i, size, field, variety, flow))
        return [eng.idx[p] for p in picks]

    def _active_mix_tracks(i, size, field=None):
        """[seed_path] + pick paths, picks chosen by the ACTIVE engine (the same selection
        /api/mix returns). Export routes build their track list from this so the exported
        m3u/Plex playlist matches the mix on screen regardless of engine."""
        picks_i = _active_mix_indices(i, size, field)
        return [eng.paths[i]] + [eng.paths[pi] for pi in picks_i]

    def _dedup_arg():
        f = request.args.get("dedup") or None
        return f if f in DEDUP_FIELDS else None

    # eng.w is process-wide shared state that mix()/_score() read directly; HybridEngine
    # has no per-call weights param, so per-request slider overrides are applied by
    # temporarily patching eng.w, running the mix, then restoring it -- serialized via a
    # lock so two overlapping requests can't see each other's half-applied weights.
    weights_lock = threading.Lock()

    def _weight_overrides():
        out = {}
        for k in SLIDER_KEYS:
            v = request.args.get(k)
            if v is None:
                continue
            try:
                out[k] = max(0.0, min(float(v), 5.0))
            except ValueError:
                pass
        return out

    def _with_weights(overrides, fn):
        if not overrides:
            return fn()
        with weights_lock:
            saved = dict(eng.w)
            eng.w.update(overrides)
            try:
                return fn()
            finally:
                eng.w = saved

    app = Flask(__name__)

    @app.get("/")
    def index():
        return redirect("/studio")

    @app.get("/classic")
    def classic():
        return render_template_string(
            PAGE, count=len(eng.paths), plex=plex_configured, engine=engine_name,
            weights={k: eng.w.get(k, 0.0) for k in SLIDER_KEYS},
            musicip_style=MUSICIP_STYLE_DEFAULT, musicip_variety=MUSICIP_VARIETY_DEFAULT,
            musicip_style_max=MUSICIP_STYLE_MAX)

    @app.get("/api/search")
    def search():
        q = (request.args.get("q") or "").strip().lower()
        if not q:
            return jsonify(results=[])
        field = _dedup_arg()
        # with a dedup field, over-fetch generously so 50 UNIQUE hits can still surface
        # (matches the old scan-till-50-unique behaviour) instead of capping pre-dedup.
        limit = 5000 if field else 50
        out, seen = [], set()
        for r in active.search(q, limit=limit):
            if field:
                k = _dupkey(eng.paths[r.pool_i], field)
                if k in seen:
                    continue
                seen.add(k)
            out.append({"i": r.pool_i, "label": r.label})
            if len(out) >= 50:
                break
        return jsonify(results=out, truncated=len(out) >= 50)

    @app.get("/api/mix")
    def mix():
        try:
            i = int(request.args.get("i", ""))
            size = min(max(int(request.args.get("size", 100)), 20), 150)
        except ValueError:
            return jsonify(error="bad request"), 400
        if not (0 <= i < len(eng.paths)):
            return jsonify(error="unknown seed"), 404
        field = _dedup_arg()

        try:
            picks_i = _active_mix_indices(i, size, field)
        except ValueError:
            return jsonify(error="bad request"), 400
        tracks = [{"i": pi, "label": labels[pi]} for pi in picks_i]

        if is_musicip:
            style = min(max(int(request.args.get("style", MUSICIP_STYLE_DEFAULT)), 0), MUSICIP_STYLE_MAX)
            variety = min(max(int(request.args.get("variety", MUSICIP_VARIETY_DEFAULT)), 0), MUSICIP_VARIETY_MAX)
            return jsonify(
                seed=labels[i], seed_i=i, tracks=tracks, style=style, variety=variety)

        overrides = _weight_overrides()
        effective = {k: overrides.get(k, eng.w.get(k)) for k in SLIDER_KEYS}
        return jsonify(seed=labels[i], seed_i=i, tracks=tracks, weights=effective)

    @app.post("/api/refine")
    def refine():
        """Thumbs up/down relevance feedback: Rocchio-refine the seed's CLAP vector
        toward liked and away from disliked pool-indices, then re-rank by cosine to
        that vector. Indices only -- never accepts a raw path from the client."""
        if "refine" not in active.capabilities:
            return jsonify(error=f"refine not supported by the '{active.name}' engine"), 501
        data = request.get_json(silent=True) or {}
        try:
            i = int(data.get("i"))
            size = min(max(int(data.get("size", 100)), 20), 150)
        except (TypeError, ValueError):
            return jsonify(error="bad request"), 400
        if not (0 <= i < len(eng.paths)):
            return jsonify(error="unknown seed"), 404

        def _idx_list(key):
            vals = data.get(key) or []
            if not isinstance(vals, list):
                raise ValueError(f"{key} must be a list")
            out = []
            for x in vals:
                xi = int(x)
                if not (0 <= xi < len(eng.paths)):
                    raise ValueError(f"{key} index out of range: {xi}")
                out.append(xi)
            return out

        try:
            liked = _idx_list("liked")
            disliked = _idx_list("disliked")
        except (TypeError, ValueError) as e:
            return jsonify(error=str(e)), 400

        try:
            q = eng.refine(i, liked_idx=liked, disliked_idx=disliked)
            picks = eng.mix_from_vector(q, size=size, exclude=[i] + liked + disliked)
        except ValueError as e:
            return jsonify(error=str(e)), 400

        return jsonify(
            seed=labels[i],
            seed_i=i,
            tracks=[{"i": eng.idx[p], "label": _label(eng, p)} for p in picks],
        )

    @app.get("/api/explain")
    def explain():
        """Per-term score breakdown for one (seed, candidate) pair -- backs the
        "why?" affordance per mix row."""
        if "explain" not in active.capabilities:
            return jsonify(error=f"explain not supported by the '{active.name}' engine"), 501
        try:
            si = int(request.args.get("seed", ""))
            ci = int(request.args.get("cand", ""))
        except ValueError:
            return jsonify(error="bad request"), 400
        if not (0 <= si < len(eng.paths)) or not (0 <= ci < len(eng.paths)):
            return jsonify(error="unknown track"), 404
        try:
            comp = eng.explain(si, ci)
        except ValueError as e:
            return jsonify(error=str(e)), 400
        return jsonify(seed=labels[si], cand=labels[ci], **comp)

    @app.get("/audio")
    def audio():
        """Range-streams the audio file for a pool index -- index into eng.paths
        ONLY, never a raw client-supplied path."""
        try:
            i = int(request.args.get("i", ""))
        except ValueError:
            return jsonify(error="bad request"), 400
        if not (0 <= i < len(eng.paths)):
            return jsonify(error="unknown track"), 404
        path = eng.paths[i]
        if not os.path.isfile(path):
            return jsonify(error="audio file missing on disk"), 404
        return send_file(path, mimetype=_audio_mimetype(path), conditional=True)

    def _seed_index():
        """Parse + bounds-check the ?i / ?size args shared by the export routes. Mix length
        is capped at 150 to match /api/mix (a playlist longer than that is the 'too long'
        case we deliberately don't generate)."""
        i = int(request.args.get("i", ""))
        size = min(max(int(request.args.get("size", 100)), 1), 150)
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
        try:
            tracks = _active_mix_tracks(i, size, _dedup_arg())
        except ValueError:
            return jsonify(error="bad request"), 400
        oneline = lambda s: (s or "").replace("\r", " ").replace("\n", " ")
        lines = ["#EXTM3U"]
        try:
            for p in tracks:
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
            tracks = _active_mix_tracks(i, size, _dedup_arg())
        except ValueError:
            return jsonify(ok=False, error="bad request"), 400
        try:
            if "plex" not in plex_holder:
                plex_holder["plex"] = export.plex_from_env(cfg, mapper)
        except SystemExit as e:
            return jsonify(ok=False, error=str(e)), 400
        title = f"Attune — like {labels[i]}"
        try:
            res = plex_holder["plex"].create_playlist(title, tracks)
        except Exception as e:                      # network / Plex API failure
            return jsonify(ok=False, error=f"Plex error: {e}"), 502
        # don't leak full local library paths in the HTTP response — basenames only
        if isinstance(res.get("missed"), list):
            res["missed"] = [os.path.basename(p) for p in res["missed"]]
        return jsonify(res)

    # ---- Settings: the one config source of truth (src/config.py). GET hands the
    # client its server-side settings; POST merges a partial update and reports which
    # keys need a process restart to take effect (honestly, instead of pretending).
    cfgmod = _load_config()

    @app.get("/api/settings")
    def get_settings():
        s = cfgmod.load()
        return jsonify(settings=s, path=cfgmod.settings_path(),
                       restart_keys=sorted(cfgmod.RESTART_KEYS))

    @app.post("/api/settings")
    def post_settings():
        patch = request.get_json(silent=True)
        if not isinstance(patch, dict):
            return jsonify(ok=False, error="expected a JSON object"), 400
        patch.pop("settings_version", None)          # managed by config.py, not the client
        unknown = sorted(k for k in patch if k not in cfgmod.DEFAULTS)
        if unknown:
            return jsonify(ok=False, error=f"unknown settings: {', '.join(unknown)}"), 400
        s, needs_restart = cfgmod.update(patch)
        return jsonify(ok=True, settings=s, needs_restart=needs_restart)

    # ---- Folder picker: a read-only local directory browser for the first-run wizard
    # and Preferences -> Library. Attune is local-first — the server already reads audio
    # from arbitrary on-disk paths and streams files by absolute path — so listing folder
    # NAMES adds no new exposure. Dirs only, never file contents. This one endpoint gives a
    # real folder picker in BOTH the browser and the desktop (pywebview) window, with no
    # native-dialog bridge, and it returns absolute paths the analyzer can actually use.
    @app.get("/api/fs/dirs")
    def fs_dirs():
        raw = (request.args.get("path") or "").strip()
        # Empty path = the top level. On Windows that's the set of existing drive roots;
        # on POSIX it's "/". Choosing this top level itself is disallowed client-side.
        if not raw:
            if os.name == "nt":
                drives = [{"name": f"{c}:\\", "path": f"{c}:\\"}
                          for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                          if os.path.exists(f"{c}:\\")]
                return jsonify(path="", parent=None, dirs=drives)
            raw = "/"
        path = os.path.abspath(raw)
        if not os.path.isdir(path):
            return jsonify(error=f"not a folder: {path}"), 404
        dirs = []
        try:
            with os.scandir(path) as it:
                for e in it:
                    try:
                        if e.is_dir(follow_symlinks=False):
                            dirs.append({"name": e.name, "path": e.path})
                    except OSError:
                        pass          # unreadable entry — skip, don't fail the whole listing
        except OSError as ex:
            return jsonify(error=f"cannot read folder: {ex}"), 403
        dirs.sort(key=lambda d: d["name"].lower())
        parent = os.path.dirname(path)
        # A drive root ("C:\") is its own dirname on Windows — send the user back to the
        # drive list ("") rather than looping; "/" on POSIX has no parent (None).
        if parent == path:
            parent = "" if os.name == "nt" else None
        return jsonify(path=path, parent=parent, dirs=dirs)

    # ---- Attune Studio: the MusicIP-shaped desktop view. Adds library facets, the
    # playlist-folder browser, album art and save-to-folder export. It reuses the routes
    # above rather than reimplementing them; _active_mix_tracks is handed over so an
    # exported playlist always equals the mix on screen.
    lib = _load_studio().register(app, {
        "db_path": db_path,
        "eng": eng,
        "labels": labels,
        "mapper": mapper,
        "playlist_dir": playlist_dir,
        "engine_name": engine_name,
        "plex_configured": plex_configured,
        "active_mix_tracks": _active_mix_tracks,
    })

    # ---- user data: ratings / play counts / tag editor / ReplayGain (userdata.py).
    # A tag edit must also refresh the precomputed search labels here and inside the
    # active V2 engine, or search would keep matching the OLD artist/title.
    def _on_tags_changed(i):
        labels[i] = _label(eng, eng.paths[i])
        labels_lc[i] = labels[i].lower()
        if hasattr(active, "_labels"):
            active._labels[i] = labels[i]
            active._labels_lc[i] = labels_lc[i]

    ud_spec = importlib.util.spec_from_file_location(
        "attune_userdata", os.path.join(HERE, "userdata.py"))
    userdata = importlib.util.module_from_spec(ud_spec)
    ud_spec.loader.exec_module(userdata)
    userdata.register(app, {"db_path": db_path, "eng": eng, "lib": lib,
                            "on_tags_changed": _on_tags_changed})

    # ---- auto-playlists: user-defined smart-playlist rules (smartlists.py). Registered
    # AFTER userdata so lib already carries rating/plays/loved/date_added columns the
    # rule evaluator reads.
    sl_spec = importlib.util.spec_from_file_location(
        "attune_smartlists", os.path.join(HERE, "smartlists.py"))
    smartlists = importlib.util.module_from_spec(sl_spec)
    sl_spec.loader.exec_module(smartlists)
    smartlists.register(app, {"db_path": db_path, "lib": lib})

    # ---- rescan: the existing incremental pipeline behind a button (scanjob.py)
    sj_spec = importlib.util.spec_from_file_location(
        "attune_scanjob", os.path.join(HERE, "scanjob.py"))
    scanjob = importlib.util.module_from_spec(sj_spec)
    sj_spec.loader.exec_module(scanjob)
    scan_job = scanjob.register(app, {"db_path": db_path, "load_settings": cfgmod.load})

    # ---- auto-scan: honors scan_on_launch (previously dead) and, if `watchdog` is
    # installed, live-watches library_folders so new/changed audio triggers the same
    # incremental pipeline without a manual Rescan click (autoscan.py).
    as_spec = importlib.util.spec_from_file_location(
        "attune_autoscan", os.path.join(HERE, "autoscan.py"))
    autoscan = importlib.util.module_from_spec(as_spec)
    as_spec.loader.exec_module(autoscan)
    autoscan.register(app, {"job": scan_job, "load_settings": cfgmod.load})

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
  details.weights { margin-top: 10px; font-size: 13px; color: var(--muted); }
  details.weights summary { cursor: pointer; user-select: none; }
  .wsliders { display: grid; grid-template-columns: 56px 1fr 34px; gap: 6px 10px; align-items: center; margin-top: 10px; }
  .wsliders label { font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
  .wsliders input[type=range] { width: 100%; }
  .wsliders .wval { font-size: 12px; font-variant-numeric: tabular-nums; text-align: right; }
  .player-bar {
    position: sticky; top: 0; z-index: 5; display: none; gap: 10px; align-items: center;
    background: var(--card); border: 1px solid var(--line); border-radius: 10px;
    padding: 8px 12px; margin: -8px 0 16px; box-shadow: 0 2px 8px rgba(0,0,0,.06);
  }
  .player-bar.active { display: flex; }
  .player-bar audio { flex: 1; min-width: 0; height: 32px; }
  .player-bar .nowplaying { font-size: 12px; color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; max-width: 40%; }
  ol li {
    display: flex; align-items: center; gap: 8px; flex-wrap: wrap;
  }
  ol li .tlabel { flex: 1; min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .tbtn {
    font-size: 12px; line-height: 1; padding: 5px 7px; border-radius: 6px; border: 1px solid var(--line);
    background: var(--card); color: var(--fg); cursor: pointer;
  }
  .tbtn:hover { border-color: var(--accent); color: var(--accent); }
  .tbtn.on.up { color: #16a34a; border-color: #16a34a; }
  .tbtn.on.down { color: #dc2626; border-color: #dc2626; }
  .why-panel {
    flex-basis: 100%; font-size: 12px; color: var(--muted); background: var(--hover);
    border-radius: 6px; padding: 6px 10px; margin-top: 2px;
  }
  .why-panel code { font-variant-numeric: tabular-nums; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Attune</h1>
  <p class="sub">{{ "{:,}".format(count) }} songs · type a song or artist, click one to hear-alike</p>
  <div class="player-bar" id="playerBar">
    <audio id="player" controls preload="none"></audio>
    <span class="nowplaying" id="nowPlaying"></span>
  </div>
  <input type="search" id="q" placeholder="e.g. black dog, beatles, miles davis" autocomplete="off" autofocus>
  <div class="opts">
    <label class="chk"><input type="checkbox" id="dedup"> Filter duplicates</label>
    <select id="dedupField" disabled title="what counts as a duplicate">
      <option value="song">same song (artist + title)</option>
      <option value="title">same title</option>
      <option value="file">same file name</option>
    </select>
    {% if engine != 'musicip' %}
    <label class="chk" title="Pick a less samey set via MMR diversity re-ranking">
      <input type="checkbox" id="variety"> Variety
    </label>
    <label class="chk" title="Reorder the mix for gentler track-to-track transitions">
      <input type="checkbox" id="flow"> Smooth flow
    </label>
    {% endif %}
  </div>
  {% if engine == 'musicip' %}
  <details class="weights">
    <summary>MusicIP mix controls</summary>
    <div class="wsliders">
      <label for="mip_style">style</label>
      <input type="range" id="mip_style" min="0" max="{{ musicip_style_max }}" step="1" value="{{ musicip_style }}">
      <span class="wval" id="mip_style_v">{{ musicip_style }}</span>
      <label for="mip_variety">variety</label>
      <input type="range" id="mip_variety" min="0" max="9" step="1" value="{{ musicip_variety }}">
      <span class="wval" id="mip_variety_v">{{ musicip_variety }}</span>
    </div>
  </details>
  {% else %}
  <details class="weights">
    <summary>Mix weights</summary>
    <div class="wsliders">
      <label for="w_clap">clap</label>
      <input type="range" id="w_clap" min="0" max="2" step="0.05" value="{{ weights['clap'] }}">
      <span class="wval" id="w_clap_v">{{ weights['clap'] }}</span>
      <label for="w_lib">lib</label>
      <input type="range" id="w_lib" min="0" max="2" step="0.05" value="{{ weights['lib'] }}">
      <span class="wval" id="w_lib_v">{{ weights['lib'] }}</span>
      <label for="w_genre">genre</label>
      <input type="range" id="w_genre" min="0" max="2" step="0.05" value="{{ weights['genre'] }}">
      <span class="wval" id="w_genre_v">{{ weights['genre'] }}</span>
      <label for="w_bpm">bpm</label>
      <input type="range" id="w_bpm" min="0" max="2" step="0.05" value="{{ weights['bpm'] }}">
      <span class="wval" id="w_bpm_v">{{ weights['bpm'] }}</span>
      <label for="w_era">era</label>
      <input type="range" id="w_era" min="0" max="2" step="0.05" value="{{ weights['era'] }}">
      <span class="wval" id="w_era_v">{{ weights['era'] }}</span>
    </div>
  </details>
  {% endif %}
  <div class="results" id="results"></div>
  <div class="mix" id="mix"></div>
</div>
<script>
const q = document.getElementById('q');
const results = document.getElementById('results');
const mixEl = document.getElementById('mix');
const PLEX = {{ 'true' if plex else 'false' }};
const ENGINE = {{ (engine == 'musicip') | tojson }};
const dedupCk = document.getElementById('dedup');
const dedupSel = document.getElementById('dedupField');
const varietyCk = document.getElementById('variety');
const flowCk = document.getElementById('flow');
const player = document.getElementById('player');
const playerBar = document.getElementById('playerBar');
const nowPlaying = document.getElementById('nowPlaying');
const SLIDER_KEYS = ['clap', 'lib', 'genre', 'bpm', 'era'];
let timer = null, seq = 0;
let cur = { i: null, size: 15, liked: new Set(), disliked: new Set() };

function dedupParam() { return dedupCk.checked ? '&dedup=' + dedupSel.value : ''; }

function varietyParam() { return (varietyCk && varietyCk.checked) ? '&variety=1' : ''; }
function flowParam() { return (flowCk && flowCk.checked) ? '&flow=1' : ''; }

function weightParams() {
  return SLIDER_KEYS.map(k => '&' + k + '=' + document.getElementById('w_' + k).value).join('');
}

function musicipParams() {
  const style = document.getElementById('mip_style').value;
  const variety = document.getElementById('mip_variety').value;
  return '&style=' + style + '&variety=' + variety;
}

if (ENGINE) {
  ['mip_style', 'mip_variety'].forEach(id => {
    const el = document.getElementById(id);
    const out = document.getElementById(id + '_v');
    el.addEventListener('input', () => { out.textContent = el.value; });
    el.addEventListener('change', () => { if (cur.i !== null) makeMix(cur.i); });
  });
} else {
  SLIDER_KEYS.forEach(k => {
    const el = document.getElementById('w_' + k);
    const out = document.getElementById('w_' + k + '_v');
    el.addEventListener('input', () => { out.textContent = el.value; });
    el.addEventListener('change', () => { if (cur.i !== null) makeMix(cur.i); });
  });
}

function refresh() {
  const term = q.value.trim();
  if (term) runSearch(term);
  if (cur.i !== null) makeMix(cur.i);
}
dedupCk.addEventListener('change', () => { dedupSel.disabled = !dedupCk.checked; refresh(); });
dedupSel.addEventListener('change', refresh);
if (varietyCk) varietyCk.addEventListener('change', refresh);
if (flowCk) flowCk.addEventListener('change', refresh);

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
  cur.liked = new Set();
  cur.disliked = new Set();
  mixEl.innerHTML = '<div class="spin">Building a playlist that sounds like it…</div>';
  const extra = ENGINE ? musicipParams() : (weightParams() + varietyParam() + flowParam());
  const r = await fetch('/api/mix?i=' + i + '&size=' + cur.size + dedupParam() + extra);
  const data = await r.json();
  if (data.error || !data.tracks || !data.tracks.length) {
    mixEl.innerHTML = '<div class="spin">Couldn\\'t build a mix for that one.</div>'; return;
  }
  renderMix(data.seed, data.tracks);
}

function renderMix(seedLabel, tracks) {
  let bar = '<button class="btn" id="dlM3u">⬇ Download .m3u8</button>';
  if (PLEX) bar += '<button class="btn" id="toPlex">＋ Send to Plex</button>';
  mixEl.innerHTML = '<div class="mix">'
    + '<h2>Sounds like</h2>'
    + '<p class="seed">' + esc(seedLabel) + '</p>'
    + '<div class="bar">' + bar + '</div>'
    + '<div class="status" id="xstatus"></div>'
    + '<ol>' + tracks.map(renderTrackRow).join('') + '</ol></div>';
  document.getElementById('dlM3u').addEventListener('click', exportM3u);
  if (PLEX) document.getElementById('toPlex').addEventListener('click', sendPlex);
  wireTrackRows();
  mixEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
}

function renderTrackRow(t) {
  if (ENGINE) {
    // musicip engine: no thumbs/refine or why/explain -- those need our CLAP vectors.
    return `<li data-i="${t.i}">`
      + `<button class="tbtn play" title="Play">▶</button>`
      + `<span class="tlabel">${esc(t.label)}</span>`
      + `</li>`;
  }
  const up = cur.liked.has(t.i) ? 'on up' : '';
  const down = cur.disliked.has(t.i) ? 'on down' : '';
  return `<li data-i="${t.i}">`
    + `<button class="tbtn play" title="Play">▶</button>`
    + `<span class="tlabel">${esc(t.label)}</span>`
    + `<button class="tbtn up ${up}" title="More like this">👍</button>`
    + `<button class="tbtn down ${down}" title="Less like this">👎</button>`
    + `<button class="tbtn why" title="Why this track?">?</button>`
    + `<div class="why-panel" style="display:none"></div>`
    + `</li>`;
}

function wireTrackRows() {
  mixEl.querySelectorAll('ol li').forEach(li => {
    const i = parseInt(li.dataset.i, 10);
    li.querySelector('.play').addEventListener('click', () => playTrack(i, li.querySelector('.tlabel').textContent));
    if (ENGINE) return;    // no refine/explain controls to wire in musicip mode
    li.querySelector('.up').addEventListener('click', () => rate(i, 'liked'));
    li.querySelector('.down').addEventListener('click', () => rate(i, 'disliked'));
    li.querySelector('.why').addEventListener('click', () => toggleWhy(i, li));
  });
}

function playTrack(i, label) {
  player.src = '/audio?i=' + i;
  playerBar.classList.add('active');
  nowPlaying.textContent = label;
  player.play().catch(() => {});
}

async function rate(i, kind) {
  if (cur.i === null) return;
  const other = kind === 'liked' ? cur.disliked : cur.liked;
  other.delete(i);
  const set = kind === 'liked' ? cur.liked : cur.disliked;
  if (set.has(i)) set.delete(i); else set.add(i);

  const st = document.getElementById('xstatus');
  if (st) st.textContent = 'Refining…';
  try {
    const r = await fetch('/api/refine', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        i: cur.i, size: cur.size,
        liked: Array.from(cur.liked), disliked: Array.from(cur.disliked),
      }),
    });
    const data = await r.json();
    if (data.error || !data.tracks) { if (st) st.textContent = '✗ ' + (data.error || 'refine failed'); return; }
    renderMix(data.seed, data.tracks);
  } catch (e) {
    if (st) st.textContent = '✗ ' + e;
  }
}

async function toggleWhy(i, li) {
  const panel = li.querySelector('.why-panel');
  if (panel.style.display !== 'none') { panel.style.display = 'none'; return; }
  panel.style.display = 'block';
  panel.textContent = 'Loading…';
  try {
    const r = await fetch('/api/explain?seed=' + cur.i + '&cand=' + i);
    const d = await r.json();
    if (d.error) { panel.textContent = '✗ ' + d.error; return; }
    const parts = ['clap', 'lib', 'genre', 'bpm', 'key', 'era']
      .map(k => k + '=<code>' + d[k].toFixed(3) + '</code>').join('  ');
    panel.innerHTML = parts + '  &nbsp;→&nbsp; total=<code>' + d.total.toFixed(3) + '</code>';
  } catch (e) {
    panel.textContent = '✗ ' + e;
  }
}

function exportM3u() {
  if (cur.i === null) return;
  // the export routes rebuild the mix from these args -- omit them and you export a
  // DIFFERENT mix than the one on screen.
  const extra = ENGINE ? musicipParams() : (weightParams() + varietyParam() + flowParam());
  window.location = '/api/export/m3u?i=' + cur.i + '&size=' + cur.size + dedupParam() + extra;
}

async function sendPlex() {
  if (cur.i === null) return;
  const btn = document.getElementById('toPlex'), st = document.getElementById('xstatus');
  btn.disabled = true;
  st.textContent = 'Sending to Plex (indexing the library on first use — a few seconds)…';
  try {
    const extra = ENGINE ? musicipParams() : (weightParams() + varietyParam() + flowParam());
    const r = await fetch('/api/export/plex?i=' + cur.i + '&size=' + cur.size + dedupParam() + extra,
                          { method: 'POST' });
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
    ap = argparse.ArgumentParser(description="Attune web front-end.")
    ap.add_argument("--db", default=None,
                    help="path to an analyzed+CLAP-embedded mixer SQLite DB (see scan.py / "
                         "embed.py). Falls back to ATTUNE_DB, then settings.json.")
    ap.add_argument("--host", default="127.0.0.1", help="bind host (default: 127.0.0.1; use 0.0.0.0 for LAN)")
    ap.add_argument("--port", type=int, default=8778, help="port (default: 8778)")
    ap.add_argument("--engine", choices=("musicip", "v2", "learned", "auto"), default=None,
                    help="mix engine: 'musicip' drives a live MusicIP Mixer for search/mix; "
                         "'v2' uses our own CLAP/librosa HybridEngine with the full "
                         "weights/refine/mmr/flow UI; 'learned' uses the distilled-metric "
                         "ONNX head (selectable only -- not default until it wins a blind "
                         "ear test); 'auto' probes MusicIP and falls back "
                         "to v2. Falls back to settings.json (default: auto).")
    ap.add_argument("--musicip-url", default=None,
                    help="MusicIP Mixer HTTP API base URL (only used with --engine musicip)")
    ap.add_argument("--playlists", default=None,
                    help="folder of .m3u/.m3u8 playlists to browse, and the ONLY folder "
                         "'Save to playlist folder' can write into. Falls back to "
                         "ATTUNE_PLAYLIST_DIR, then settings.json.")
    args = ap.parse_args()

    # CLI > env > settings.json > default — the one precedence chain (config.py).
    cfgmod = _load_config()
    settings = cfgmod.load()
    db = cfgmod.effective(args.db, "ATTUNE_DB", "db_path", settings)
    engine = cfgmod.effective(args.engine, "ATTUNE_ENGINE", "engine", settings) or "auto"
    musicip_url = cfgmod.effective(args.musicip_url, "ATTUNE_MUSICIP_URL",
                                   "musicip_url", settings)
    playlists = cfgmod.effective(args.playlists, "ATTUNE_PLAYLIST_DIR",
                                 "playlist_dir", settings)
    if not db:
        raise SystemExit("No database configured. Pass --db, set ATTUNE_DB, or set "
                         f"db_path in {cfgmod.settings_path()}")
    if engine == "auto":
        # same probe the desktop launcher does: use MusicIP if it's live, else v2.
        import urllib.request
        try:
            with urllib.request.urlopen(musicip_url.rstrip("/") + "/api/version",
                                        timeout=2.0) as r:
                engine = "musicip" if b"MusicIP" in r.read(64) else "v2"
        except Exception:
            engine = "v2"
        print(f"engine=auto -> {engine}")
    app = create_app(db, engine_name=engine, musicip_url=musicip_url,
                     playlist_dir=playlists or None)
    print(f"Serving on http://{args.host}:{args.port} (engine={engine})")
    # threaded=True is REQUIRED for a media player: the default single-threaded dev server
    # serializes every request, so while the browser fetches a whole track to decode its
    # waveform (a big transfer over /audio) or computes a mix, the CURRENTLY PLAYING audio's
    # range requests queue behind it and playback stutters. Safe here: per-request SQLite
    # connections (check_same_thread satisfied), eng.w patched under weights_lock, writes under
    # UserData/ArtCache locks, and all other shared state is read-only or GIL-atomic.
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
