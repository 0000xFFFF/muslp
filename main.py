#!/usr/bin/env python3
"""
LAN Music Server — Shared Queue, Foldered Library, Live Sync

Features implemented:
 - Recursive music discovery (shows folders with "Add all" and "Shuffle add" per-folder)
 - Global playlist queue stored on server; control panel can add/remove/clear items
 - Clicking a track in the library adds it to the shared queue (does not auto-play)
 - Buttons for "Add All" and "Add All (shuffle)" for entire library
 - Playlist is shown on both control and listener pages and updates live via SSE
 - Control can play next, and when a client reports audio ended it triggers server-side advance
 - Seeking, pause/resume remain global and sync to all clients

How to use
----------
1) Put music files under ./music (can be nested in folders).
2) Install Flask: pip install Flask
3) Run: python app.py
4) Open listener: http://<your-lan-ip>:5000/
   Open control:  http://<your-lan-ip>:5000/control

Security
--------
This is designed for trusted LANs. To restrict control, set env ADMIN_TOKEN and pass ?token=... on /control or include token in requests.

Notes
-----
- The server relies on clients to report "ended" (audio.onended) to advance the queue (`/ended`). On a LAN that is fine; if you want server-side exact timing, we'd need durations indexed server-side.

"""

from __future__ import annotations
import os
import json
import time
import random
import queue
import pathlib
import threading
from typing import Dict, Any, Optional, List

from flask import (
    Flask,
    Response,
    request,
    send_from_directory,
    jsonify,
    abort,
    render_template_string,
)

# ------------------------ Configuration ------------------------
BASE_DIR = pathlib.Path(__file__).resolve().parent
MUSIC_DIR = BASE_DIR / "music"
MUSIC_DIR.mkdir(exist_ok=True)

HOST = "0.0.0.0"
PORT = int(os.environ.get("PORT", 5000))
ADMIN_TOKEN: Optional[str] = os.environ.get("ADMIN_TOKEN") or None

AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac"}

# ------------------------ App State ----------------------------
app = Flask(__name__)

state_lock = threading.Lock()
listeners: List[queue.Queue] = []

STATE: Dict[str, Any] = {
    "track": None,      # currently playing track (relative path from MUSIC_DIR)
    "paused": True,
    "position": 0.0,
    "updated": time.time(),
}

# Playlist queue: list of track paths
PLAYLIST_QUEUE: List[str] = []

_track_started_at: Optional[float] = None

# ------------------------ Helpers --------------------------------

def _discover_tracks() -> List[str]:
    files = []
    for path in MUSIC_DIR.rglob("*"):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            rel = str(path.relative_to(MUSIC_DIR)).replace("\\", "/")
            files.append(rel)
    files.sort(key=str.lower)
    return files


def _library_by_folder() -> Dict[str, List[str]]:
    # top-level folder name ('' for root) -> list of tracks
    by_folder: Dict[str, List[str]] = {}
    for t in _discover_tracks():
        parts = t.split('/')
        folder = parts[0] if len(parts) > 1 else ''
        by_folder.setdefault(folder, []).append(t)
    # keep deterministic order
    for k in by_folder:
        by_folder[k].sort(key=str.lower)
    # sort keys with '' (root) first
    ordered = {k: by_folder[k] for k in sorted(by_folder.keys(), key=lambda x: (x != '', x.lower()))}
    return ordered


def _broadcast(event: Dict[str, Any]) -> None:
    payload = json.dumps(event)
    stale = []
    for q in listeners:
        try:
            q.put_nowait(payload)
        except Exception:
            stale.append(q)
    for q in stale:
        try:
            listeners.remove(q)
        except ValueError:
            pass


