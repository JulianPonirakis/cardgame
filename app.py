import json
import random
import string
from typing import Dict, List, Optional, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse

app = FastAPI()

# -------------------------
# In-memory room state
# -------------------------
# rooms[code] = {
#   "players": [{"id": str, "name": str, "score": int, "hand": List[Tuple[int,str]], "played": Optional[Tuple[int,str]]}],
#   "sockets": set(WebSocket),
#   "socket_to_pid": {WebSocket: player_id},
#   "host_id": str,
#   "phase": "lobby" | "playing" | "finished",
#   "round": int,
#   "last_result": str,
# }
rooms: Dict[str, Dict] = {}

RANKS = list(range(2, 15))  # 11 J, 12 Q, 13 K, 14 A
SUITS = ["♠", "♥", "♦", "♣"]


def make_code(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def make_player_id() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))


def rank_to_str(r: int) -> str:
    if r <= 10:
        return str(r)
    return {11: "J", 12: "Q", 13: "K", 14: "A"}[r]


def card_to_str(card: Tuple[int, str]) -> str:
    r, s = card
    return f"{rank_to_str(r)}{s}"


def new_deck() -> List[Tuple[int, str]]:
    deck = [(r, s) for s in SUITS for r in RANKS]
    random.shuffle(deck)
    return deck


def public_room_state(code: str) -> dict:
    room = rooms[code]
    players_public = []
    for p in room["players"]:
        players_public.append(
            {
                "id": p["id"],
                "name": p["name"],
                "score": p["score"],
                "hand_count": len(p["hand"]),
                "has_played": p["played"] is not None,
            }
        )
    return {
        "type": "state",
        "code": code,
        "phase": room["phase"],
        "round": room["round"],
        "host_id": room["host_id"],
        "players": players_public,
        "last_result": room["last_result"],
    }


async def send_state_to_room(code: str) -> None:
    payload = json.dumps(public_room_state(code))
    dead = []
    for ws in list(rooms[code]["sockets"]):
        try:
            await ws.send_text(payload)
        except Exception:
            dead.append(ws)
    for ws in dead:
        rooms[code]["sockets"].discard(ws)
        rooms[code]["socket_to_pid"].pop(ws, None)


async def send_private_hand(code: str, ws: WebSocket, player_id: str) -> None:
    room = rooms[code]
    player = next((p for p in room["players"] if p["id"] == player_id), None)
    if not player:
        return
    hand_str = [card_to_str(c) for c in player["hand"]]
    msg = {"type": "hand", "hand": hand_str}
    await ws.send_text(json.dumps(msg))


def find_player(room: Dict, player_id: str) -> Optional[Dict]:
    for p in room["players"]:
        if p["id"] == player_id:
            return p
    return None


def all_players_played(room: Dict) -> bool:
    if not room["players"]:
        return False
    return all(p["played"] is not None for p in room["players"])


def resolve_round(room: Dict) -> str:
    # Determine winner by highest rank. If tie for highest, no point.
    played = [(p["name"], p["played"]) for p in room["players"]]
    # played cards are tuples (rank, suit)
    max_rank = max(card[0] for _, card in played if card is not None)
    winners = [name for name, card in played if card is not None and card[0] == max_rank]

    reveal_line = " | ".join(f"{name}: {card_to_str(card)}" for name, card in played if card is not None)

    if len(winners) == 1:
        winner_name = winners[0]
        winner_player = next(p for p in room["players"] if p["name"] == winner_name)
        winner_player["score"] += 1
        result = f"Round {room['round']} — {reveal_line} — Winner: {winner_name} (+1)"
    else:
        result = f"Round {room['round']} — {reveal_line} — Tie for highest. No points."

    # discard played cards (they are gone)
    for p in room["players"]:
        p["played"] = None

    room["round"] += 1

    # check game end
    if all(len(p["hand"]) == 0 for p in room["players"]):
        room["phase"] = "finished"
        # announce final winner(s)
        max_score = max(p["score"] for p in room["players"]) if room["players"] else 0
        champs = [p["name"] for p in room["players"] if p["score"] == max_score]
        if len(champs) == 1:
            room["last_result"] = result + f" — Game over. Champion: {champs[0]} 🏆"
        else:
            room["last_result"] = result + f" — Game over. Tie champions: {', '.join(champs)} 🏆"
    else:
        room["last_result"] = result

    return room["last_result"]


