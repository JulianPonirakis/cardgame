import asyncio
import json
import random
import string
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()


# -------------------------
# Card + Game helpers
# -------------------------

SUITS = ["S", "H", "D", "C"]  # Spades, Hearts, Diamonds, Clubs
SUIT_SYMBOL = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
# UI color hint (handled in CSS)
SUIT_COLOR = {"S": "black", "C": "black", "H": "red", "D": "red"}

RANKS = list(range(2, 15))  # 11 J, 12 Q, 13 K, 14 A
RANK_LABEL = {
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    8: "8",
    9: "9",
    10: "10",
    11: "J",
    12: "Q",
    13: "K",
    14: "A",
}


def make_code(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def new_deck() -> List[dict]:
    deck = []
    for s in SUITS:
        for r in RANKS:
            deck.append(
                {
                    "id": f"{s}-{r}",
                    "suit": s,
                    "rank": r,
                    "label": RANK_LABEL[r],
                    "symbol": SUIT_SYMBOL[s],
                    "color": SUIT_COLOR[s],
                }
            )
    random.shuffle(deck)
    return deck


def rank_value(card: dict) -> int:
    return int(card["rank"])


@dataclass
class Player:
    id: str
    name: str
    is_bot: bool = False
    score: int = 0
    hand: List[dict] = field(default_factory=list)
    locked_card_id: Optional[str] = None  # chosen for current round


@dataclass
class Room:
    code: str
    target_size: int = 4  # variable 2-6
    phase: str = "lobby"  # lobby | playing | finished
    round: int = 0
    players: List[Player] = field(default_factory=list)

    # Reveal info for UI
    last_reveal: Optional[dict] = None  # {"plays":[{playerId,card}], "winnerId":..., "explosion":bool}

    # Websocket connections
    sockets: Set[WebSocket] = field(default_factory=set)
    socket_to_player: Dict[WebSocket, str] = field(default_factory=dict)

    # Guard to avoid double-start
    starting: bool = False


rooms: Dict[str, Room] = {}


def find_player(room: Room, player_id: str) -> Optional[Player]:
    for p in room.players:
        if p.id == player_id:
            return p
    return None


def human_players(room: Room) -> List[Player]:
    return [p for p in room.players if not p.is_bot]


def bot_players(room: Room) -> List[Player]:
    return [p for p in room.players if p.is_bot]


def make_player_id() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))


def room_public_players(room: Room) -> List[dict]:
    return [{"id": p.id, "name": p.name, "isBot": p.is_bot, "score": p.score} for p in room.players]


def deal_equally(room: Room) -> None:
    deck = new_deck()
    n = len(room.players)
    if n <= 0:
        return

    each = len(deck) // n
    total = each * n
    deck = deck[:total]  # discard leftovers

    for p in room.players:
        p.hand = []
        p.locked_card_id = None

    i = 0
    for p in room.players:
        p.hand = deck[i : i + each]
        i += each


def all_locked(room: Room) -> bool:
    return all(p.locked_card_id is not None for p in room.players)


def current_plays(room: Room) -> List[Tuple[Player, dict]]:
    plays: List[Tuple[Player, dict]] = []
    for p in room.players:
        if p.locked_card_id is None:
            continue
        card = next((c for c in p.hand if c["id"] == p.locked_card_id), None)
        # Note: we remove from hand at lock-time; so it might not be in hand anymore.
        # We store the card object in last_reveal; so here we fall back if needed.
        if card is None and room.last_reveal:
            for x in room.last_reveal.get("plays", []):
                if x["playerId"] == p.id:
                    card = x["card"]
        if card is not None:
            plays.append((p, card))
    return plays


async def broadcast_state(room: Room) -> None:
    dead = []
    for ws in list(room.sockets):
        try:
            pid = room.socket_to_player.get(ws)
            you = find_player(room, pid) if pid else None

            payload = {
                "type": "state",
                "code": room.code,
                "phase": room.phase,
                "round": room.round,
                "targetSize": room.target_size,
                "players": room_public_players(room),
                "lastReveal": room.last_reveal,
                "you": {
                    "id": you.id if you else None,
                    "name": you.name if you else None,
                    "score": you.score if you else None,
                    "isBot": you.is_bot if you else None,
                    "lockedCardId": you.locked_card_id if you else None,
                    "hand": you.hand if you else [],
                },
            }
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)

    for ws in dead:
        room.sockets.discard(ws)
        room.socket_to_player.pop(ws, None)


