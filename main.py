#!/usr/bin/env python3
"""
LAN Music Server with Global Sync + Live Seek Updates

Improvements:
-------------
• All listeners stay in the same seek position (new joiners sync immediately).
• /control seek slider now auto-updates in real-time while music plays.
• Recursive music scanning with directory headers preserved.
"""

from __future__ import annotations
import os
import json
import time
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
    "track": None,
    "paused": False,
    "position": 0.0,
    "updated": time.time(),
}

_track_started_at: Optional[float] = None


def _playlist() -> List[str]:
    files = []
    for path in MUSIC_DIR.rglob("*"):
        if path.is_file() and path.suffix.lower() in AUDIO_EXTS:
            rel = str(path.relative_to(MUSIC_DIR))
            files.append(rel)
    files.sort(key=str.lower)
    return files


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
    global _track_started_at
    with state_lock:
        if track is not None:
            if track not in _playlist():
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
        event = {"type": "state", **STATE}
    _broadcast(event)
    return STATE.copy()

# ------------------------ Routes -------------------------------
@app.get("/playlist")
def playlist():
    return jsonify({"files": _playlist()})


@app.get("/media/<path:filename>")
def media(filename: str):
    safe = pathlib.Path(filename)
    file_path = MUSIC_DIR / safe
    if not file_path.exists() or file_path.suffix.lower() not in AUDIO_EXTS:
        abort(404)
    return send_from_directory(MUSIC_DIR, safe, as_attachment=False)


@app.get("/state")
def get_state():
    with state_lock:
        snapshot = STATE.copy()
        if not snapshot.get("paused", False) and _track_started_at is not None:
            snapshot["position"] = max(0.0, time.time() - _track_started_at)
    return jsonify(snapshot)


@app.get("/events")
def sse_events():
    q: queue.Queue = queue.Queue()
    listeners.append(q)

    def stream():
        init = json.dumps({"type": "state", **STATE})
        yield f"event: state\n"
        yield f"data: {init}\n\n"
        try:
            while True:
                payload = q.get()
                yield f"event: state\n"
                yield f"data: {payload}\n\n"
        except GeneratorExit:
            try:
                listeners.remove(q)
            except ValueError:
                pass

    headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"}
    return Response(stream(), headers=headers)


def _check_admin():
    if ADMIN_TOKEN is None:
        return True
    token = request.args.get("token") or request.headers.get("X-Admin-Token") or (request.json or {}).get("token")
    return token == ADMIN_TOKEN

@app.post("/play")
def play_track():
    if not _check_admin():
        abort(403)
    data = request.get_json(silent=True) or {}
    filename = data.get("track")
    pos = data.get("position")
    try:
        new_state = _set_state(track=filename, paused=False, position=pos)
    except FileNotFoundError:
        abort(404, f"No such track: {filename}")
    return jsonify(new_state)


@app.post("/pause")
def pause():
    if not _check_admin():
        abort(403)
    body = request.get_json(silent=True) or {}
    paused = bool(body.get("paused", True))
    new_state = _set_state(paused=paused)
    return jsonify(new_state)


@app.post("/seek")
def seek():
    if not _check_admin():
        abort(403)
    body = request.get_json(silent=True) or {}
    pos = float(body.get("position", 0))
    new_state = _set_state(position=pos)
    return jsonify(new_state)