HOME_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Cardgame</title>
    <style>
      body { font-family: Arial, sans-serif; max-width: 820px; margin: 40px auto; line-height: 1.35; }
      .box { border: 1px solid #ddd; padding: 16px; border-radius: 14px; margin-bottom: 16px; }
      input, button, select { padding: 10px; font-size: 16px; }
      button { cursor: pointer; }
      .players { margin-top: 10px; }
      .pill { display: inline-block; padding: 6px 10px; border: 1px solid #ddd; border-radius: 999px; margin: 4px; }
      .code { font-weight: 800; letter-spacing: 1px; }
      .muted { color: #666; }
      .row { display:flex; gap: 10px; flex-wrap: wrap; align-items: center; }
      .spacer { height: 8px; }
      .result { background: #fafafa; border: 1px solid #eee; padding: 10px; border-radius: 12px; }
      .you { font-weight: 700; }
      .btn { border-radius: 10px; border: 1px solid #ccc; background: white; }
      .btnPrimary { border-radius: 10px; border: 1px solid #111; background: #111; color: white; }
      .danger { color: #b00020; }
    </style>
  </head>
  <body>
    <h1>Cardgame</h1>
    <p class="muted">Create a room, share the code, everyone joins. Host starts the game. Each round everyone plays 1 card, highest wins 1 point.</p>

    <div class="box">
      <h2>Create a room</h2>
      <button class="btn" onclick="createRoom()">Create Room</button>
      <p id="created"></p>
    </div>

    <div class="box">
      <h2>Join a room</h2>
      <div class="row">
        <input id="code" placeholder="Room code" />
        <input id="name" placeholder="Your name" />
        <button class="btn" onclick="joinRoom()">Join</button>
      </div>
      <p id="status"></p>

      <div id="room" style="display:none;">
        <div class="spacer"></div>
        <p>Room: <span class="code" id="roomcode"></span> · Phase: <span id="phase"></span> · Round: <span id="round"></span></p>

        <div class="row" id="hostControls" style="display:none;">
          <span class="you">You are the host.</span>
          <button class="btnPrimary" onclick="startGame()">Start game (deal cards)</button>
        </div>

        <div class="spacer"></div>
        <div class="result" id="lastResult" style="display:none;"></div>

        <div class="spacer"></div>
        <h3>Players</h3>
        <div class="players" id="players"></div>

        <div class="spacer"></div>
        <div id="playArea" style="display:none;">
          <h3>Your hand</h3>
          <div class="row">
            <select id="handSelect"></select>
            <button class="btnPrimary" onclick="playCard()">Play card</button>
          </div>
          <p class="muted">Your chosen card stays hidden until everyone plays.</p>
          <p id="playStatus" class="danger"></p>
        </div>
      </div>
    </div>

    <script>
      let ws = null;
      let myId = null;
      let currentCode = null;
      let lastHand = [];

      async function createRoom() {
        const res = await fetch("/create");
        const data = await res.json();
        document.getElementById("created").innerHTML =
          `Room created: <span class="code">${data.code}</span> (share this code)`;
      }

      function renderPlayers(players) {
        const el = document.getElementById("players");
        el.innerHTML = "";
        for (const p of players) {
          const pill = document.createElement("span");
          pill.className = "pill";
          pill.textContent = `${p.name} · ${p.score} pts · ${p.hand_count} cards${p.has_played ? " · played" : ""}`;
          el.appendChild(pill);
        }
      }

      function setHandDropdown(hand) {
        lastHand = hand.slice();
        const sel = document.getElementById("handSelect");
        sel.innerHTML = "";
        for (const c of hand) {
          const opt = document.createElement("option");
          opt.value = c;
          opt.textContent = c;
          sel.appendChild(opt);
        }
      }

      function joinRoom() {
        const code = document.getElementById("code").value.trim().toUpperCase();
        const name = document.getElementById("name").value.trim();
        if (!code || !name) {
          document.getElementById("status").textContent = "Enter room code + name.";
          return;
        }

        document.getElementById("status").textContent = "Connecting...";
        currentCode = code;

        ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws/${code}`);

        ws.onopen = () => {
          ws.send(JSON.stringify({ type: "join", name }));
        };

        ws.onmessage = (ev) => {
          const msg = JSON.parse(ev.data);

          if (msg.type === "error") {
            document.getElementById("status").textContent = msg.message;
            return;
          }

          if (msg.type === "joined") {
            myId = msg.player_id;
            document.getElementById("status").textContent = "Joined!";
            document.getElementById("room").style.display = "block";
            document.getElementById("roomcode").textContent = msg.code;
            return;
          }

          if (msg.type === "hand") {
            setHandDropdown(msg.hand);
            return;
          }

          if (msg.type === "state") {
            document.getElementById("phase").textContent = msg.phase;
            document.getElementById("round").textContent = msg.round;
            renderPlayers(msg.players);

            const isHost = (myId && msg.host_id === myId);
            document.getElementById("hostControls").style.display = isHost ? "flex" : "none";

            if (msg.last_result && msg.last_result.length > 0) {
              const lr = document.getElementById("lastResult");
              lr.style.display = "block";
              lr.textContent = msg.last_result;
            }

            const playArea = document.getElementById("playArea");
            if (msg.phase === "playing") {
              playArea.style.display = "block";
            } else {
              playArea.style.display = "none";
            }
          }
        };

        ws.onclose = () => {
          document.getElementById("status").textContent = "Disconnected.";
        };
      }

      function startGame() {
        if (!ws) return;
        ws.send(JSON.stringify({ type: "start" }));
      }

      function playCard() {
        document.getElementById("playStatus").textContent = "";
        if (!ws) return;
        const sel = document.getElementById("handSelect");
        const card = sel.value;
        if (!card) {
          document.getElementById("playStatus").textContent = "No cards left.";
          return;
        }
        ws.send(JSON.stringify({ type: "play", card }));
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

    rooms[code] = {
        "players": [],
        "sockets": set(),
        "socket_to_pid": {},
        "host_id": "",
        "phase": "lobby",
        "round": 1,
        "last_result": "",
    }
    return {"code": code}


@app.websocket("/ws/{code}")
async def ws_room(websocket: WebSocket, code: str):
    code = code.strip().upper()
    await websocket.accept()

    if code not in rooms:
        await websocket.send_text(json.dumps({"type": "error", "message": "Room not found. Create it first."}))
        await websocket.close()
        return

    room = rooms[code]
    room["sockets"].add(websocket)

    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            msg_type = msg.get("type")

            if msg_type == "join":
                if room["phase"] != "lobby":
                    await websocket.send_text(json.dumps({"type": "error", "message": "Game already started."}))
                    continue

                name = (msg.get("name") or "").strip()
                if not name:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Name required."}))
                    continue

                player_id = make_player_id()
                while any(p["id"] == player_id for p in room["players"]):
                    player_id = make_player_id()

                player = {"id": player_id, "name": name, "score": 0, "hand": [], "played": None}
                room["players"].append(player)
                room["socket_to_pid"][websocket] = player_id

                if not room["host_id"]:
                    room["host_id"] = player_id

                await websocket.send_text(json.dumps({"type": "joined", "code": code, "player_id": player_id}))
                await send_state_to_room(code)

            elif msg_type == "start":
                pid = room["socket_to_pid"].get(websocket)
                if not pid or pid != room["host_id"]:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Only host can start."}))
                    continue

                if len(room["players"]) < 2:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Need at least 2 players."}))
                    continue

                # Deal evenly, discard remainder
                deck = new_deck()
                n = len(room["players"])
                deal_n = (len(deck) // n) * n
                deck = deck[:deal_n]

                # clear old game state
                for p in room["players"]:
                    p["score"] = 0
                    p["hand"] = []
                    p["played"] = None

                # deal round-robin
                for i, card in enumerate(deck):
                    room["players"][i % n]["hand"].append(card)

                room["phase"] = "playing"
                room["round"] = 1
                room["last_result"] = "Game started. Choose a card each round. Highest wins 1 point."

                # send each player's private hand
                for ws in list(room["sockets"]):
                    wpid = room["socket_to_pid"].get(ws)
                    if wpid:
                        await send_private_hand(code, ws, wpid)

                await send_state_to_room(code)

            elif msg_type == "play":
                if room["phase"] != "playing":
                    await websocket.send_text(json.dumps({"type": "error", "message": "Game not in progress."}))
                    continue

                pid = room["socket_to_pid"].get(websocket)
                if not pid:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Not joined."}))
                    continue

                player = find_player(room, pid)
                if not player:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Player not found."}))
                    continue

                if player["played"] is not None:
                    await websocket.send_text(json.dumps({"type": "error", "message": "You already played this round."}))
                    continue

                card_str = (msg.get("card") or "").strip()
                # find that card in hand by its string representation
                idx = None
                for i, c in enumerate(player["hand"]):
                    if card_to_str(c) == card_str:
                        idx = i
                        break
                if idx is None:
                    await websocket.send_text(json.dumps({"type": "error", "message": "That card is not in your hand."}))
                    continue

                card = player["hand"].pop(idx)
                player["played"] = card

                # update this player's private hand after removal
                await send_private_hand(code, websocket, pid)

                # if everyone played, resolve round
                if all_players_played(room):
                    resolve_round(room)

                    # after resolution, if still playing, keep hands as-is
                    # send updated hands (no change except everyone already removed a card)
                    for ws in list(room["sockets"]):
                        wpid = room["socket_to_pid"].get(ws)
                        if wpid:
                            await send_private_hand(code, ws, wpid)

                await send_state_to_room(code)

            else:
                await websocket.send_text(json.dumps({"type": "error", "message": "Unknown message type."}))

    except WebSocketDisconnect:
        room["sockets"].discard(websocket)
        room["socket_to_pid"].pop(websocket, None)
        await send_state_to_room(code)
