#!/usr/bin/env python3
"""
LAN Music Server — single file
- Dark-themed listener + control pages
- Recursive library (folders), per-folder Add/Shuffle
- Shared server-side queue (add/remove/clear/next)
- Global play/pause/seek that syncs to all listeners via SSE
- Search box in control (client-side filtering)
- Media served from ./music (relative paths preserved)

Run:
    pip install Flask
    python app.py
"""
from __future__ import annotations
import time
import json
import random
import queue
import threading
from pathlib import Path
from typing import Dict, Any, List, Optional, Tuple

from flask import Flask, Response, request, jsonify, send_from_directory, abort, render_template_string

# ---------- Config ----------
BASE = Path(__file__).resolve().parent
MUSIC_DIR = (BASE / "music")
MUSIC_DIR.mkdir(exist_ok=True)
HOST = "0.0.0.0"
PORT = 5000
ADMIN_TOKEN: Optional[str] = None  # set to a string to require ?token=... for admin routes
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac"}

app = Flask(__name__)

# ---------- Global state ----------
state_lock = threading.Lock()
_listeners: List[queue.Queue] = []

STATE: Dict[str, Any] = {
    "track": None,      # relative path like "folder/song.mp3"
    "paused": True,
    "position": 0.0,
    "updated": time.time(),
}

PLAYLIST_QUEUE: List[str] = []
_track_started_at: Optional[float] = None

# ---------- Helpers ----------
def discover_tracks() -> List[str]:
    out: List[str] = []
    for p in MUSIC_DIR.rglob("*"):
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS:
            rel = str(p.relative_to(MUSIC_DIR)).replace("\\", "/")
            out.append(rel)
    out.sort(key=str.lower)
    return out

def library_by_folder() -> Dict[str, List[str]]:
    by: Dict[str, List[str]] = {}
    for t in discover_tracks():
        parts = t.split("/")
        folder = parts[0] if len(parts) > 1 else ""
        by.setdefault(folder, []).append(t)
    for k in by:
        by[k].sort(key=str.lower)
    keys = sorted(by.keys(), key=lambda x: (x != "", x.lower()))
    return {k: by[k] for k in keys}

def _broadcast_event(ev_type: str, payload: Dict[str, Any]) -> None:
    """Put an event (type + JSON) into every listener queue."""
    s = json.dumps({"type": ev_type, **payload} if ev_type == "state" else {"type": ev_type, **payload})
    stale: List[queue.Queue] = []
    for q in list(_listeners):
        try:
            q.put_nowait((ev_type, s))
        except Exception:
            stale.append(q)
    for q in stale:
        try:
            _listeners.remove(q)
        except ValueError:
            pass

def _set_state(track: Optional[str] = None, paused: Optional[bool] = None, position: Optional[float] = None) -> Dict[str, Any]:
    """Update global playback state and broadcast it."""
    global _track_started_at
    with state_lock:
        if track is not None:
            if track not in discover_tracks():
                raise FileNotFoundError(track)
            STATE["track"] = track
            STATE["position"] = 0.0 if position is None else float(position)
            STATE["paused"] = False if paused is None else paused
            _track_started_at = None if STATE["paused"] else time.time() - STATE["position"]
        if paused is not None and track is None:
            if STATE["track"] is None:
                STATE["paused"] = True
            else:
                if paused and not STATE["paused"]:
                    if _track_started_at is not None:
                        STATE["position"] += max(0.0, time.time() - _track_started_at)
                        _track_started_at = None
                if not paused and STATE["paused"]:
                    _track_started_at = time.time() - STATE["position"]
                STATE["paused"] = paused
        if position is not None and track is None:
            STATE["position"] = max(0.0, float(position))
            if not STATE.get("paused", False):
                _track_started_at = time.time() - STATE["position"]
        if not STATE.get("paused", False) and _track_started_at is not None:
            STATE["position"] = max(0.0, time.time() - _track_started_at)
        STATE["updated"] = time.time()
        snapshot = STATE.copy()
    _broadcast_event("state", snapshot)
    return snapshot

# ---------- Playlist helpers ----------
def _broadcast_playlist() -> None:
    _broadcast_event("playlist", {"queue": PLAYLIST_QUEUE})

def add_to_queue(items: List[str], shuffle: bool = False) -> None:
    valid = [t for t in items if t in discover_tracks()]
    if shuffle:
        random.shuffle(valid)
    PLAYLIST_QUEUE.extend(valid)
    _broadcast_playlist()
    # if nothing playing, start the first
    if STATE.get("track") is None and PLAYLIST_QUEUE:
        nxt = PLAYLIST_QUEUE.pop(0)
        _set_state(track=nxt, paused=False, position=0.0)