async def bot_lock_after_delay(room: Room, bot: Player, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    if room.phase != "playing":
        return
    if bot.locked_card_id is not None:
        return
    if not bot.hand:
        return

    # Simple bot strategy: mix of conservative + random + "pressure" (score difference)
    scores = [p.score for p in room.players]
    leader = max(scores) if scores else 0
    trailing_by = leader - bot.score

    # Choose card index biased by pressure:
    # ahead -> lower card, behind -> higher card, plus randomness.
    bot.hand.sort(key=lambda c: c["rank"])
    if trailing_by >= 2:
        # push higher
        idx = int(random.uniform(0.65, 1.0) * (len(bot.hand) - 1))
    elif trailing_by <= 0:
        # coast
        idx = int(random.uniform(0.0, 0.45) * (len(bot.hand) - 1))
    else:
        idx = int(random.uniform(0.25, 0.75) * (len(bot.hand) - 1))

    idx = max(0, min(idx, len(bot.hand) - 1))
    chosen = bot.hand.pop(idx)
    bot.locked_card_id = chosen["id"]

    # We stash played cards in last_reveal only at reveal-time,
    # but we need the card object to show the table; store it temporarily
    # by adding it into a hidden per-room buffer inside last_reveal draft.
    if room.last_reveal is None or room.last_reveal.get("pendingRound") != room.round:
        room.last_reveal = {"pendingRound": room.round, "plays": []}
    room.last_reveal["plays"].append({"playerId": bot.id, "card": chosen})

    await maybe_reveal_and_advance(room)
    await broadcast_state(room)


async def start_game_if_ready(room: Room) -> None:
    if room.phase != "lobby":
        return
    if room.starting:
        return
    if len(room.players) < 2:
        return

    room.starting = True
    await broadcast_state(room)
    await asyncio.sleep(0.6)

    room.phase = "playing"
    room.round = 1
    room.last_reveal = None

    deal_equally(room)

    # schedule bots to pick for round 1 (they can also wait until humans lock; this feels more alive)
    for b in bot_players(room):
        asyncio.create_task(bot_lock_after_delay(room, b, delay_s=random.uniform(0.8, 2.0)))

    await broadcast_state(room)
    room.starting = False


def ensure_bot_fill(room: Room) -> None:
    # We always want at least 2 total players for a playable round.
    # And we fill up to target_size with bots so the room feels like a table.
    target = max(2, min(6, room.target_size))

    # Keep existing humans; fill remaining seats with bots
    while len(room.players) < target:
        bot = Player(id=make_player_id(), name=f"Bot {len(bot_players(room)) + 1}", is_bot=True)
        room.players.append(bot)


async def maybe_reveal_and_advance(room: Room) -> None:
    if room.phase != "playing":
        return
    if not all_locked(room):
        return

    # Collect played cards.
    # Some were popped from hand at lock time for humans; for safety we use a reveal list we build now.
    plays = []
    card_by_player: Dict[str, dict] = {}

    # If last_reveal has pending plays (from bots), use that
    pending = {}
    if room.last_reveal and room.last_reveal.get("pendingRound") == room.round:
        for x in room.last_reveal.get("plays", []):
            pending[x["playerId"]] = x["card"]

    for p in room.players:
        if p.locked_card_id is None:
            continue
        card = pending.get(p.id)
        if card is None:
            # Human lock path stores card by removing from hand and returning the object in that moment.
            # But if we didn't stash it (we will), we can't reconstruct from hand.
            # So: we store it at lock-time for humans too.
            # If missing, just show a placeholder.
            card = {"id": p.locked_card_id, "suit": "S", "rank": 2, "label": "?", "symbol": "?", "color": "black"}
        plays.append({"playerId": p.id, "card": card})
        card_by_player[p.id] = card

    # Determine highest
    values = [(pid, rank_value(card)) for pid, card in card_by_player.items()]
    max_val = max(v for _, v in values)
    top = [pid for pid, v in values if v == max_val]

    explosion = len(top) > 1
    winner_id = None

    if not explosion:
        winner_id = top[0]
        winner = find_player(room, winner_id)
        if winner:
            winner.score += 1

    room.last_reveal = {"plays": plays, "winnerId": winner_id, "explosion": explosion}

    await broadcast_state(room)

    # Pause so the table can "breathe" and show who won / explosion
    await asyncio.sleep(1.2)

    # Advance to next round or finish
    # Remove locked marks and ensure hands shrink (cards already popped at lock-time).
    for p in room.players:
        p.locked_card_id = None

    still_has_cards = any(len(p.hand) > 0 for p in room.players)

    if not still_has_cards:
        room.phase = "finished"
        await broadcast_state(room)
        return

    room.round += 1
    room.last_reveal = None

    # bots pick for new round
    for b in bot_players(room):
        asyncio.create_task(bot_lock_after_delay(room, b, delay_s=random.uniform(0.8, 2.0)))

    await broadcast_state(room)


# -------------------------
# HTML UI
# -------------------------

HOME_HTML = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>Cardgame</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      :root {
        --bg: #ffffff;
        --text: #111;
        --muted: #666;
        --border: #e6e6e6;
        --card: #ffffff;
        --shadow: 0 10px 30px rgba(0,0,0,0.08);
        --radius: 18px;
      }

      body {
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
        background: var(--bg);
        color: var(--text);
        max-width: 1000px;
        margin: 30px auto;
        padding: 0 16px;
      }

      h1 { font-size: 42px; margin: 0 0 8px; }
      .sub { color: var(--muted); margin: 0 0 22px; }

      .grid {
        display: grid;
        grid-template-columns: 1fr;
        gap: 16px;
      }

      @media (min-width: 900px) {
        .grid { grid-template-columns: 1fr 1fr; }
      }

      .box {
        border: 1px solid var(--border);
        border-radius: var(--radius);
        padding: 18px;
        box-shadow: var(--shadow);
        background: #fff;
      }

      input, button, select {
        padding: 10px 12px;
        font-size: 16px;
        border-radius: 12px;
        border: 1px solid var(--border);
        background: #fff;
        color: var(--text);
      }

      button {
        cursor: pointer;
        border: 1px solid #111;
      }

      button:disabled {
        opacity: 0.5;
        cursor: not-allowed;
      }

      .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }

      .pill {
        display: inline-flex;
        gap: 8px;
        align-items: center;
        border: 1px solid var(--border);
        border-radius: 999px;
        padding: 8px 12px;
        margin: 6px 6px 0 0;
        background: #fafafa;
      }

      .code {
        font-weight: 800;
        letter-spacing: 1px;
      }

      .status { margin-top: 10px; color: var(--muted); min-height: 20px; }

      .table {
        margin-top: 14px;
        padding-top: 14px;
        border-top: 1px dashed var(--border);
      }

      .cards {
        display: flex;
        gap: 10px;
        flex-wrap: wrap;
        margin-top: 12px;
      }

      .card {
        width: 74px;
        height: 104px;
        border-radius: 14px;
        border: 1px solid var(--border);
        background: var(--card);
        box-shadow: 0 6px 16px rgba(0,0,0,0.06);
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        padding: 10px;
        user-select: none;
        transition: transform 0.08s ease;
      }

      .card:hover { transform: translateY(-2px); }
      .card.locked { outline: 3px solid #111; }
      .card.played { opacity: 0.6; }

      .corner { font-weight: 900; font-size: 18px; }
      .suit { font-size: 24px; align-self: flex-end; }

      .red { color: #c1121f; }
      .black { color: #111; }

      .big {
        font-size: 22px;
        font-weight: 900;
      }

      .winner {
        margin-top: 10px;
        font-weight: 800;
      }

      .explosion {
        margin-top: 10px;
        font-weight: 900;
      }
    </style>
  </head>

  <body>
    <h1>Cardgame</h1>
    <p class="sub">Create a room, share the code, then everyone locks a card. Reveal happens together. Highest wins a point. Tie = explosion (no point).</p>

    <div class="grid">
      <div class="box">
        <h2>Create a room</h2>
        <div class="row">
          <label>Table size</label>
          <select id="size">
            <option value="2">2</option>
            <option value="3">3</option>
            <option value="4" selected>4</option>
            <option value="5">5</option>
            <option value="6">6</option>
          </select>
          <button onclick="createRoom()">Create Room</button>
        </div>
        <div class="status" id="created"></div>
        <div class="status" id="createdUrl"></div>
      </div>

      <div class="box">
        <h2>Join a room</h2>
        <div class="row">
          <input id="code" placeholder="Room code" />
          <input id="name" placeholder="Your name" />
          <button onclick="joinRoom()">Join</button>
        </div>

        <div class="status" id="status"></div>

        <div id="game" style="display:none;">
          <div class="row" style="margin-top:10px;">
            <div class="pill">Room: <span class="code" id="roomcode"></span></div>
            <div class="pill">Round: <span id="round">-</span></div>
            <div class="pill">Phase: <span id="phase">-</span></div>
          </div>

          <div style="margin-top:12px;">
            <div class="big">Players</div>
            <div id="players"></div>
          </div>

          <div class="table">
            <div class="big">Table (reveals)</div>
            <div id="tablePlays" class="cards"></div>
            <div id="resultText"></div>
          </div>

          <div class="table">
            <div class="big">Your hand</div>
            <div class="status" id="lockHint"></div>
            <div id="hand" class="cards"></div>
          </div>
        </div>
      </div>
    </div>

    <script>
      let ws = null;
      let youId = null;
      let lastState = null;

      function byId(x){ return document.getElementById(x); }

      function cardEl(card, clickable, locked) {
        const el = document.createElement("div");
        el.className = "card " + (card.color === "red" ? "red" : "black");
        if (locked) el.classList.add("locked");
        el.innerHTML = `
          <div class="corner">${card.label}</div>
          <div class="suit">${card.symbol}</div>
        `;
        if (clickable) {
          el.style.cursor = "pointer";
          el.onclick = () => lockCard(card.id);
        } else {
          el.style.cursor = "default";
        }
        return el;
      }

      async function createRoom() {
        const size = parseInt(byId("size").value, 10);
        const res = await fetch(`/create?size=${size}`);
        const data = await res.json();
        byId("created").innerHTML = `Room created: <span class="code">${data.code}</span> (table size ${data.targetSize})`;
        byId("createdUrl").textContent = `Share link: ${location.origin}/?code=${data.code}`;
        byId("code").value = data.code;
      }

      function renderPlayers(players) {
        const el = byId("players");
        el.innerHTML = "";
        for (const p of players) {
          const pill = document.createElement("div");
          pill.className = "pill";
          const bot = p.isBot ? "🤖" : "🧑";
          pill.textContent = `${bot} ${p.name} — ${p.score} pts`;
          el.appendChild(pill);
        }
      }

      function renderTable(lastReveal, playersById) {
        const el = byId("tablePlays");
        const rt = byId("resultText");
        el.innerHTML = "";
        rt.innerHTML = "";

        if (!lastReveal) {
          rt.innerHTML = `<div class="status">No reveal yet. Lock a card to play.</div>`;
          return;
        }

        const plays = lastReveal.plays || [];
        for (const x of plays) {
          const player = playersById[x.playerId];
          const wrap = document.createElement("div");
          wrap.style.display = "flex";
          wrap.style.flexDirection = "column";
          wrap.style.gap = "6px";
          wrap.style.alignItems = "center";

          const name = document.createElement("div");
          name.style.fontSize = "12px";
          name.style.color = "#666";
          name.textContent = player ? player.name : x.playerId;

          wrap.appendChild(name);
          wrap.appendChild(cardEl(x.card, false, false));
          el.appendChild(wrap);
        }

        if (lastReveal.explosion) {
          rt.innerHTML = `<div class="explosion">💥 Explosion! Tie for highest card. No point awarded.</div>`;
        } else {
          const winner = playersById[lastReveal.winnerId];
          rt.innerHTML = `<div class="winner">🏆 ${winner ? winner.name : "Winner"} takes the point.</div>`;
        }
      }

      function renderHand(hand, lockedCardId, phase) {
        const el = byId("hand");
        el.innerHTML = "";

        const canPlay = phase === "playing" && !lockedCardId;

        for (const c of hand) {
          const locked = lockedCardId === c.id;
          el.appendChild(cardEl(c, canPlay, locked));
        }

        const hint = byId("lockHint");
        if (phase === "lobby") hint.textContent = "Waiting for players… bots will fill seats automatically.";
        else if (phase === "finished") hint.textContent = "Game finished. Refresh to start again (new room recommended).";
        else if (lockedCardId) hint.textContent = "Locked in. Waiting for reveal…";
        else hint.textContent = "Pick one card to lock in. Reveal happens when everyone has locked.";
      }

      function lockCard(cardId) {
        if (!ws) return;
        if (!lastState) return;
        if (lastState.phase !== "playing") return;
        if (lastState.you.lockedCardId) return;

        ws.send(JSON.stringify({ type: "lock", cardId }));
      }

      function joinRoom() {
        const code = byId("code").value.trim().toUpperCase();
        const name = byId("name").value.trim();
        if (!code || !name) {
          byId("status").textContent = "Enter room code + name.";
          return;
        }

        byId("status").textContent = "Connecting…";
        const proto = location.protocol === "https:" ? "wss" : "ws";
        ws = new WebSocket(`${proto}://${location.host}/ws/${code}`);

        ws.onopen = () => ws.send(JSON.stringify({ type: "join", name }));

        ws.onmessage = (ev) => {
          const msg = JSON.parse(ev.data);

          if (msg.type === "error") {
            byId("status").textContent = msg.message;
            return;
          }

          if (msg.type === "hello") {
            youId = msg.playerId;
            byId("status").textContent = "Joined. Loading game…";
            byId("game").style.display = "block";
            byId("roomcode").textContent = code;
            return;
          }

          if (msg.type === "state") {
            lastState = msg;
            byId("phase").textContent = msg.phase;
            byId("round").textContent = msg.round;

            const playersById = {};
            for (const p of msg.players) playersById[p.id] = p;

            renderPlayers(msg.players);
            renderTable(msg.lastReveal, playersById);
            renderHand(msg.you.hand || [], msg.you.lockedCardId, msg.phase);

            byId("status").textContent = "";
          }
        };

        ws.onclose = () => {
          byId("status").textContent = "Disconnected.";
        };
      }

      // Convenience: if you share ?code=XXXXX
      const params = new URLSearchParams(location.search);
      if (params.get("code")) {
        byId("code").value = params.get("code").toUpperCase();
      }
    </script>
  </body>
</html>
"""


# -------------------------
# Routes
# -------------------------

@app.get("/")
def home():
    return HTMLResponse(HOME_HTML)


@app.get("/create")
def create(size: int = 4):
    size = max(2, min(6, int(size)))
    code = make_code()
    while code in rooms:
        code = make_code()

    room = Room(code=code, target_size=size)
    rooms[code] = room

    return JSONResponse({"code": code, "targetSize": size})


@app.websocket("/ws/{code}")
async def ws_room(websocket: WebSocket, code: str):
    code = code.strip().upper()
    await websocket.accept()

    if code not in rooms:
        await websocket.send_text(json.dumps({"type": "error", "message": "Room not found. Create it first."}))
        await websocket.close()
        return

    room = rooms[code]
    room.sockets.add(websocket)

    player_id: Optional[str] = None

    try:
        while True:
            raw = await websocket.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            if mtype == "join":
                name = (msg.get("name") or "").strip()
                if not name:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Name required."}))
                    continue

                # If this socket already joined, ignore
                if player_id is not None:
                    continue

                player_id = make_player_id()
                player = Player(id=player_id, name=name, is_bot=False)
                room.players.append(player)

                # Fill bots to table size (and ensure at least 2 players for solo)
                ensure_bot_fill(room)

                room.socket_to_player[websocket] = player_id

                await websocket.send_text(json.dumps({"type": "hello", "playerId": player_id}))
                await broadcast_state(room)

                # Auto-start when ready
                asyncio.create_task(start_game_if_ready(room))
                continue

            if player_id is None:
                await websocket.send_text(json.dumps({"type": "error", "message": "Join first."}))
                continue

            if mtype == "lock":
                if room.phase != "playing":
                    continue

                you = find_player(room, player_id)
                if not you:
                    continue

                if you.locked_card_id is not None:
                    continue  # already locked

                card_id = (msg.get("cardId") or "").strip()
                if not card_id:
                    continue

                # Card must be in your hand
                idx = next((i for i, c in enumerate(you.hand) if c["id"] == card_id), None)
                if idx is None:
                    continue

                chosen = you.hand.pop(idx)
                you.locked_card_id = chosen["id"]

                # Stash chosen for reveal UI
                if room.last_reveal is None or room.last_reveal.get("pendingRound") != room.round:
                    room.last_reveal = {"pendingRound": room.round, "plays": []}
                room.last_reveal["plays"].append({"playerId": you.id, "card": chosen})

                # If bots haven't locked yet, they will soon; but if humans lock fast, speed bots up a bit
                for b in bot_players(room):
                    if b.locked_card_id is None:
                        asyncio.create_task(bot_lock_after_delay(room, b, delay_s=random.uniform(0.2, 0.8)))

                await broadcast_state(room)
                await maybe_reveal_and_advance(room)
                continue

    except WebSocketDisconnect:
        pass
    finally:
        room.sockets.discard(websocket)
        room.socket_to_player.pop(websocket, None)

        # Remove the human player if they disconnect
        if player_id:
            room.players = [p for p in room.players if p.id != player_id]

            # If game was running and now fewer than 2 players remain, end it
            if room.phase == "playing" and len(room.players) < 2:
                room.phase = "finished"

            # Re-fill with bots to keep the table populated (optional, but keeps rooms stable)
            if room.phase == "lobby":
                ensure_bot_fill(room)

        await broadcast_state(room)
