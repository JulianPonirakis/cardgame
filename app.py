import json
import random
import string
from typing import Dict

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()

# In-memory rooms (resets if server restarts)
# code -> {"players": [{"name": ...}], "sockets": set(WebSocket)}
rooms: Dict[str, Dict] = {}


def make_code(n: int = 5) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return "".join(random.choice(alphabet) for _ in range(n))


def room_state(code: str) -> dict:
    return {"type": "state", "code": code, "players": rooms[code]["players"]}


HOME_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Cardgame</title>
    <style>
      body { font-family: Arial, sans-serif; max-width: 760px; margin: 40px auto; padding: 0 16px; }
      .box { border: 1px solid #ddd; padding: 16px; border-radius: 12px; margin-bottom: 16px; }
      input, button { padding: 10px; font-size: 16px; }
      input { margin-right: 8px; margin-bottom: 8px; }
      button { cursor: pointer; }
      .players { margin-top: 10px; }
      .pill { display: inline-block; padding: 6px 10px; border: 1px solid #ddd; border-radius: 999px; margin: 4px; }
      .code { font-weight: 700; letter-spacing: 1px; }
      .muted { color: #666; }
      .row { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }
    </style>
  </head>
  <body>
    <h1>Cardgame</h1>
    <p class="muted">Create a room, share the code, then everyone joins. Live player list updates instantly.</p>

    <div class="box">
      <h2>Create a room</h2>
      <button onclick="createRoom()">Create Room</button>
      <p id="created"></p>
    </div>

    <div class="box">
      <h2>Join a room</h2>
      <div class="row">
        <input id="code" placeholder="Room code" />
        <input id="name" placeholder="Your name" />
        <button onclick="joinRoom()">Join</button>
      </div>

      <p id="status" class="muted"></p>

      <div id="room" style="display:none;">
        <p>Room: <span class="code" id="roomcode"></span></p>
        <div class="players" id="players"></div>
      </div>
    </div>

    <script>
      let ws = null;

      async function createRoom() {
        const res = await fetch("/create");
        const data = await res.json();
        document.getElementById("created").innerHTML =
          `Room created: <span class="code">${data.code}</span> (share this code)`;
        // Helpful: auto-fill the join box with the new code
        document.getElementById("code").value = data.code;
      }

      function renderPlayers(players) {
        const el = document.getElementById("players");
        el.innerHTML = "";
        for (const p of players) {
          const pill = document.createElement("span");
          pill.className = "pill";
          pill.textContent = p.name;
          el.appendChild(pill);
        }
      }

      function joinRoom() {
        const code = document.getElementById("code").value.trim().toUpperCase();
        const name = document.getElementById("name").value.trim();

        if (!code || !name) {
          document.getElementById("status").textContent = "Enter room code and name.";
          return;
        }

        document.getElementById("status").textContent = "Connecting...";

        // Close old connection if any
        if (ws) {
          try { ws.close(); } catch (e) {}
          ws = null;
        }

        const scheme = (location.protocol === "https:") ? "wss" : "ws";
        ws = new WebSocket(`${scheme}://${location.host}/ws/${code}`);

        ws.onopen = () => {
          ws.send(JSON.stringify({ type: "join", name }));
        };

        ws.onmessage = (ev) => {
          const msg = JSON.parse(ev.data);

          if (msg.type === "error") {
            document.getElementById("status").textContent = msg.message;
            return;
          }

          if (msg.type === "state") {
            document.getElementById("status").textContent = "Joined!";
            document.getElementById("room").style.display = "block";
            document.getElementById("roomcode").textContent = msg.code;
            renderPlayers(msg.players);
          }
        };

        ws.onclose = () => {
          document.getElementById("status").textContent = "Disconnected.";
        };

        ws.onerror = () => {
          document.getElementById("status").textContent = "Connection error.";
        };
      }
    </script>
  </body>
</html>
"""


@app.get("/")
def home():
    return HTMLResponse(HOME_HTML)


@app.get("/create")
def create():
    code = make_code()
    while code in rooms:
        code = make_code()

    rooms[code] = {"players": [], "sockets": set()}
    return {"code": code}


@app.websocket("/ws/{code}")
async def ws_room(websocket: WebSocket, code: str):
    code = code.strip().upper()
    await websocket.accept()

    if code not in rooms:
        await websocket.send_text(json.dumps({"type": "error", "message": "Room not found. Create it first."}))
        await websocket.close()
        return

    rooms[code]["sockets"].add(websocket)

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)

            if msg.get("type") == "join":
                name = (msg.get("name") or "").strip()
                if not name:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Name required."}))
                    continue

                rooms[code]["players"].append({"name": name})

                # broadcast updated state to everyone in the room
                payload = json.dumps(room_state(code))
                dead = []
                for ws in list(rooms[code]["sockets"]):
                    try:
                        await ws.send_text(payload)
                    except Exception:
                        dead.append(ws)

                for ws in dead:
                    rooms[code]["sockets"].discard(ws)

    except WebSocketDisconnect:
        rooms[code]["sockets"].discard(websocket)
