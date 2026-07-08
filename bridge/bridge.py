"""Attune Bridge — modern LAN front-end for a live MusicIP Mixer.

MusicIP (localhost:10002) stays the mix engine; this adds what 2026 needs:
  * browser UI reachable from any LAN device (laptop/phone)
  * library search with instant seed picking
  * style/variety/size controls
  * one-click .m3u export (MusicBee/Plex/LMS-readable)
  * "Copy to device": mix -> USB stick/folder directly, car-stereo-friendly
    numbered filenames, ID3 tags intact, optional track# renumbering

Setup: copy config.example.json -> config.json, edit paths, then:
    python bridge.py
Requires: flask, mutagen (pip install flask mutagen); a running MusicIP Mixer
with its API started (File > Preferences > Services > check API > Start).
If library_json is empty, the catalog is pulled from the MusicIP API at startup.
"""
import json, os, re, shutil, string, sys, threading, time, urllib.parse, urllib.request, unicodedata, uuid
from flask import Flask, request, jsonify, Response

HERE = os.path.dirname(os.path.abspath(__file__))
CFG_PATH = os.path.join(HERE, "config.json")
if not os.path.exists(CFG_PATH):
    sys.exit("config.json not found - copy config.example.json to config.json and edit it")
CFG = json.load(open(CFG_PATH, encoding="utf-8"))
MIP = CFG.get("mip_url", "http://localhost:10002").rstrip("/")
PORT = int(CFG.get("port", 8765))
EXPORT_DIR = CFG.get("export_dir") or os.path.join(HERE, "playlists")
READ_MAP = [tuple(x) for x in CFG.get("read_path_map", []) if x and x[0]]
M3U_MAP = [tuple(x) for x in CFG.get("m3u_path_map", []) if x and x[0]]

app = Flask(__name__)