# ------------------------ Pages -------------------------------
INDEX_HTML = r"""
<!doctype html>
<html><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>🎶 LAN Party — Listener</title></head>
<body style="font-family:sans-serif;background:#0b0f14;color:#e6edf3;">
<div style="max-width:760px;margin:24px auto;padding:16px;">
<h1>🎶 Listener</h1>
<button id="join">▶️ Join & Play</button> <span id="status">idle</span>
<h2 id="title">No track selected</h2>
<audio id="player" controls preload="auto"></audio>
</div>
<script>
const player=document.getElementById('player');
const title=document.getElementById('title');
const statusEl=document.getElementById('status');
let joined=false;

document.getElementById('join').onclick=()=>{joined=true;statusEl.textContent='connected';player.play().catch(()=>{});};

function setTrack(name,paused,position){
 if(!name){title.textContent='No track selected';player.removeAttribute('src');return;}
 const url='/media/'+encodeURIComponent(name);
 if(player.src!==location.origin+url){player.src=url;}
 title.textContent=name;
 if(typeof position==='number'){const seek=()=>{try{player.currentTime=position;}catch(e){}};if(player.readyState>=1)seek();else player.onloadedmetadata=seek;}
 if(!paused&&joined)player.play().catch(()=>{});
 if(paused)player.pause();
}

async function refresh(){const s=await (await fetch('/state')).json();setTrack(s.track,s.paused,s.position);} 

const es=new EventSource('/events');
es.addEventListener('state',(e)=>{const d=JSON.parse(e.data);if(d.type==='state'){setTrack(d.track,d.paused,d.position);}});

refresh();
</script></body></html>
"""

CONTROL_HTML = r"""
<!doctype html>
<html><head><meta charset="utf-8" /><meta name="viewport" content="width=device-width, initial-scale=1" />
<title>🎛️ Control</title></head>
<body style="font-family:sans-serif;background:#0b0f14;color:#e6edf3;">
<div style="max-width:920px;margin:24px auto;padding:16px;">
<h1>🎛️ Control Panel</h1>
<div><button id="pause">⏸️ Pause</button><button id="resume">▶️ Resume</button> <input id="seek" type="range" min="0" max="300" value="0"> <span id="now">loading…</span></div>
<div id="playlist"></div>
</div>
<script>
const list=document.getElementById('playlist');
const now=document.getElementById('now');
const seek=document.getElementById('seek');
let currentState={track:null,paused:true,position:0};
const token=new URLSearchParams(location.search).get('token')||'';

async function load(){const res=await fetch('/playlist');const d=await res.json();list.innerHTML='';let currentDir='';d.files.forEach(f=>{let parts=f.split('/');if(parts.length>1&&parts[0]!==currentDir){currentDir=parts[0];let h=document.createElement('h3');h.textContent=currentDir;list.appendChild(h);}let b=document.createElement('button');b.textContent='▶️ '+parts[parts.length-1];b.onclick=()=>play(f);list.appendChild(b);});}

async function play(name){await fetch('/play'+(token?`?token=${token}`:''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({track:name})});}

document.getElementById('pause').onclick=async()=>{await fetch('/pause'+(token?`?token=${token}`:''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:true})});};

document.getElementById('resume').onclick=async()=>{await fetch('/pause'+(token?`?token=${token}`:''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({paused:false})});};

seek.onchange=async()=>{const pos=parseInt(seek.value);await fetch('/seek'+(token?`?token=${token}`:''),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({position:pos})});};

function updateUI(){now.textContent=currentState.track?`Now: ${currentState.track}${currentState.paused?' (paused)':''}`:'No track';seek.value=Math.floor(currentState.position||0);} 

const es=new EventSource('/events');
es.addEventListener('state',(e)=>{const d=JSON.parse(e.data);if(d.type==='state'){currentState=d;updateUI();}});

setInterval(()=>{if(currentState.track&&!currentState.paused){currentState.position+=1;updateUI();}},1000);

load();
</script></body></html>
"""

@app.get("/")
def index_page():
    return render_template_string(INDEX_HTML)

@app.get("/control")
def control_page():
    if ADMIN_TOKEN and not _check_admin():
        return "<h1>🔒 Control Locked</h1><p>Append ?token=YOUR_TOKEN</p>"
    return render_template_string(CONTROL_HTML)

if __name__=="__main__":
    print(f"LAN Music Server running on http://<your-LAN-IP>:{PORT}/")
    app.run(host=HOST,port=PORT,debug=False,threaded=True)