def _set_state(track: Optional[str] = None, paused: Optional[bool] = None, position: Optional[float] = None) -> Dict[str, Any]:
    """Update global playback state and broadcast to clients."""
    global _track_started_at
    with state_lock:
        if track is not None:
            if track not in _discover_tracks():
                raise FileNotFoundError(f"Track not found: {track}")
            STATE["track"] = track
            STATE["position"] = 0.0 if position is None else float(position)
            STATE["paused"] = False if paused is None else paused
            _track_started_at = None if STATE["paused"] else time.time() - STATE["position"]
        if paused is not None and track is None:
            if STATE["track"] is None:
                STATE["paused"] = True
            else:
                if paused and not STATE["paused"]:
                    # pause -> freeze position
                    if _track_started_at is not None:
                        STATE["position"] += max(0.0, time.time() - _track_started_at)
                        _track_started_at = None
                if not paused and STATE["paused"]:
                    # resume -> continue from position
                    _track_started_at = time.time() - STATE["position"]
                STATE["paused"] = paused
        if position is not None and track is None:
            STATE["position"] = max(0.0, float(position))
            if not STATE.get("paused", False):
                _track_started_at = time.time() - STATE["position"]
        if not STATE.get("paused", False) and _track_started_at is not None:
            STATE["position"] = max(0.0, time.time() - _track_started_at)
        STATE["updated"] = time.time()
        event = {"type": "state", **STATE}
    _broadcast(event)
    return STATE.copy()

# ------------------------ Queue Management ----------------------

def _broadcast_playlist():
    evt = {"type": "playlist", "queue": PLAYLIST_QUEUE}
    _broadcast(evt)


def add_to_queue(items: List[str], shuffle: bool = False) -> None:
    # validate items exist
    tracks = [t for t in items if t in _discover_tracks()]
    if shuffle:
        random.shuffle(tracks)
    PLAYLIST_QUEUE.extend(tracks)
    _broadcast_playlist()


def remove_from_queue(index: int) -> None:
    if 0 <= index < len(PLAYLIST_QUEUE):
        PLAYLIST_QUEUE.pop(index)
        _broadcast_playlist()


def clear_queue() -> None:
    PLAYLIST_QUEUE.clear()
    _broadcast_playlist()


def queue_pop_next() -> Optional[str]:
    if PLAYLIST_QUEUE:
        nxt = PLAYLIST_QUEUE.pop(0)
        _broadcast_playlist()
        return nxt
    return None

# ------------------------ Routes: API ---------------------------

@app.get('/playlist')
def api_playlist():
    # return server-side queue
    return jsonify({"queue": PLAYLIST_QUEUE})


@app.post('/queue/add')
def api_queue_add():
    if ADMIN_TOKEN and request.args.get('token') != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    items = body.get('items') or []
    shuffle = bool(body.get('shuffle', False))
    add_to_queue(items, shuffle=shuffle)
    return jsonify({"queue": PLAYLIST_QUEUE})


@app.post('/queue/clear')
def api_queue_clear():
    if ADMIN_TOKEN and request.args.get('token') != ADMIN_TOKEN:
        abort(403)
    clear_queue()
    return jsonify({"queue": PLAYLIST_QUEUE})


@app.post('/queue/remove')
def api_queue_remove():
    if ADMIN_TOKEN and request.args.get('token') != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    idx = int(body.get('index', -1))
    remove_from_queue(idx)
    return jsonify({"queue": PLAYLIST_QUEUE})


@app.post('/queue/next')
def api_queue_next():
    # pop next and start playing it
    if ADMIN_TOKEN and request.args.get('token') != ADMIN_TOKEN:
        abort(403)
    nxt = queue_pop_next()
    if nxt:
        _set_state(track=nxt, paused=False, position=0.0)
        return jsonify({"playing": nxt})
    else:
        # nothing left: stop playback
        _set_state(track=None, paused=True, position=0.0)
        return jsonify({"playing": None})


@app.post('/ended')
def api_ended():
    # a client notifies the server that the current track ended -> server pops next
    # any client may call this (on a LAN), so it's fine if multiple do; queue_pop_next handles state
    nxt = queue_pop_next()
    if nxt:
        _set_state(track=nxt, paused=False, position=0.0)
        return jsonify({"playing": nxt})
    else:
        _set_state(track=None, paused=True, position=0.0)
        return jsonify({"playing": None})


@app.get('/library')
def api_library():
    # grouped by folder
    return jsonify(_library_by_folder())


@app.get('/media/<path:filename>')
def media(filename: str):
    safe = pathlib.Path(filename)
    file_path = MUSIC_DIR / safe
    if not file_path.exists() or file_path.suffix.lower() not in AUDIO_EXTS:
        abort(404)
    return send_from_directory(MUSIC_DIR, safe, as_attachment=False)


@app.get('/state')
def api_state():
    with state_lock:
        snapshot = STATE.copy()
        if not snapshot.get('paused', False) and _track_started_at is not None:
            snapshot['position'] = max(0.0, time.time() - _track_started_at)
    return jsonify(snapshot)


