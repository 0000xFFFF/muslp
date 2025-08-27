#!/usr/bin/env python3
"""
LAN Music Server — single-file Flask app

What it does
------------
• Serves a simple web app that your friends can open from any device on your LAN to listen.
• You (the host) control which track is playing from a /control page.
• All listeners switch to the current track instantly via Server‑Sent Events (SSE).
• Streams local audio files (mp3, m4a, ogg, wav, flac*) from the ./music folder.
  *FLAC support depends on the browser; Safari/iOS may not play it.

How to use
----------
1) Install deps:  pip install Flask
2) Put your music files into a folder named "music" in the same directory as this script.
3) Run the server:  python app.py
4) Find your LAN IP (e.g. 192.168.1.23). On your device open:
   • Listener page:    http://<your-LAN-IP>:5000/
   • Host control UI: http://<your-LAN-IP>:5000/control
5) If Windows firewall prompts you, allow access for Private networks.

Security
--------
This is meant for trusted LANs only. Anyone on your LAN who knows the URL can listen and control if they open /control. To lock control, set ADMIN_TOKEN below (then append ?token=... to /control).

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
# Set a token to restrict access to /control (optional). Example: ADMIN_TOKEN = "letmein123"
ADMIN_TOKEN: Optional[str] = os.environ.get("ADMIN_TOKEN") or None

# Allowed audio extensions
AUDIO_EXTS = {".mp3", ".m4a", ".aac", ".ogg", ".oga", ".wav", ".flac"}

# ------------------------ App State ----------------------------
app = Flask(__name__)

state_lock = threading.Lock()
listeners: List[queue.Queue] = []  # per-client event queues

# The shared state broadcast to clients
STATE: Dict[str, Any] = {
    "track": None,      # str | None: filename relative to MUSIC_DIR
    "paused": False,    # bool
    "position": 0.0,    # seconds from track start (best-effort)
    "updated": time.time(),
}

# When we switch tracks, we track the server-side start time to estimate position
_track_started_at: Optional[float] = None


def _playlist() -> List[str]:
    files = [f.name for f in MUSIC_DIR.iterdir() if f.is_file() and f.suffix.lower() in AUDIO_EXTS]
    files.sort(key=str.lower)
    return files


def _broadcast(event: Dict[str, Any]) -> None:
    # Push event (as JSON string) to all listener queues
    payload = json.dumps(event)
    stale = []
    for q in listeners:
        try:
            q.put_nowait(payload)
        except Exception:
            stale.append(q)
    # Clean up any broken queues
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
            STATE["position"] = 0.0
            STATE["paused"] = False if paused is None else paused
            _track_started_at = None if STATE["paused"] else time.time()
        if paused is not None and track is None:
            # update paused state without changing track
            if STATE["track"] is None:
                STATE["paused"] = True
            else:
                STATE["paused"] = paused
                if paused:
                    # freeze position
                    if _track_started_at is not None:
                        STATE["position"] += max(0.0, time.time() - _track_started_at)
                        _track_started_at = None
                else:
                    # resume counting from now
                    _track_started_at = time.time()
        if position is not None:
            STATE["position"] = max(0.0, float(position))
            if not STATE.get("paused", False):
                _track_started_at = time.time() - STATE["position"]
        # Update derived position if playing
        if not STATE.get("paused", False) and _track_started_at is not None:
            STATE["position"] = max(0.0, time.time() - _track_started_at)
        STATE["updated"] = time.time()
        event = {"type": "state", **STATE}
    _broadcast(event)
    return STATE.copy()


# ------------------------ Routes: API --------------------------
@app.get("/playlist")
def playlist():
    return jsonify({"files": _playlist()})


@app.get("/media/<path:filename>")
def media(filename: str):
    # Security: ensure the path stays inside MUSIC_DIR
    safe = pathlib.Path(filename).name
    file_path = MUSIC_DIR / safe
    if not file_path.exists() or file_path.suffix.lower() not in AUDIO_EXTS:
        abort(404)
    # Let the browser stream the file
    return send_from_directory(MUSIC_DIR, safe, as_attachment=False)


@app.get("/state")
def get_state():
    # Return the current state snapshot
    with state_lock:
        snapshot = STATE.copy()
        # Update position if currently playing
        if not snapshot.get("paused", False) and _track_started_at is not None:
            snapshot["position"] = max(0.0, time.time() - _track_started_at)
    return jsonify(snapshot)


@app.get("/events")
def sse_events():
    """Server‑Sent Events stream that pushes state updates to listeners."""
    q: queue.Queue = queue.Queue()
    listeners.append(q)

    def stream():
        # Send an initial state immediately
        init = json.dumps({"type": "state", **STATE})
        yield f"event: state\n"
        yield f"data: {init}\n\n"
        try:
            while True:
                payload = q.get()
                yield f"event: state\n"
                yield f"data: {payload}\n\n"
        except GeneratorExit:
            # client disconnected; remove queue
            try:
                listeners.remove(q)
            except ValueError:
                pass

    headers = {"Content-Type": "text/event-stream", "Cache-Control": "no-cache", "Connection": "keep-alive"}
    return Response(stream(), headers=headers)


# ------------------------ Routes: Control ----------------------


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
    if not filename:
        abort(400, "Missing 'track'")
    try:
        new_state = _set_state(track=filename, paused=False)
    except FileNotFoundError:
        abort(404, f"No such track: {filename}")
    return jsonify(new_state)


@app.post("/pause")
def pause():
    if not _check_admin():
        abort(403)
    paused = True
    if request.data:
        body = request.get_json(silent=True) or {}
        if "paused" in body:
            paused = bool(body["paused"])
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


# ------------------------ Pages: UI ----------------------------
INDEX_HTML = r"""
<!doctype html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>🎶 LAN Party — Listener</title>
  <style>
    html, body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background:#0b0f14; color:#e6edf3; margin:0; }
    .wrap { max-width: 760px; margin: 24px auto; padding: 16px; }
    .card { background:#0f1722; border: 1px solid #1f2a3a; border-radius: 16px; padding: 20px; box-shadow: 0 10px 30px rgba(0,0,0,.2); }
    button { font-size: 16px; border-radius: 999px; padding: 12px 18px; border: none; cursor: pointer; background:#2b82ff; color:white; }
    button.secondary { background:#263244; color:#cbd5e1; }
    .row { display:flex; gap:12px; align-items:center; flex-wrap:wrap; }
    .meta { opacity: .8; font-size: 14px; }
    audio { width: 100%; margin-top: 16px; }
    .pill { display:inline-block; padding:4px 10px; border-radius:999px; background:#1c2533; font-size:12px; }
  </style>
</head>
<body>
  <div class="wrap">
    <div class="card">
      <h1>🎶 LAN Music — Listener</h1>
      <p class="meta">Ask the host for the link to <code>/control</code> to change tracks. This page auto‑switches when the host changes the song.</p>
      <div class="row">
        <button id="join">▶️ Join & Play</button>
        <span id="status" class="pill">idle</span>
      </div>
      <h2 id="title">No track selected</h2>
      <audio id="player" controls preload="auto"></audio>
    </div>
  </div>
<script>
const player = document.getElementById('player');
const title = document.getElementById('title');
const statusEl = document.getElementById('status');
const joinBtn = document.getElementById('join');
let joined = false;

joinBtn.addEventListener('click', async () => {
  joined = true;
  statusEl.textContent = 'connected';
  await player.play().catch(()=>{});
});

function setTrack(name, paused, position){
  if(!name){
    title.textContent = 'No track selected';
    player.removeAttribute('src');
    return;
  }
  const cacheBust = Date.now();
  const url = `/media/${encodeURIComponent(name)}?v=${cacheBust}`;
  if (player.src !== location.origin + url) {
    player.src = url;
  }
  title.textContent = name;
  if (typeof position === 'number' && !isNaN(position)) {
    const trySeek = () => { try { player.currentTime = position; } catch(e){} };
    if (player.readyState >= 1) trySeek(); else player.onloadedmetadata = trySeek;
  }
  if (!paused && joined) player.play().catch(()=>{});
  if (paused) player.pause();
}

async function refreshState(){
  const res = await fetch('/state');
  const s = await res.json();
  setTrack(s.track, s.paused, s.position);
}

// Live updates via SSE
const es = new EventSource('/events');
es.addEventListener('state', (e) => {
  try {
    const data = JSON.parse(e.data);
    if (data && data.type === 'state') {
      setTrack(data.track, data.paused, data.position);
    }
  } catch(err){}
});

refreshState();
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
  <title>🎛️ LAN Music — Control</title>
  <style>
    html, body { font-family: system-ui, -apple-system, Segoe UI, Roboto, Helvetica, Arial, sans-serif; background:#0b0f14; color:#e6edf3; margin:0; }
    .wrap { max-width: 920px; margin: 24px auto; padding: 16px; }
    .grid { display:grid; grid-template-columns: repeat(auto-fill, minmax(220px, 1fr)); gap:12px; }
    .card { background:#0f1722; border: 1px solid #1f2a3a; border-radius: 16px; padding: 16px; box-shadow: 0 10px 30px rgba(0,0,0,.2); }
    button { font-size: 14px; border-radius: 999px; padding: 10px 14px; border: none; cursor: pointer; background:#2b82ff; color:white; }
    button.secondary { background:#263244; color:#cbd5e1; }
    .row { display:flex; gap:8px; align-items:center; flex-wrap:wrap; }
    .pill { display:inline-block; padding:4px 10px; border-radius:999px; background:#1c2533; font-size:12px; }
    input[type="range"]{ width: 200px; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>🎛️ Control Panel</h1>
    <p>Click any track to play it for everyone. Listeners auto‑switch within a second.</p>
    <div class="row" style="margin-bottom:12px;">
      <button id="pause">⏸️ Pause</button>
      <button id="resume" class="secondary">▶️ Resume</button>
      <span class="pill" id="now">loading…</span>
    </div>
    <div class="grid" id="grid"></div>
  </div>
<script>
const grid = document.getElementById('grid');
const pill = document.getElementById('now');
const pauseBtn = document.getElementById('pause');
const resumeBtn = document.getElementById('resume');

const adminToken = new URLSearchParams(location.search).get('token') || '';

async function load(){
  const res = await fetch('/playlist');
  const data = await res.json();
  grid.innerHTML='';
  for (const name of data.files){
    const card = document.createElement('div');
    card.className='card';
    const btn = document.createElement('button');
    btn.textContent = '▶️ Play';
    btn.onclick = () => play(name);
    const p = document.createElement('p');
    p.textContent = name;
    card.appendChild(p);
    card.appendChild(btn);
    grid.appendChild(card);
  }
  const s = await (await fetch('/state')).json();
  pill.textContent = s.track ? `Now: ${s.track}${s.paused? ' (paused)':''}` : 'No track';
}

async function play(name){
  const res = await fetch('/play' + (adminToken? `?token=${encodeURIComponent(adminToken)}`:''), {
    method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({track:name})
  });
  const s = await res.json();
  pill.textContent = s.track ? `Now: ${s.track}${s.paused? ' (paused)':''}` : 'No track';
}

pauseBtn.onclick = async () => {
  const res = await fetch('/pause' + (adminToken? `?token=${encodeURIComponent(adminToken)}`:''), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({paused:true})});
  const s = await res.json();
  pill.textContent = s.track ? `Now: ${s.track} (paused)` : 'No track';
};

resumeBtn.onclick = async () => {
  const res = await fetch('/pause' + (adminToken? `?token=${encodeURIComponent(adminToken)}`:''), {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({paused:false})});
  const s = await res.json();
  pill.textContent = s.track ? `Now: ${s.track}` : 'No track';
};

load();
</script>
</body>
</html>
"""


@app.get("/")
def index_page():
    return render_template_string(INDEX_HTML)


@app.get("/control")
def control_page():
    if ADMIN_TOKEN and not _check_admin():
        # If token required but not provided, show a friendly message
        return render_template_string("""
        <html><body style="font-family:system-ui;padding:2rem;background:#0b0f14;color:#e6edf3;">
        <h1>🔒 Control Locked</h1>
        <p>This server requires a token to access <code>/control</code>. Append <code>?token=YOUR_TOKEN</code> to the URL.</p>
        </body></html>
        """)
    return render_template_string(CONTROL_HTML)


# ------------------------ Main -------------------------------
if __name__ == "__main__":
    print("\nLAN Music Server running…")
    print(f"Music folder: {MUSIC_DIR}")
    print(f"Open listener: http://<your-LAN-IP>:{PORT}/")
    print(f"Open control:  http://<your-LAN-IP>:{PORT}/control" + ("?token=YOUR_TOKEN" if ADMIN_TOKEN else ""))
    app.run(host=HOST, port=PORT, debug=False, threaded=True)