def remove_from_queue(idx: int) -> None:
    if 0 <= idx < len(PLAYLIST_QUEUE):
        PLAYLIST_QUEUE.pop(idx)
        _broadcast_playlist()

def clear_queue() -> None:
    PLAYLIST_QUEUE.clear()
    _broadcast_playlist()

def pop_next() -> Optional[str]:
    if PLAYLIST_QUEUE:
        nxt = PLAYLIST_QUEUE.pop(0)
        _broadcast_playlist()
        return nxt
    return None

# ---------- API routes ----------
@app.get("/library")
def api_library():
    return jsonify(library_by_folder())

@app.get("/playlist")
def api_playlist():
    return jsonify({"queue": PLAYLIST_QUEUE})

@app.post("/queue/add")
def api_queue_add():
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    items = body.get("items") or []
    shuffle = bool(body.get("shuffle", False))
    add_to_queue(items, shuffle=shuffle)
    return jsonify({"queue": PLAYLIST_QUEUE})

@app.post("/queue/clear")
def api_queue_clear():
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    clear_queue()
    return jsonify({"queue": PLAYLIST_QUEUE})

@app.post("/queue/remove")
def api_queue_remove():
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    idx = int(body.get("index", -1))
    remove_from_queue(idx)
    return jsonify({"queue": PLAYLIST_QUEUE})

@app.post("/queue/next")
def api_queue_next():
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    nxt = pop_next()
    if nxt:
        _set_state(track=nxt, paused=False, position=0.0)
        return jsonify({"playing": nxt})
    _set_state(track=None, paused=True, position=0.0)
    return jsonify({"playing": None})

@app.post("/ended")
def api_ended():
    # client reports end -> server pops next
    nxt = pop_next()
    if nxt:
        _set_state(track=nxt, paused=False, position=0.0)
        return jsonify({"playing": nxt})
    _set_state(track=None, paused=True, position=0.0)
    return jsonify({"playing": None})

@app.get("/media/<path:filename>")
def media(filename: str):
    safe = Path(filename)
    file_path = MUSIC_DIR / safe
    if not file_path.exists() or file_path.suffix.lower() not in AUDIO_EXTS:
        abort(404)
    # send_from_directory handles nested paths correctly
    return send_from_directory(MUSIC_DIR, filename, as_attachment=False)

@app.get("/state")
def api_state():
    with state_lock:
        snap = STATE.copy()
        # update position if playing
        global _track_started_at
        if not snap.get("paused", False) and _track_started_at is not None:
            snap["position"] = max(0.0, time.time() - _track_started_at)
    return jsonify(snap)

# ---------- SSE events ----------
@app.get("/events")
def sse_events():
    q: queue.Queue = queue.Queue()
    _listeners.append(q)

    def stream():
        # initial events
        with state_lock:
            init_state = STATE.copy()
        init_pl = {"queue": PLAYLIST_QUEUE}
        # yield named events
        yield f"event: state\n"
        yield f"data: {json.dumps({'type': 'state', **init_state})}\n\n"
        yield f"event: playlist\n"
        yield f"data: {json.dumps({'type': 'playlist', **init_pl})}\n\n"
        try:
            while True:
                ev_type, payload = q.get()
                # payload is a JSON string already with {"type":..., ...}
                yield f"event: {ev_type}\n"
                yield f"data: {payload}\n\n"
        except GeneratorExit:
            try:
                _listeners.remove(q)
            except ValueError:
                pass

    headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"}
    return Response(stream(), headers=headers)

# ---------- Playback control endpoints ----------
@app.post("/play")
def api_play():
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    track = body.get("track")
    pos = body.get("position")
    if not track:
        abort(400, "Missing track")
    _set_state(track=track, paused=False, position=pos)
    return jsonify(STATE)

@app.post("/pause")
def api_pause():
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    paused = bool(body.get("paused", True))
    _set_state(paused=paused)
    return jsonify(STATE)

@app.post("/seek")
def api_seek():
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    pos = float(body.get("position", 0.0))
    _set_state(position=pos)
    return jsonify(STATE)

