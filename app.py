import asyncio
import json
import random
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

# -------------------------
# Card helpers
# -------------------------

SUITS = ["S", "H", "D", "C"]  # Spades, Hearts, Diamonds, Clubs
SUIT_SYMBOL = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
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


def make_player_id() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))


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


# -------------------------
# Game state
# -------------------------

@dataclass
class Player:
    id: str
    name: str
    is_bot: bool = False
    score: int = 0
    hand: List[dict] = field(default_factory=list)
    locked: bool = False  # locked for current round


@dataclass
class Room:
    code: str
    target_size: int = 4
    phase: str = "lobby"  # lobby | lock_in | reveal | finished
    round: int = 0  # current round number (starts at 1)
    rounds_played: int = 0  # total rounds completed

    players: List[Player] = field(default_factory=list)

    # Hidden selections (never broadcast during lock_in)
    pending_plays: Dict[str, dict] = field(default_factory=dict)  # playerId -> card

    # Reveal payload broadcast only when phase == reveal
    last_reveal: Optional[dict] = None  # {"round":int,"order":[ids],"plays":[{playerId,card}], "winnerId":..., "explosion":bool}

    sockets: Set[WebSocket] = field(default_factory=set)
    socket_to_player: Dict[WebSocket, str] = field(default_factory=dict)

    starting: bool = False
    advancing: bool = False


rooms: Dict[str, Room] = {}


def find_player(room: Room, player_id: str) -> Optional[Player]:
    for p in room.players:
        if p.id == player_id:
            return p
    return None


def bot_players(room: Room) -> List[Player]:
    return [p for p in room.players if p.is_bot]


def ensure_bot_fill(room: Room) -> None:
    target = max(2, min(6, room.target_size))
    while len(room.players) < target:
        bot = Player(id=make_player_id(), name=f"Bot {len([b for b in room.players if b.is_bot]) + 1}", is_bot=True)
        room.players.append(bot)


def deal_equally(room: Room) -> None:
    deck = new_deck()
    n = len(room.players)
    if n <= 0:
        return

    each = len(deck) // n
    deck = deck[: each * n]

    for p in room.players:
        p.hand = []
        p.locked = False

    i = 0
    for p in room.players:
        p.hand = deck[i : i + each]
        i += each


def all_locked(room: Room) -> bool:
    return all(p.locked for p in room.players)


def room_public_players(room: Room) -> List[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "isBot": p.is_bot,
            "score": p.score,
            "locked": p.locked,
            "cardsLeft": len(p.hand),
        }
        for p in room.players
    ]


async def broadcast_state(room: Room) -> None:
    # Only show lastReveal during reveal phase (or finished)
    visible_reveal = room.last_reveal if room.phase in ("reveal", "finished") else None

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
                "roundsPlayed": room.rounds_played,
                "targetSize": room.target_size,
                "players": room_public_players(room),
                "lastReveal": visible_reveal,
                "you": {
                    "id": you.id if you else None,
                    "name": you.name if you else None,
                    "score": you.score if you else None,
                    "isBot": you.is_bot if you else None,
                    "locked": you.locked if you else None,
                    "hand": you.hand if you else [],
                },
            }
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)

    for ws in dead:
        room.sockets.discard(ws)
        room.socket_to_player.pop(ws, None)


async def bot_choose(room: Room, bot: Player) -> None:
    if room.phase != "lock_in":
        return
    if bot.locked:
        return
    if not bot.hand:
        return

    # Tiny strategy: if trailing, lean higher; if leading, lean lower; with randomness.
    scores = [p.score for p in room.players]
    leader = max(scores) if scores else 0
    trailing_by = leader - bot.score

    sorted_hand = sorted(bot.hand, key=lambda c: c["rank"])
    n = len(sorted_hand)

    if trailing_by >= 2:
        idx = int(random.uniform(0.65, 1.0) * (n - 1))
    elif trailing_by <= 0:
        idx = int(random.uniform(0.0, 0.45) * (n - 1))
    else:
        idx = int(random.uniform(0.25, 0.75) * (n - 1))

    idx = max(0, min(idx, n - 1))
    chosen = sorted_hand[idx]

    # Remove chosen from bot hand
    for i, c in enumerate(bot.hand):
        if c["id"] == chosen["id"]:
            bot.hand.pop(i)
            break

    room.pending_plays[bot.id] = chosen
    bot.locked = True


async def schedule_bots(room: Room) -> None:
    # Bots lock with staggered delays so it feels alive,
    # but their cards will NOT appear anywhere until reveal.
    for b in bot_players(room):
        delay = random.uniform(0.7, 2.0)
        asyncio.create_task(bot_lock_after_delay(room, b, delay))