@app.get('/events')
def sse_events():
    q: queue.Queue = queue.Queue()
    listeners.append(q)

    def stream():
        # send initial full payload (state + playlist)
        init_state = json.dumps({"type": "state", **STATE})
        init_pl = json.dumps({"type": "playlist", "queue": PLAYLIST_QUEUE})
        yield f"event: state\n"
        yield f"data: {init_state}\n\n"
        yield f"event: playlist\n"
        yield f"data: {init_pl}\n\n"
        try:
            while True:
                payload = q.get()
                # client will receive events of type 'state' or 'playlist'
                yield f"data: {payload}\n\n"
        except GeneratorExit:
            try:
                listeners.remove(q)
            except ValueError:
                pass

    headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"}
    return Response(stream(), headers=headers)

# ------------------------ Playback control endpoints ----------

@app.post('/play')
def api_play():
    if ADMIN_TOKEN and request.args.get('token') != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    track = body.get('track')
    pos = body.get('position')
    if not track:
        abort(400, 'Missing track')
    _set_state(track=track, paused=False, position=pos)
    return jsonify(STATE)


@app.post('/pause')
def api_pause():
    if ADMIN_TOKEN and request.args.get('token') != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    paused = bool(body.get('paused', True))
    _set_state(paused=paused)
    return jsonify(STATE)


@app.post('/seek')
def api_seek():
    if ADMIN_TOKEN and request.args.get('token') != ADMIN_TOKEN:
        abort(403)
    body = request.get_json(silent=True) or {}
    pos = float(body.get('position', 0.0))
    _set_state(position=pos)
    return jsonify(STATE)

# ------------------------ Pages: UI -------------------------------