# ---------- Pages (templates inline) ----------
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LAN Music — Listener</title>
<style>
:root{--bg:#05060a;--card:#071426;--muted:#9fb6c8;--accent:#2b82ff}
body{background:linear-gradient(180deg,#05060a,#0b1117);color:#e6eef8;font-family:system-ui, -apple-system, 'Segoe UI', Roboto, Arial;margin:0;padding:18px}
.container{max-width:880px;margin:0 auto}
.card{background:var(--card);border:1px solid #0f2433;padding:18px;border-radius:12px}
h1{margin:0 0 8px}
audio{width:100%;margin-top:12px}
.pill{display:inline-block;padding:6px 10px;background:#0f2130;border-radius:999px;border:1px solid #123}
.queue{margin-top:14px}
.queue-item{padding:8px;border-radius:8px;background:#071826;margin-bottom:6px;display:flex;justify-content:space-between}
.now{font-weight:700;color:#9be7ff}
button{background:var(--accent);border:none;color:white;padding:8px 12px;border-radius:10px;cursor:pointer}
button.secondary{background:#1b2430}
.small{color:var(--muted);font-size:13px}
</style>
</head>
<body>
<div class="container">
  <div class="card">
    <h1>🎧 LAN Party — Listener</h1>
    <p class="pill">Open <code>/control</code> on the host to manage the queue</p>
    <h2 id="title">No track selected</h2>
    <audio id="player" controls preload="auto"></audio>
    <div class="queue">
      <h3>Playlist</h3>
      <div id="queue"></div>
    </div>
  </div>
</div>

<script>
const player = document.getElementById('player');
const title = document.getElementById('title');
const queueEl = document.getElementById('queue');

function renderQueue(q){
  queueEl.innerHTML = '';
  q.forEach((t,i)=>{
    const el = document.createElement('div');
    el.className = 'queue-item';
    el.innerHTML = `<div>${i+1}. ${t}</div>`;
    if(i===0) el.querySelector('div').classList.add('now');
    queueEl.appendChild(el);
  });
}

function buildMediaUrl(track){
  // encode each segment so slashes remain separators
  return '/media/' + track.split('/').map(encodeURIComponent).join('/');
}

function applyState(s){
  title.textContent = s.track || 'No track selected';
  if(!s.track){
    player.removeAttribute('src');
    return;
  }
  const url = buildMediaUrl(s.track);
  if(!player.src || !player.src.includes(url)){
    player.src = url;
  }
  // always try to align to server position
  const trySeek = ()=>{ try{ if(Math.abs(player.currentTime - s.position) > 0.8) player.currentTime = s.position; }catch(e){} };
  if(player.readyState >= 1) trySeek(); else player.onloadedmetadata = trySeek;
  if(s.paused) player.pause(); else player.play().catch(()=>{});
}

player.onended = ()=>{ fetch('/ended', {method:'POST'}).catch(()=>{}); };

const es = new EventSource('/events');
es.addEventListener('state', e => { const d = JSON.parse(e.data); applyState(d); });
es.addEventListener('playlist', e => { const d = JSON.parse(e.data); renderQueue(d.queue); });

// initial fetch (in case SSE missed anything)
(async ()=>{
  const s = await (await fetch('/state')).json();
  applyState(s);
  const pl = await (await fetch('/playlist')).json();
  renderQueue(pl.queue);
})();
</script>
</body>
</html>
"""

CONTROL_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>LAN Music — Control</title>
<style>
:root{--bg:#05060a;--card:#071426;--muted:#9fb6c8;--accent:#2b82ff}
body{background:linear-gradient(180deg,#05060a,#0b1117);color:#e6eef8;font-family:system-ui, -apple-system, 'Segoe UI', Roboto, Arial;margin:0;padding:18px}
.wrap{max-width:1100px;margin:8px auto}
.card{background:var(--card);border:1px solid #0f2433;padding:14px;border-radius:12px}
.controls{display:flex;gap:8px;align-items:center;margin-bottom:10px}
.btn{background:var(--accent);border:none;padding:8px 12px;border-radius:10px;cursor:pointer}
.btn.ghost{background:transparent;border:1px solid #1f6feb;color:#a9d3ff;padding:6px 8px}
.grid{display:grid;grid-template-columns:340px 1fr;gap:12px}
.panel{padding:10px;background:#081626;border-radius:10px}
.folder{margin-bottom:8px}
.track{display:flex;justify-content:space-between;padding:6px;border-radius:8px;background:#06202b;margin:6px 0}
.btn-sm{background:#1f6feb;border:none;color:white;padding:6px 8px;border-radius:8px;cursor:pointer}
.playlist{margin-top:14px;padding:10px;background:#071826;border-radius:10px;max-height:320px;overflow:auto}
input[type=text]{width:100%;padding:8px;border-radius:8px;border:1px solid #123;background:#06131a;color:#e6eef8}
.small{font-size:13px;color:var(--muted)}
.queue-item{display:flex;justify-content:space-between;padding:8px;border-radius:8px;background:#051722;margin:6px 0}
.now{color:#9be7ff;font-weight:700}
.seek-row{display:flex;gap:12px;align-items:center}
.seek-row input[type=range]{flex:1}
.time{width:120px;text-align:right;color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
  <div class="card">
    <h1>🎛️ LAN Music — Control</h1>

    <div class="controls">
      <button id="playNext" class="btn btn-sm">Play Next</button>
      <button id="pauseBtn" class="btn btn-sm">Pause</button>
      <button id="resumeBtn" class="btn btn-sm">Resume</button>
      <div style="flex:1"></div>
      <button id="addAllBtn" class="btn btn-sm">Add All</button>
      <button id="addAllShuffleBtn" class="btn btn-sm">Add All (Shuffle)</button>
      <button id="clearQueueBtn" class="btn btn-sm">Clear</button>
    </div>

    <div class="card panel grid">
      <div>
        <h3 style="margin-top:0">Search</h3>
        <input id="searchInput" type="text" placeholder="Search songs (type 2+ chars)..." autocomplete="off">
        <div id="searchResults" style="margin-top:8px"></div>
      </div>

      <div>
        <h3 style="margin-top:0">Library</h3>
        <div id="library" style="max-height:420px;overflow:auto"></div>
      </div>
    </div>

    <div style="margin-top:12px">
      <div class="card panel">
        <div class="seek-row">
          <input id="seek" type="range" min="0" max="600" value="0">
          <div class="time" id="seekTime">0:00 / 0:00</div>
        </div>
      </div>

      <div class="playlist">
        <h3 style="margin:0 0 8px 0">Playlist Queue</h3>
        <div id="queue"></div>
      </div>
    </div>

  </div>
</div>

<script>
/* Utilities */
const $ = s => document.querySelector(s);
const fmt = s => { s = Math.max(0, Math.floor(s||0)); const m=Math.floor(s/60); const sec = s%60; return m + ':' + (sec<10?'0':'') + sec; };
const mediaUrl = track => '/media/' + track.split('/').map(encodeURIComponent).join('/');

let STATE = {track:null, paused:true, position:0};
let LIB = {};

async function loadLibrary(){
  const res = await fetch('/library');
  LIB = await res.json();
  renderLibrary();
  renderSearchResults(); // in case there's active search
}
async function loadQueue(){
  const res = await fetch('/playlist');
  const d = await res.json();
  renderQueue(d.queue);
}
async function loadState(){ const s = await (await fetch('/state')).json(); applyState(s); }

/* Renderers */
function renderLibrary(){
  const el = $('#library'); el.innerHTML='';
  for(const folder in LIB){
    const group = document.createElement('div'); group.className='folder';
    const header = document.createElement('div');
    header.innerHTML = `<strong>${folder || 'root'}</strong> <button class="btn ghost" data-folder="${folder}" data-action="addAll">Add All</button> <button class="btn ghost" data-folder="${folder}" data-action="shuffleAll">Shuffle</button>`;
    group.appendChild(header);
    LIB[folder].forEach(t=>{
      const tr = document.createElement('div'); tr.className='track';
      tr.innerHTML = `<div class="small">${t.split('/').pop()}</div>`;
      const right = document.createElement('div');
      const add = document.createElement('button'); add.className='btn-sm'; add.textContent='Add';
      add.onclick = ()=> queueAdd([t], false);
      right.appendChild(add);
      tr.appendChild(right);
      group.appendChild(tr);
    });
    el.appendChild(group);
  }
  // attach folder-level handlers
  el.querySelectorAll('[data-action]').forEach(btn=>{
    btn.onclick = ()=>{
      const folder = btn.dataset.folder;
      const action = btn.dataset.action;
      const items = LIB[folder] || [];
      if(action === 'addAll') queueAdd(items, false);
      else queueAdd(items, true);
    };
  });
}

function renderQueue(q){
  const el = $('#queue'); el.innerHTML='';
  q.forEach((t,i)=>{
    const div = document.createElement('div'); div.className='queue-item';
    div.innerHTML = `<div${i===0? ' class="now"':''}>${i+1}. ${t.split('/').pop()}</div>`;
    const right = document.createElement('div');
    const rm = document.createElement('button'); rm.className='btn ghost'; rm.textContent='✖';
    rm.onclick = ()=> fetch('/queue/remove',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({index:i})});
    right.appendChild(rm);
    if(i===0){
      const nowBtn = document.createElement('button'); nowBtn.className='btn-sm'; nowBtn.textContent='▶ Now';
      nowBtn.onclick = ()=> fetch('/play',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({track:t})});
      right.appendChild(nowBtn);
    }
    div.appendChild(right);
    el.appendChild(div);
  });
}

function renderSearchResults(term){
  const out = $('#searchResults'); out.innerHTML='';
  if(!term || term.length < 2) return;
  const lowered = term.toLowerCase();
  for(const folder in LIB){
    LIB[folder].forEach(t=>{
      if(t.toLowerCase().includes(lowered)){
        const row = document.createElement('div'); row.style.display='flex'; row.style.justifyContent='space-between'; row.style.padding='6px 0';
        row.innerHTML = `<div class="small">${t}</div>`;
        const b = document.createElement('button'); b.className='btn-sm'; b.textContent='Add';
        b.onclick = ()=> queueAdd([t], false);
        row.appendChild(b);
        out.appendChild(row);
      }
    });
  }
}

/* Actions */
async function queueAdd(items, shuffle){
  await fetch('/queue/add',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({items, shuffle})});
}
async function queueClear(){ await fetch('/queue/clear',{method:'POST'}); }
async function playNext(){ await fetch('/queue/next',{method:'POST'}); }
async function removeFromQueue(idx){ await fetch('/queue/remove',{method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({index:idx})}); }

/* State application */
function applyState(s){
  STATE = s;
  const player = document.querySelector('audio');
  if(STATE.track){
    const url = mediaUrl(STATE.track);
    if(!player.src || !player.src.includes(url)) player.src = url;
    try{ if(Math.abs(player.currentTime - STATE.position) > 0.8) player.currentTime = STATE.position; }catch(e){}
    if(STATE.paused) player.pause(); else player.play().catch(()=>{});
    $('#seek').value = Math.floor(STATE.position || 0);
  } else {
    player.removeAttribute('src');
  }
  $('#seekTime').textContent = fmt(STATE.position || 0) + " / " + (STATE.track ? (isFinite(player.duration)? fmt(player.duration): '?:??') : '0:00');
}

/* Wire up controls */
$('#addAllBtn').onclick = async ()=> { let all = []; for(const k in LIB) all = all.concat(LIB[k]); await queueAdd(all, false); };
$('#addAllShuffleBtn').onclick = async ()=> { let all = []; for(const k in LIB) all = all.concat(LIB[k]); await queueAdd(all, true); };
$('#clearQueueBtn').onclick = async ()=> { await queueClear(); };
$('#pauseBtn').onclick = ()=> fetch('/pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:true})});
$('#resumeBtn').onclick = ()=> fetch('/pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:false})});
$('#playNext').onclick = ()=> playNext();
$('#seek').addEventListener('input', e => { fetch('/seek',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position: parseFloat(e.target.value)})}); });

$('#searchInput').addEventListener('input', e => { renderSearchResults(e.target.value); });

/* SSE */
const sse = new EventSource('/events');
sse.addEventListener('state', e => { const d = JSON.parse(e.data); applyState(d); });
sse.addEventListener('playlist', e => { const d = JSON.parse(e.data); renderQueue(d.queue); });

/* Tick UI for seek time */
setInterval(()=>{
  const p = document.querySelector('audio');
  if(STATE.track && !STATE.paused){
    STATE.position = (STATE.position||0) + 1;
    $('#seek').value = Math.floor(STATE.position);
    $('#seekTime').textContent = fmt(STATE.position) + " / " + (p && isFinite(p.duration) ? fmt(p.duration) : '?:??');
  }
}, 1000);

/* initial load */
(async ()=>{
  await loadLibrary();
  await loadQueue();
  const s = await (await fetch('/state')).json();
  applyState(s);
})();

/* attach onended for audio when it appears */
new MutationObserver(()=>{
  const p = document.querySelector('audio');
  if(p) p.onended = ()=> fetch('/ended',{method:'POST'}).catch(()=>{});
}).observe(document.body, {childList:true, subtree:true});

</script>
</body>
</html>
"""

# Serve pages
@app.get("/")
def index_page():
    return render_template_string(INDEX_HTML)

@app.get("/control")
def control_page():
    if ADMIN_TOKEN and request.args.get("token") != ADMIN_TOKEN:
        return "<h1>🔒 Control Locked</h1><p>Append ?token=YOUR_TOKEN</p>"
    return render_template_string(CONTROL_HTML)

# ---------- Run ----------
if __name__ == "__main__":
    print(f"Starting LAN Music Server on http://{HOST}:{PORT}/")
    app.run(host=HOST, port=PORT, threaded=True)