async def bot_lock_after_delay(room: Room, bot: Player, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    if room.phase != "lock_in":
        return
    if bot.locked:
        return
    await bot_choose(room, bot)
    await broadcast_state(room)
    await maybe_start_reveal(room)


async def start_game(room: Room) -> None:
    if room.phase != "lobby":
        return
    if room.starting:
        return
    if len(room.players) < 2:
        return

    room.starting = True
    await broadcast_state(room)
    await asyncio.sleep(0.25)

    room.round = 1
    room.rounds_played = 0
    room.phase = "lock_in"
    room.pending_plays = {}
    room.last_reveal = None

    deal_equally(room)
    await schedule_bots(room)

    room.starting = False
    await broadcast_state(room)


async def maybe_start_reveal(room: Room) -> None:
    if room.phase != "lock_in":
        return
    if not all_locked(room):
        return
    if room.advancing:
        return

    room.advancing = True
    await do_reveal_and_advance(room)
    room.advancing = False


async def do_reveal_and_advance(room: Room) -> None:
    # Phase: reveal
    room.phase = "reveal"

    # Build reveal order (stable + fun)
    order = [p.id for p in room.players]
    random.shuffle(order)

    plays = []
    for pid in order:
        card = room.pending_plays.get(pid)
        if card is None:
            # Shouldn't happen if all_locked is true, but keep safe.
            card = {"id": "X", "suit": "S", "rank": 2, "label": "?", "symbol": "?", "color": "black"}
        plays.append({"playerId": pid, "card": card})

    # Determine winner (highest rank). Tie = explosion (no point).
    values = [(x["playerId"], rank_value(x["card"])) for x in plays]
    max_val = max(v for _, v in values) if values else 0
    top = [pid for pid, v in values if v == max_val]

    explosion = len(top) > 1
    winner_id = None
    if not explosion and top:
        winner_id = top[0]
        w = find_player(room, winner_id)
        if w:
            w.score += 1

    room.last_reveal = {
        "round": room.round,
        "order": order,
        "plays": plays,
        "winnerId": winner_id,
        "explosion": explosion,
    }

    await broadcast_state(room)

    # Let the client animation breathe:
    # base 1.8s + 0.55s per card feels good
    wait_s = 1.8 + 0.55 * max(1, len(room.players))
    await asyncio.sleep(wait_s)

    room.rounds_played += 1

    # Prepare next round or finish
    for p in room.players:
        p.locked = False

    room.pending_plays = {}
    room.last_reveal = None

    still_has_cards = any(len(p.hand) > 0 for p in room.players)
    if not still_has_cards:
        room.phase = "finished"
        await broadcast_state(room)
        return

    room.round += 1
    room.phase = "lock_in"
    await schedule_bots(room)
    await broadcast_state(room)


# -------------------------
# HTML UI (single file)
# -------------------------

HOME_HTML = r"""
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
        max-width: 1100px;
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

      .code { font-weight: 800; letter-spacing: 1px; }
      .status { margin-top: 10px; color: var(--muted); min-height: 20px; }

      .split {
        display: grid;
        grid-template-columns: 1fr;
        gap: 16px;
        margin-top: 14px;
      }
      @media (min-width: 900px) {
        .split { grid-template-columns: 360px 1fr; }
      }

      .section {
        border: 1px solid var(--border);
        border-radius: var(--radius);
        padding: 16px;
        box-shadow: var(--shadow);
        background: #fff;
      }

      .big {
        font-size: 18px;
        font-weight: 900;
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
        transition: transform 0.08s ease, outline 0.08s ease;
      }

      .card:hover { transform: translateY(-2px); }
      .card.selected { outline: 3px solid #111; }
      .card.back {
        background: linear-gradient(135deg, #111 0%, #333 100%);
        border-color: #111;
      }

      .corner { font-weight: 900; font-size: 18px; }
      .suit { font-size: 24px; align-self: flex-end; }

      .red { color: #c1121f; }
      .black { color: #111; }

      .winner {
        margin-top: 10px;
        font-weight: 900;
      }

      .explosion {
        margin-top: 10px;
        font-weight: 900;
      }

      .muted { color: var(--muted); }

      .hide { display: none !important; }
    </style>
  </head>

  <body>
    <h1>Cardgame</h1>
    <p class="sub">Create a room, share the code, then everyone locks a card. Reveal happens together. Highest wins a point. Tie = explosion (no point).</p>

    <!-- LOBBY -->
    <div id="lobby">
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
        </div>
      </div>
    </div>

    <!-- GAME -->
    <div id="game" class="hide">
      <div class="row" style="margin-top:10px;">
        <div class="pill">Room: <span class="code" id="roomcode"></span></div>
        <div class="pill">Round: <span id="round">-</span></div>
        <div class="pill">Rounds played: <span id="roundsPlayed">0</span></div>
        <div class="pill">Phase: <span id="phase">-</span></div>
        <button onclick="copyRoom()">Copy room code</button>
      </div>

      <div class="split">
        <div class="section">
          <div class="big">Scoreboard</div>
          <div class="status muted" id="lockStatus"></div>
          <div id="players"></div>
        </div>

        <div class="section">
          <div class="big">Table</div>
          <div class="status muted" id="tableHint"></div>
          <div id="tablePlays" class="cards"></div>
          <div id="resultText"></div>

          <div style="margin-top:16px; border-top:1px dashed var(--border); padding-top:16px;">
            <div class="big">Your hand</div>
            <div class="status muted" id="handHint"></div>
            <div id="hand" class="cards"></div>
          </div>
        </div>
      </div>
    </div>

    <script>
      let ws = null;
      let lastState = null;
      let selectedCardId = null;

      function byId(x){ return document.getElementById(x); }

      function cardEl(card, clickable, selected) {
        const el = document.createElement("div");
        el.className = "card " + (card.color === "red" ? "red" : "black");
        if (selected) el.classList.add("selected");

        el.innerHTML = `
          <div class="corner">${card.label}</div>
          <div class="suit">${card.symbol}</div>
        `;

        if (clickable) {
          el.style.cursor = "pointer";
          el.onclick = () => {
            selectedCardId = card.id;
            renderHand(lastState.you.hand || [], lastState.you.locked, lastState.phase);
          };
        } else {
          el.style.cursor = "default";
        }
        return el;
      }

      function cardBackEl() {
        const el = document.createElement("div");
        el.className = "card back";
        el.innerHTML = `<div></div><div></div>`;
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

      function copyRoom(){
        const code = byId("roomcode").textContent.trim();
        if (!code) return;
        navigator.clipboard.writeText(code);
      }

      function renderPlayers(players) {
        const el = byId("players");
        el.innerHTML = "";

        // Sort by score desc
        const sorted = [...players].sort((a,b) => b.score - a.score);

        for (const p of sorted) {
          const pill = document.createElement("div");
          pill.className = "pill";
          const icon = p.isBot ? "🤖" : "🧑";
          const status = p.locked ? "Locked" : "Choosing…";
          pill.textContent = `${icon} ${p.name} — ${p.score} pts — ${status}`;
          el.appendChild(pill);
        }
      }

      function renderTable(phase, lastReveal, playersById) {
        const el = byId("tablePlays");
        const rt = byId("resultText");
        const hint = byId("tableHint");
        el.innerHTML = "";
        rt.innerHTML = "";

        if (phase === "lobby") {
          hint.textContent = "Waiting to start…";
          return;
        }

        if (phase === "lock_in") {
          hint.textContent = "Cards are face down. Reveal happens when everyone locks in.";
          // show facedown placeholders (one per player)
          for (const pid in playersById) {
            const wrap = document.createElement("div");
            wrap.style.display = "flex";
            wrap.style.flexDirection = "column";
            wrap.style.gap = "6px";
            wrap.style.alignItems = "center";

            const name = document.createElement("div");
            name.style.fontSize = "12px";
            name.style.color = "#666";
            name.textContent = playersById[pid].name;

            wrap.appendChild(name);
            wrap.appendChild(cardBackEl());
            el.appendChild(wrap);
          }
          return;
        }

        if (phase !== "reveal" || !lastReveal) {
          hint.textContent = "Waiting…";
          return;
        }

        hint.textContent = "Revealing…";

        // Sequential reveal animation
        const order = lastReveal.order || [];
        const playMap = {};
        for (const x of (lastReveal.plays || [])) playMap[x.playerId] = x.card;

        let i = 0;
        function step(){
          el.innerHTML = "";
          for (let j = 0; j < order.length; j++){
            const pid = order[j];
            const player = playersById[pid];
            const wrap = document.createElement("div");
            wrap.style.display = "flex";
            wrap.style.flexDirection = "column";
            wrap.style.gap = "6px";
            wrap.style.alignItems = "center";

            const name = document.createElement("div");
            name.style.fontSize = "12px";
            name.style.color = "#666";
            name.textContent = player ? player.name : pid;

            wrap.appendChild(name);

            if (j <= i) {
              wrap.appendChild(cardEl(playMap[pid], false, false));
            } else {
              wrap.appendChild(cardBackEl());
            }

            el.appendChild(wrap);
          }

          i++;
          if (i < order.length) {
            setTimeout(step, 500);
          } else {
            // show winner text at end
            if (lastReveal.explosion) {
              rt.innerHTML = `<div class="explosion">💥 Explosion! Tie for highest card. No point awarded.</div>`;
            } else {
              const winner = playersById[lastReveal.winnerId];
              rt.innerHTML = `<div class="winner">🏆 ${winner ? winner.name : "Winner"} takes the point.</div>`;
            }
          }
        }

        step();
      }

      function renderHand(hand, locked, phase) {
        const el = byId("hand");
        const hint = byId("handHint");
        el.innerHTML = "";

        if (phase === "finished") {
          hint.textContent = "Game finished. Create a new room to play again.";
          return;
        }

        if (phase !== "lock_in") {
          hint.textContent = "Waiting for the next lock in…";
          return;
        }

        if (locked) {
          hint.textContent = "Locked in. Waiting for reveal…";
        } else {
          hint.textContent = "Select a card, then click Lock in.";
        }

        const canSelect = (phase === "lock_in" && !locked);
        for (const c of hand) {
          const selected = (selectedCardId === c.id);
          el.appendChild(cardEl(c, canSelect, selected));
        }

        // Add lock in button
        const btn = document.createElement("button");
        btn.textContent = locked ? "Locked" : "Lock in";
        btn.disabled = locked || !selectedCardId;
        btn.style.marginTop = "12px";
        btn.onclick = () => lockSelected();
        el.appendChild(btn);
      }

      function lockSelected() {
        if (!ws || !lastState) return;
        if (lastState.phase !== "lock_in") return;
        if (lastState.you.locked) return;
        if (!selectedCardId) return;

        ws.send(JSON.stringify({ type: "lock", cardId: selectedCardId }));
      }

      function renderLockStatus(players, phase) {
        const el = byId("lockStatus");
        if (phase === "lock_in") {
          const locked = players.filter(p => p.locked).length;
          el.textContent = `${locked}/${players.length} locked`;
        } else if (phase === "reveal") {
          el.textContent = "Reveal in progress…";
        } else if (phase === "finished") {
          el.textContent = "Finished";
        } else {
          el.textContent = "";
        }
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
            // Switch UI to game screen
            byId("lobby").classList.add("hide");
            byId("game").classList.remove("hide");
            byId("roomcode").textContent = code;
            byId("status").textContent = "";
            return;
          }

          if (msg.type === "state") {
            lastState = msg;

            byId("phase").textContent = msg.phase;
            byId("round").textContent = msg.round || "-";
            byId("roundsPlayed").textContent = msg.roundsPlayed || 0;

            const playersById = {};
            for (const p of msg.players) playersById[p.id] = p;

            renderPlayers(msg.players);
            renderLockStatus(msg.players, msg.phase);

            // If your hand changed and you selected a card that no longer exists, clear selection.
            if (msg.you && msg.you.hand) {
              const exists = msg.you.hand.some(c => c.id === selectedCardId);
              if (!exists) selectedCardId = null;
            }

            renderTable(msg.phase, msg.lastReveal, playersById);
            renderHand((msg.you && msg.you.hand) ? msg.you.hand : [], (msg.you && msg.you.locked) ? true : false, msg.phase);
          }
        };

        ws.onclose = () => {
          byId("status").textContent = "Disconnected. Refresh and rejoin.";
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

                if player_id is not None:
                    continue

                player_id = make_player_id()
                player = Player(id=player_id, name=name, is_bot=False)
                room.players.append(player)

                ensure_bot_fill(room)

                room.socket_to_player[websocket] = player_id
                await websocket.send_text(json.dumps({"type": "hello", "playerId": player_id}))

                # Start game automatically once we have 2+ total players (bots count)
                if room.phase == "lobby":
                    asyncio.create_task(start_game(room))

                await broadcast_state(room)
                continue

            if player_id is None:
                await websocket.send_text(json.dumps({"type": "error", "message": "Join first."}))
                continue

            if mtype == "lock":
                if room.phase != "lock_in":
                    continue

                you = find_player(room, player_id)
                if not you:
                    continue

                if you.locked:
                    continue

                card_id = (msg.get("cardId") or "").strip()
                if not card_id:
                    continue

                # Card must be in your hand
                idx = next((i for i, c in enumerate(you.hand) if c["id"] == card_id), None)
                if idx is None:
                    continue

                chosen = you.hand.pop(idx)
                room.pending_plays[you.id] = chosen
                you.locked = True

                # If bots haven't locked yet, speed them up a bit
                for b in bot_players(room):
                    if not b.locked:
                        asyncio.create_task(bot_lock_after_delay(room, b, delay_s=random.uniform(0.15, 0.7)))

                await broadcast_state(room)
                await maybe_start_reveal(room)
                continue

    except WebSocketDisconnect:
        pass
    finally:
        room.sockets.discard(websocket)
        room.socket_to_player.pop(websocket, None)

        if player_id:
            room.players = [p for p in room.players if p.id != player_id]

            if len(room.players) < 2:
                room.phase = "finished"

        await broadcast_state(room)