# ---------------- MusicIP client ----------------
def mip_get(pathq, timeout=60):
    with urllib.request.urlopen(MIP + pathq, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")

# ---------------- library ----------------
def load_catalog():
    src = CFG.get("library_json")
    if src and os.path.exists(src):
        print(f"loading catalog from {src}")
        return json.load(open(src, encoding="utf-8"))
    print("pulling catalog from MusicIP API (songs?extended=1)...")
    raw = mip_get("/api/songs?extended=1", timeout=300)
    recs, cur = [], {}
    for line in raw.splitlines():
        if not line.strip():
            if cur: recs.append(cur); cur = {}
            continue
        k, _, v = line.partition(" ")
        cur[k] = v
    if cur: recs.append(cur)
    for r in recs:
        for k in ("seconds", "year", "bytes"):
            if k in r:
                try: r[k] = int(r[k])
                except ValueError: pass
    return recs

print("Attune Bridge starting...")
LIB = load_catalog()
BY_FILE = {r["file"]: r for r in LIB if r.get("file")}
def _fold(s):
    return unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
SEARCH_ROWS = [(_fold(f"{r.get('artist','')} {r.get('name','')} {r.get('album','')}"), r)
               for r in LIB if r.get("file")]
print(f"{len(SEARCH_ROWS)} tracks loaded")

def meta(path):
    r = BY_FILE.get(path, {})
    return {"file": path, "artist": r.get("artist"), "title": r.get("name"),
            "album": r.get("album"), "genre": r.get("genre"), "year": r.get("year"),
            "seconds": r.get("seconds")}

# ---------------- API ----------------
@app.get("/api/status")
def status():
    try:
        v = mip_get("/api/version", timeout=8).strip()
        return jsonify({"ok": True, "engine": v, "tracks": len(SEARCH_ROWS),
                        "export_dir": EXPORT_DIR})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502

@app.get("/api/search")
def search():
    q = _fold(request.args.get("q", "")).strip()
    if len(q) < 2:
        return jsonify([])
    terms = q.split()
    out = []
    for hay, r in SEARCH_ROWS:
        if all(t in hay for t in terms):
            out.append(meta(r["file"]))
            if len(out) >= 40:
                break
    return jsonify(out)

@app.get("/api/mix")
def mix():
    song = request.args.get("song", "")
    if not song:
        return jsonify({"error": "song required"}), 400
    args = {
        "song": song,
        "size": request.args.get("size", "25"),
        "sizetype": "tracks",
        "style": request.args.get("style", "40"),
        "variety": request.args.get("variety", "5"),
        "mixgenre": request.args.get("mixgenre", "0"),
        "rejectsize": request.args.get("rejectsize", "3"),
        "rejecttype": "tracks",
    }
    try:
        body = mip_get("/api/mix?" + urllib.parse.urlencode(args))
    except Exception as e:
        return jsonify({"error": f"MusicIP: {e}"}), 502
    tracks = [l for l in body.splitlines() if l.strip()]
    return jsonify({"seed": meta(song), "params": args,
                    "tracks": [meta(t) for t in tracks]})

def safe_name(s):
    s = re.sub(r"[^\w\-&' .()]", "", s or "mix").strip()
    return s[:80] or "mix"

def remap(path, table):
    for a, b in table:
        if path.startswith(a):
            return b + path[len(a):]
    return path

# --- security: the Bridge binds 0.0.0.0 (LAN) with no auth by design, so anyone on
# the network can call it. Two guards keep that from meaning arbitrary host file
# read/write: (1) export sources must be tracks that exist in the loaded library;
# (2) copy destinations are blocked from system/protected directories. ---
def known_tracks(tracks):
    """Keep only paths that are in the loaded library (blocks reading arbitrary host
    files by passing e.g. C:\\Windows\\... as a 'track')."""
    return [t for t in (tracks or []) if t in BY_FILE]

# --- destination ALLOWLIST (replaces the old system-dir blocklist) ---
# The Bridge binds 0.0.0.0 with no auth, so any LAN caller can post a copy job.
# Copy targets are restricted to an allowlist: removable drives plus roots listed in
# config ("device_export_roots"). Everything else — fixed/system drives, UNC/network
# shares, device namespaces — is refused. NOTE: "removable" is Windows DRIVE_REMOVABLE
# (type 2): USB sticks, SD cards, AND external HDDs/eSATA all count, so any mounted
# removable volume is fully writable by a LAN caller (never C:).
# Fail-closed: unknown => denied. Closes the blocklist bypasses (UNC exfil, \\?\
# device paths, 8.3 short names) and the %APPDATA%\...\Startup persistence vector.
DEVICE_EXPORT_ROOTS = [x for x in CFG.get("device_export_roots", []) if x]

def _removable_roots():
    roots = []
    if os.name == "nt":
        import ctypes
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            try:
                if os.path.exists(root) and ctypes.windll.kernel32.GetDriveTypeW(root) == 2:
                    roots.append(root)
            except OSError:
                pass
    return roots

def _allowed_roots():
    """Normalized real paths of every currently-allowed destination root."""
    norm = []
    for r in list(_removable_roots()) + list(DEVICE_EXPORT_ROOTS):
        if not r:
            continue
        try:
            norm.append(os.path.normcase(os.path.realpath(r)))
        except Exception:
            pass
    return norm

def _under_allowed(real_norm, roots):
    for r in roots:
        if real_norm == r or real_norm.startswith(r.rstrip("\\/") + os.sep):
            return True
    return False

def dest_ok(dest):
    """Validate a copy destination against the allowlist. Returns (ok, reason).
    Rejects relative paths, UNC/network shares, and Windows device namespaces
    (\\?\\, \\.\\); accepts only paths under a removable drive or configured root.
    copy_job re-verifies the real path AFTER makedirs (see _dest_still_allowed)."""
    if not dest or not isinstance(dest, str):
        return False, "destination required"
    d = dest.strip().strip('"')
    # Reject UNC (\\server\share) and device namespaces (\\?\, \\.\) up front, in
    # both slash directions, before any normalization can mask them.
    if d[:2] in ("\\\\", "//"):
        return False, "network (UNC) and device-namespace destinations are not allowed"
    if not os.path.isabs(d):
        return False, "destination must be an absolute path (e.g. E:\\Music\\Mix)"
    try:
        real = os.path.normcase(os.path.realpath(d))
    except Exception:
        return False, "invalid destination path"
    if real[:2] in ("\\\\", "//"):
        return False, "destination resolves to a network path"
    roots = _allowed_roots()
    if not roots:
        return False, "no removable drive or configured export folder available"
    if not _under_allowed(real, roots):
        return False, "destination must be on a removable drive or a configured export folder"
    return True, None

def _dest_still_allowed(dest):
    """Re-check AFTER makedirs: resolve the real path of the now-existing directory
    (following any junction/symlink) and confirm it is still under an allowed root.
    Guards the check->create->copy TOCTOU window."""
    try:
        real = os.path.normcase(os.path.realpath(dest))
    except Exception:
        return False
    if real[:2] in ("\\\\", "//"):
        return False
    return _under_allowed(real, _allowed_roots())

@app.post("/api/export")
def export():
    d = request.get_json(force=True)
    tracks = known_tracks(d.get("tracks"))
    if not tracks:
        return jsonify({"error": "no known tracks"}), 400
    name = "Like-" + safe_name(d.get("name") or "mix")
    os.makedirs(EXPORT_DIR, exist_ok=True)
    path = os.path.join(EXPORT_DIR, name + ".m3u")
    n = 1
    while os.path.exists(path):
        n += 1
        path = os.path.join(EXPORT_DIR, f"{name} ({n}).m3u")
    lines = ["#EXTM3U"]
    for t in tracks:
        m = BY_FILE.get(t, {})
        dur = m.get("seconds") or -1
        title = f"{m.get('artist','?')} - {m.get('name', os.path.basename(t))}"
        lines.append(f"#EXTINF:{dur},{title}")
        lines.append(remap(t, M3U_MAP))
    content = "\n".join(lines) + "\n"
    # .m3u for players whose file picker doesn't list .m3u8 (e.g. MusicBee's
    # Import Playlist dialog); UTF-8 BOM keeps non-ASCII intact either way.
    with open(path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write(content)
    m3u8_path = path[:-4] + ".m3u8"
    with open(m3u8_path, "w", encoding="utf-8-sig", newline="\r\n") as fh:
        fh.write(content)
    return jsonify({"ok": True, "path": path, "m3u8_path": m3u8_path, "count": len(tracks)})

# ---------------- device export ----------------
def read_path(p):
    """Prefer a fast local mirror for reading, fall back to the original path."""
    for a, b in READ_MAP:
        if p.startswith(a):
            loc = b + p[len(a):]
            if os.path.exists(loc):
                return loc
    return p

FAT_BAD = '<>:"/\\|?*'
def fat_safe(s, maxlen=120):
    s = "".join(c for c in (s or "") if c not in FAT_BAD and ord(c) >= 32)
    return s.strip(" .")[:maxlen] or "track"

def dest_filename(scheme, i, m, src):
    ext = os.path.splitext(src)[1]
    nn = f"{i:02d}"
    artist = fat_safe(m.get("artist") or "Unknown")
    title = fat_safe(m.get("title") or os.path.splitext(os.path.basename(src))[0])
    if scheme == "nn_artist_title":
        return f"{nn} - {artist} - {title}{ext}"
    if scheme == "nn_title":
        return f"{nn} - {title}{ext}"
    if scheme == "artist_title":
        return f"{artist} - {title}{ext}"
    return os.path.basename(src)

def renumber_tag(path, n):
    try:
        from mutagen import File as MFile
        from mutagen.id3 import ID3, TRCK
        if path.lower().endswith(".mp3"):
            try:
                t = ID3(path)
            except Exception:
                t = ID3()
            t.setall("TRCK", [TRCK(encoding=3, text=str(n))])
            t.save(path)
        else:
            f = MFile(path)
            if f is not None and f.tags is not None:
                f.tags["tracknumber"] = str(n)
                f.save()
    except Exception:
        pass

JOBS = {}
_JOBS_LOCK = threading.Lock()
MAX_COPY_TRACKS = 1000          # per-job cap: a LAN caller can't queue unbounded copies
MAX_ACTIVE_COPIES = 2           # concurrent copy jobs (disk/thread DoS guard)
_COPY_SEM = threading.BoundedSemaphore(MAX_ACTIVE_COPIES)

def _set_job(job_id, **kw):
    with _JOBS_LOCK:
        j = JOBS.get(job_id)
        if j is not None:
            j.update(**kw)

def copy_job(job_id, tracks, dest, scheme, renumber, write_m3u, plname):
    written = []
    if not _COPY_SEM.acquire(blocking=False):
        _set_job(job_id, state="error", error="too many active copies; try again shortly")
        return
    try:
        os.makedirs(dest, exist_ok=True)
        # TOCTOU re-check: the real created dir must still be under an allowed root
        # (defeats a junction/symlink planted between dest_ok and now).
        if not _dest_still_allowed(dest):
            _set_job(job_id, state="error", error="destination not allowed")
            return
        for i, t in enumerate(tracks, 1):
            src = read_path(t)
            m = BY_FILE.get(t, {})
            mm = {"artist": m.get("artist"), "title": m.get("name")}
            name = dest_filename(scheme, i, mm, src)
            out = os.path.join(dest, name)
            base, ext = os.path.splitext(out)
            k = 1
            while os.path.exists(out):
                k += 1
                out = f"{base} ({k}){ext}"
            _set_job(job_id, current=os.path.basename(out), done=i - 1)
            shutil.copy2(src, out)
            if renumber:
                renumber_tag(out, i)
            written.append(os.path.basename(out))
        if write_m3u and written:
            lines = ["#EXTM3U"] + written
            with open(os.path.join(dest, "Like-" + safe_name(plname) + ".m3u"),
                      "w", encoding="utf-8-sig", newline="\r\n") as fh:
                fh.write("\n".join(lines) + "\n")
        _set_job(job_id, done=len(tracks), state="done", current="")
    except Exception as e:
        _set_job(job_id, state="error", error=f"{e.__class__.__name__}: {e}")
    finally:
        _COPY_SEM.release()

@app.get("/api/devices")
def devices():
    out = []
    if os.name == "nt":
        import ctypes
        for letter in string.ascii_uppercase:
            root = f"{letter}:\\"
            if not os.path.exists(root):
                continue
            if ctypes.windll.kernel32.GetDriveTypeW(root) == 2:
                try:
                    usage = shutil.disk_usage(root)
                    out.append({"drive": root, "free_gb": round(usage.free / 1e9, 1),
                                "label": f"{letter}: (USB, {round(usage.free/1e9,1)} GB free)"})
                except OSError:
                    pass
    return jsonify(out)

@app.post("/api/export_device")
def export_device():
    d = request.get_json(force=True)
    tracks = known_tracks(d.get("tracks"))
    dest = (d.get("dest") or "").strip()
    if not tracks:
        return jsonify({"error": "no known tracks"}), 400
    if len(tracks) > MAX_COPY_TRACKS:
        return jsonify({"error": f"too many tracks (max {MAX_COPY_TRACKS})"}), 400
    ok, reason = dest_ok(dest)
    if not ok:
        return jsonify({"error": reason}), 400
    job_id = uuid.uuid4().hex[:10]
    with _JOBS_LOCK:
        if len(JOBS) > 200:   # bound memory: drop oldest finished jobs
            for k in [k for k, v in list(JOBS.items()) if v.get("state") != "running"][:100]:
                JOBS.pop(k, None)
        JOBS[job_id] = {"state": "running", "done": 0, "total": len(tracks),
                        "current": "", "dest": dest}
    threading.Thread(target=copy_job, args=(
        job_id, tracks, dest, d.get("scheme", "nn_artist_title"),
        bool(d.get("renumber", True)), bool(d.get("m3u", True)),
        d.get("name") or "mix"), daemon=True).start()
    return jsonify({"ok": True, "job": job_id})

@app.get("/api/export_status/<job_id>")
def export_status(job_id):
    j = JOBS.get(job_id)
    if not j:
        return jsonify({"error": "unknown job"}), 404
    return jsonify(j)

# ---------------- UI ----------------
PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Attune Bridge</title><style>
:root{--bg:#12141a;--card:#1c1f28;--tx:#e8e9ee;--mut:#8a8fa3;--acc:#7c5cff;--ok:#2fbf71}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--tx);
font:16px/1.45 system-ui,Segoe UI,Roboto,sans-serif;padding-bottom:70px}
.wrap{max-width:760px;margin:0 auto;padding:14px}
h1{font-size:20px;margin:8px 0 2px}h1 span{color:var(--acc)}
.sub{color:var(--mut);font-size:13px;margin-bottom:12px}
input[type=search]{width:100%;padding:12px 14px;font-size:17px;border-radius:12px;
border:1px solid #333a4d;background:var(--card);color:var(--tx);outline:none}
.card{background:var(--card);border-radius:12px;padding:10px 12px;margin:8px 0}
.row{display:flex;align-items:center;gap:10px;padding:8px 6px;border-radius:9px;cursor:pointer}
.row:hover{background:#262a38}.row .t{flex:1;min-width:0}
.row .name{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.row .sub2{color:var(--mut);font-size:12.5px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.badge{background:#2a2f40;color:var(--mut);font-size:11px;padding:2px 8px;border-radius:99px}
.controls{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px;margin:6px 0}
.controls label{font-size:12px;color:var(--mut);display:block}
.controls output{float:right;color:var(--tx)}
input[type=range]{width:100%;accent-color:var(--acc)}
button{background:var(--acc);border:0;color:#fff;font-size:15px;font-weight:600;
padding:12px 18px;border-radius:11px;cursor:pointer}
button.ghost{background:#2a2f40}button:disabled{opacity:.45}
.bar{position:fixed;left:0;right:0;bottom:0;background:#191c25;border-top:1px solid #2c3040;
padding:10px 14px;display:flex;gap:10px;justify-content:center}
.toast{position:fixed;top:12px;left:50%;transform:translateX(-50%);background:var(--ok);
color:#fff;padding:10px 18px;border-radius:10px;font-weight:600;display:none;z-index:9}
.num{color:var(--mut);width:20px;text-align:right;font-size:12.5px}
#status{font-size:12px;color:var(--mut)}
</style></head><body><div class="wrap">
<h1>&#127911; Attune <span>Bridge</span></h1>
<div class="sub">MusicIP engine &middot; modern controls &middot; <span id="status">connecting&hellip;</span></div>
<input type="search" id="q" placeholder="Search artist / title / album&hellip;" autocomplete="off">
<div id="results"></div>
<div id="mixer" style="display:none">
 <div class="card">
  <div class="row" id="seedrow"></div>
  <div class="controls">
   <div><label>Size <output id="ov1">25</output></label><input type="range" id="size" min="5" max="100" value="25" oninput="ov1.value=this.value"></div>
   <div><label>Style <output id="ov2">40</output></label><input type="range" id="style" min="0" max="100" value="40" oninput="ov2.value=this.value"></div>
   <div><label>Variety <output id="ov3">5</output></label><input type="range" id="variety" min="0" max="9" value="5" oninput="ov3.value=this.value"></div>
  </div>
  <button id="mixbtn" onclick="doMix()">Mix &#9656;</button>
  <button class="ghost" onclick="closeMixer()">&#10005;</button>
 </div>
 <div id="playlist"></div>
</div>
</div>
<div class="bar" id="bar" style="display:none">
 <button id="saveb" onclick="doExport()">&#128190; Save .m3u</button>
 <button class="ghost" onclick="toggleDevice()">&#128192; Copy to device&hellip;</button>
</div>
<div class="card" id="devpanel" style="display:none;position:fixed;left:50%;bottom:70px;
 transform:translateX(-50%);width:min(94vw,520px);z-index:8;border:1px solid #333a4d">
 <div style="font-weight:600;margin-bottom:6px">Copy mix to device / folder</div>
 <div id="drives" style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:8px"></div>
 <input type="search" id="dest" placeholder="E:\\Music\\RoadTrip  (full destination path)"
  style="margin-bottom:8px">
 <div style="display:flex;gap:14px;flex-wrap:wrap;font-size:13.5px;margin-bottom:8px">
  <select id="scheme" style="background:#2a2f40;color:var(--tx);border:0;border-radius:8px;padding:8px">
   <option value="nn_artist_title">01 - Artist - Title.mp3</option>
   <option value="nn_title">01 - Title.mp3</option>
   <option value="artist_title">Artist - Title.mp3</option>
   <option value="original">Original filenames</option>
  </select>
  <label><input type="checkbox" id="renum" checked> renumber track# tag</label>
  <label><input type="checkbox" id="incm3u" checked> include .m3u</label>
 </div>
 <div id="copyprog" style="display:none;color:var(--mut);font-size:13px;margin-bottom:8px"></div>
 <button id="copybtn" onclick="doDeviceCopy()">Copy &#9656;</button>
 <button class="ghost" onclick="$('devpanel').style.display='none'">Cancel</button>
</div>
<div class="toast" id="toast"></div>
<script>
let seed=null, mixTracks=[];
const $=id=>document.getElementById(id);
fetch('/api/status').then(r=>r.json()).then(s=>{
  $('status').textContent = s.ok? `${s.engine} · ${s.tracks.toLocaleString()} tracks` : 'ENGINE OFFLINE — start MusicIP + API';
});
let deb;
$('q').addEventListener('input',e=>{clearTimeout(deb);deb=setTimeout(()=>search(e.target.value),200)});
async function search(q){
  if(q.trim().length<2){$('results').innerHTML='';return}
  const r=await fetch('/api/search?q='+encodeURIComponent(q)).then(r=>r.json());
  $('results').innerHTML=r.map(t=>rowHtml(t)).join('');
}
function esc(s){return (s||'').replace(/[&<>"]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]))}
function rowHtml(t,i){
  return `<div class="row" onclick='pick(${JSON.stringify(t).replace(/'/g,"&#39;")})'>
   ${i!==undefined?`<div class="num">${i+1}</div>`:''}
   <div class="t"><div class="name">${esc(t.title)||'?'}</div>
   <div class="sub2">${esc(t.artist)||'?'} · ${esc(t.album)||''}</div></div>
   ${t.genre?`<span class="badge">${esc(t.genre).slice(0,22)}</span>`:''}</div>`;
}
function pick(t){
  seed=t; $('q').value=''; $('results').innerHTML='';
  $('mixer').style.display='block';
  $('seedrow').innerHTML=`<div class="t"><div class="name">🎯 ${esc(t.title)}</div>
   <div class="sub2">${esc(t.artist)} · seed track</div></div>`;
  $('playlist').innerHTML=''; $('bar').style.display='none';
  window.scrollTo({top:0,behavior:'smooth'});
}
function closeMixer(){$('mixer').style.display='none';$('bar').style.display='none';seed=null}
async function doMix(){
  if(!seed)return;
  $('mixbtn').disabled=true; $('mixbtn').textContent='Mixing…';
  const p=new URLSearchParams({song:seed.file,size:$('size').value,style:$('style').value,variety:$('variety').value});
  const r=await fetch('/api/mix?'+p).then(r=>r.json()).catch(e=>({error:e}));
  $('mixbtn').disabled=false; $('mixbtn').textContent='Mix ▸';
  if(r.error){toast('⚠ '+r.error,1);return}
  mixTracks=r.tracks;
  $('playlist').innerHTML='<div class="card">'+r.tracks.map((t,i)=>rowHtml(t,i)).join('')+'</div>';
  $('bar').style.display='flex';
}
async function doExport(){
  const name=(seed?.title||'mix');
  const r=await fetch('/api/export',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name,tracks:mixTracks.map(t=>t.file)})}).then(r=>r.json());
  if(r.ok)toast(`✔ Saved ${r.count} tracks → ${r.path.split('\\\\').pop()}`);
  else toast('⚠ '+(r.error||'export failed'),1);
}
function toast(msg,err){const t=$('toast');t.textContent=msg;
  t.style.background=err?'#d64545':'var(--ok)';t.style.display='block';
  setTimeout(()=>t.style.display='none',3500)}
async function toggleDevice(){
  const p=$('devpanel');
  if(p.style.display==='block'){p.style.display='none';return}
  p.style.display='block';
  const dr=await fetch('/api/devices').then(r=>r.json()).catch(()=>[]);
  $('drives').innerHTML = dr.length
    ? dr.map(d=>`<button class="ghost" style="padding:8px 12px;font-size:13px"
        onclick="$('dest').value='${d.drive.replace(/\\\\/g,'\\\\\\\\')}Music'">${d.label}</button>`).join('')
    : '<span style="color:var(--mut);font-size:13px">no USB drives detected — type a path</span>';
}
let copyPoll=null;
async function doDeviceCopy(){
  const dest=$('dest').value.trim();
  if(!dest){toast('⚠ enter a destination path',1);return}
  $('copybtn').disabled=true;
  const r=await fetch('/api/export_device',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({tracks:mixTracks.map(t=>t.file),dest,
      scheme:$('scheme').value,renumber:$('renum').checked,
      m3u:$('incm3u').checked,name:seed?.title||'mix'})}).then(r=>r.json());
  if(!r.ok){toast('⚠ '+(r.error||'failed'),1);$('copybtn').disabled=false;return}
  $('copyprog').style.display='block';
  copyPoll=setInterval(async()=>{
    const s=await fetch('/api/export_status/'+r.job).then(r=>r.json());
    if(s.state==='running'){
      $('copyprog').textContent=`Copying ${s.done+1}/${s.total}: ${s.current}`;
    }else{
      clearInterval(copyPoll);$('copybtn').disabled=false;
      $('copyprog').style.display='none';
      if(s.state==='done'){toast(`✔ ${s.total} tracks copied to ${s.dest}`);
        $('devpanel').style.display='none';}
      else toast('⚠ '+(s.error||'copy failed'),1);
    }
  },700);
}
</script></body></html>"""

@app.get("/")
def home():
    return Response(PAGE, mimetype="text/html")

if __name__ == "__main__":
    print(f"Attune Bridge -> http://localhost:{PORT}  (LAN: use this machine's IP)")
    app.run(host="0.0.0.0", port=PORT, threaded=True)