INDEX_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LAN Music — Listener</title>
<style>
  body{font-family:system-ui, -apple-system, 'Segoe UI', Roboto, Arial;background:#0b0f14;color:#e6edf3;margin:0}
  .wrap{max-width:760px;margin:24px auto;padding:16px}
  .card{background:#0f1722;padding:16px;border-radius:12px}
  audio{width:100%;margin-top:12px}
  .queue{margin-top:12px}
  .queue-item{display:flex;justify-content:space-between;padding:6px 8px;border-radius:8px;background:#0b1220;margin-bottom:6px}
  .now{font-weight:700}
</style>
</head>
<body>
<div class="wrap card">
  <h1>🎶 Listener</h1>
  <p>Ask the host to share <code>/control</code> to manage the queue.</p>
  <div><button id="join">Join (auto-play when host plays)</button> <span id="status">idle</span></div>
  <h2 id="title">No track</h2>
  <audio id="player" controls preload="auto"></audio>
  <div class="queue">
    <h3>Playlist</h3>
    <div id="queue"></div>
  </div>
</div>
<script>
const player = document.getElementById('player');
const title = document.getElementById('title');
const status = document.getElementById('status');
const queueEl = document.getElementById('queue');
let joined=false;
let currentState={track:null,paused:true,position:0};

document.getElementById('join').onclick = ()=>{joined=true;status.textContent='connected';player.play().catch(()=>{});};

function renderQueue(q){queueEl.innerHTML='';q.forEach((t,i)=>{const div=document.createElement('div');div.className='queue-item';div.innerHTML=`<div>${i+1}. ${t}</div><div class=\"small\">${i===0 && currentState.track===t?'<span class="now">(up next)</span>':''}</div>`;queueEl.appendChild(div);});}

function setTrack(name, paused, position){
  currentState.track = name;
  currentState.paused = !!paused;
  currentState.position = position || 0;
  if(!name){ title.textContent='No track'; player.removeAttribute('src'); return }
  const url = '/media/' + encodeURIComponent(name);
  if(player.src !== location.origin + url){ player.src = url; }
  title.textContent = name;
  // always seek to global position when state arrives
  const trySeek = ()=>{ try{ player.currentTime = currentState.position; } catch(e){} };
  if(player.readyState >= 1) trySeek(); else player.onloadedmetadata = trySeek;
  if(!currentState.paused && joined) player.play().catch(()=>{});
  if(currentState.paused) player.pause();
}

player.onended = async ()=>{
  // notify server so it can pop next and broadcast
  try{ await fetch('/ended', {method:'POST'}); }catch(e){}
}

// Live SSE
const es = new EventSource('/events');
es.onmessage = (e)=>{
  // fallback: parse generic messages (we always send full JSON payloads)
  try{
    const parsed = JSON.parse(e.data);
    if(parsed.type === 'state'){
      setTrack(parsed.track, parsed.paused, parsed.position);
    } else if(parsed.type === 'playlist'){
      renderQueue(parsed.queue);
    }
  }catch(err){ }
};
// also listen to named events (older browsers may differ)
es.addEventListener('state', (e)=>{ const d = JSON.parse(e.data); setTrack(d.track,d.paused,d.position); });
es.addEventListener('playlist', (e)=>{ const d = JSON.parse(e.data); renderQueue(d.queue); });

// initial fetch
(async ()=>{ const s = await (await fetch('/state')).json(); setTrack(s.track,s.paused,s.position); const pl = await (await fetch('/playlist')).json(); renderQueue(pl.queue); })();
</script>
</body>
</html>
"""

CONTROL_HTML = r"""
<!doctype html>
<html>
<head>
<meta charset="utf-8" />
<meta name="viewport" content="width=device-width, initial-scale=1" />
<title>LAN Music — Control</title>
<style>
  body{font-family:system-ui, -apple-system, 'Segoe UI', Roboto, Arial;background:#07101a;color:#e6edf3;margin:0}
  .wrap{max-width:1100px;margin:16px auto;padding:16px}
  .cols{display:grid;grid-template-columns:1fr 360px;gap:12px}
  .card{background:#0b1622;padding:12px;border-radius:10px}
  .lib-list{max-height:480px;overflow:auto}
  .folder{margin-bottom:8px}
  .track{display:flex;justify-content:space-between;padding:6px;border-radius:6px;background:#071826;margin:4px 0}
  button{padding:6px 10px;border-radius:8px;border:none;cursor:pointer}
  .queue-item{display:flex;justify-content:space-between;padding:6px;border-radius:6px;background:#071826;margin-bottom:6px}
  .controls{display:flex;gap:8px;align-items:center}
</style>
</head>
<body>
<div class="wrap">
  <h1>🎛️ Control Panel</h1>
  <div class="controls card">
    <button id="playNext">Play Next</button>
    <button id="pause">Pause</button>
    <button id="resume">Resume</button>
    <label>Seek: <input id="seek" type="range" min="0" max="600" value="0" style="width:240px"></label>
    <button id="clearQueue">Clear Playlist</button>
    <button id="addAll">Add All</button>
    <button id="addAllShuffle">Add All (Shuffle)</button>
    <span id="now">loading…</span>
  </div>

  <div class="cols">
    <div class="card">
      <h3>Library</h3>
      <div class="lib-list" id="library"></div>
    </div>

    <div class="card">
      <h3>Playlist Queue</h3>
      <div id="queue"></div>
    </div>
  </div>
</div>

<script>
const libraryEl = document.getElementById('library');
const queueEl = document.getElementById('queue');
const now = document.getElementById('now');
const seek = document.getElementById('seek');
let currentState = {track:null, paused:true, position:0};
const token = new URLSearchParams(location.search).get('token') || '';

async function api(path, opts){
  if(token) path += (path.includes('?')? '&':'?') + 'token=' + encodeURIComponent(token);
  return fetch(path, opts);
}

function renderLibrary(lib){
  libraryEl.innerHTML='';
  for(const folder in lib){
    const tracks = lib[folder];
    const container = document.createElement('div');
    container.className='folder';
    const header = document.createElement('div');
    header.innerHTML = `<strong>${folder||'root'}</strong> <button data-folder='${folder}' class='addFolder'>Add All</button> <button data-folder='${folder}' class='addFolderShuffle'>Add All (Shuffle)</button>`;
    container.appendChild(header);
    tracks.forEach(t=>{
      const tr = document.createElement('div'); tr.className='track';
      const name = document.createElement('div'); name.textContent = t.split('/').pop();
      const actions = document.createElement('div');
      const addBtn = document.createElement('button'); addBtn.textContent='Add'; addBtn.onclick = ()=> addToQueue([t], false);
      actions.appendChild(addBtn);
      tr.appendChild(name); tr.appendChild(actions); container.appendChild(tr);
    });
    libraryEl.appendChild(container);
  }
  // attach folder buttons
  libraryEl.querySelectorAll('.addFolder').forEach(btn=>{btn.onclick=()=>{const f=btn.dataset.folder; const items = lib[f]; addToQueue(items, false);}});
  libraryEl.querySelectorAll('.addFolderShuffle').forEach(btn=>{btn.onclick=()=>{const f=btn.dataset.folder; const items = lib[f]; addToQueue(items, true);}});
}

function renderQueue(q){
  queueEl.innerHTML='';
  q.forEach((t,i)=>{
    const div = document.createElement('div'); div.className='queue-item';
    div.innerHTML = `<div>${i+1}. ${t.split('/').pop()}</div>`;
    const right = document.createElement('div');
    const rm = document.createElement('button'); rm.textContent='✖'; rm.onclick = ()=> removeFromQueue(i);
    right.appendChild(rm);
    if(i===0){ const playNow = document.createElement('button'); playNow.textContent='▶ Now'; playNow.onclick = ()=> { fetch('/play' + (token?`?token=${token}`:''), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({track:t})}); }; right.appendChild(playNow); }
    div.appendChild(right);
    queueEl.appendChild(div);
  });
}

async function addToQueue(items, shuffle){
  await api('/queue/add', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({items:items, shuffle:shuffle})});
}
async function clearQueue(){ await api('/queue/clear', {method:'POST'}); }
async function removeFromQueue(idx){ await api('/queue/remove', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({index:idx})}); }
async function playNext(){ await api('/queue/next', {method:'POST'}); }

// controls
document.getElementById('clearQueue').onclick = clearQueue;
document.getElementById('addAll').onclick = async ()=>{ const lib = await (await fetch('/library')).json(); let all=[]; for(const k in lib) all = all.concat(lib[k]); await addToQueue(all,false); };
document.getElementById('addAllShuffle').onclick = async ()=>{ const lib = await (await fetch('/library')).json(); let all=[]; for(const k in lib) all = all.concat(lib[k]); await addToQueue(all,true); };

document.getElementById('pause').onclick = ()=> fetch('/pause' + (token?`?token=${token}`:''), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({paused:true})});
document.getElementById('resume').onclick = ()=> fetch('/pause' + (token?`?token=${token}`:''), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({paused:false})});

document.getElementById('playNext').onclick = playNext;

seek.onchange = ()=>{ fetch('/seek' + (token?`?token=${token}`:''), {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({position:parseFloat(seek.value)})}); };

function updateNow(s){ now.textContent = s.track ? `Now: ${s.track.split('/').pop()}${s.paused? ' (paused)':''}` : 'No track'; seek.max = 600; seek.value = Math.floor(s.position||0); }

// SSE
const es = new EventSource('/events');
es.onmessage = (e)=>{ try{ const parsed = JSON.parse(e.data); if(parsed.type === 'state'){ currentState = parsed; updateNow(parsed); } else if(parsed.type === 'playlist'){ renderQueue(parsed.queue); } }catch(err){} };
es.addEventListener('state', (e)=>{ const d=JSON.parse(e.data); currentState=d; updateNow(d); });
es.addEventListener('playlist', (e)=>{ const d=JSON.parse(e.data); renderQueue(d.queue); });

// keep local ticking so seek updates while playing
setInterval(()=>{ if(currentState.track && !currentState.paused){ currentState.position = currentState.position + 1; seek.value = Math.floor(currentState.position); } }, 1000);

// initial load
(async ()=>{ const lib = await (await fetch('/library')).json(); renderLibrary(lib); const q = await (await fetch('/playlist')).json(); renderQueue(q.queue); const s = await (await fetch('/state')).json(); currentState = s; updateNow(s); })();
</script>
</body>
</html>
"""

# ------------------------ Routes: Pages --------------------------
@app.get('/')
def index_page():
    return render_template_string(INDEX_HTML)


@app.get('/control')
def control_page():
    if ADMIN_TOKEN and request.args.get('token') != ADMIN_TOKEN:
        return '<h1>🔒 Control Locked</h1><p>Append ?token=YOUR_TOKEN</p>'
    return render_template_string(CONTROL_HTML)

# ------------------------ Run server ----------------------------
if __name__ == '__main__':
    print(f"LAN Music Server running on http://<your-LAN-IP>:{PORT}/")
    app.run(host=HOST, port=PORT, debug=False, threaded=True)

