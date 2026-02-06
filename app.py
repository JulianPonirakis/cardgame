# app.py
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

# Add A and B (B is rank 15)
RANKS = list(range(2, 16))  # 11 J, 12 Q, 13 K, 14 A, 15 B
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
    15: "B",
}


def make_code(n: int = 5) -> str:
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(n))


def make_player_id() -> str:
    return "".join(random.choice(string.ascii_lowercase + string.digits) for _ in range(10))


def new_deck() -> List[dict]:
    deck: List[dict] = []
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
    locked: bool = False


@dataclass
class Room:
    code: str
    target_size: int = 4
    phase: str = "lobby"  # lobby | lock_in | reveal | finished
    round: int = 0
    rounds_played: int = 0

    players: List[Player] = field(default_factory=list)

    # Hidden selections during lock_in
    pending_plays: Dict[str, dict] = field(default_factory=dict)  # playerId -> card

    # Reveal payload visible during reveal/finished
    last_reveal: Optional[dict] = None  # {"round","order","plays","winnerId","explosion","topIds","topRank"}

    # Round history (most recent first)
    history: List[dict] = field(default_factory=list)  # keep last N

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
        bot_count = len([b for b in room.players if b.is_bot])
        bot = Player(id=make_player_id(), name=f"Bot {bot_count + 1}", is_bot=True)
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
    visible_reveal = room.last_reveal if room.phase in ("reveal", "finished") else None

    dead: List[WebSocket] = []
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
                "history": room.history,
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
    if bot.locked or not bot.hand:
        return

    scores = [p.score for p in room.players]
    leader = max(scores) if scores else 0
    trailing_by = leader - bot.score

    sorted_hand = sorted(bot.hand, key=lambda c: c["rank"])
    n = len(sorted_hand)

    # A tiny bit of “poker psychology”
    if trailing_by >= 2:
        idx = int(random.uniform(0.68, 1.0) * (n - 1))
    elif trailing_by <= 0:
        idx = int(random.uniform(0.0, 0.42) * (n - 1))
    else:
        idx = int(random.uniform(0.22, 0.78) * (n - 1))

    idx = max(0, min(idx, n - 1))
    chosen = sorted_hand[idx]

    for i, c in enumerate(bot.hand):
        if c["id"] == chosen["id"]:
            bot.hand.pop(i)
            break

    room.pending_plays[bot.id] = chosen
    bot.locked = True


async def bot_lock_after_delay(room: Room, bot: Player, delay_s: float) -> None:
    await asyncio.sleep(delay_s)
    if room.phase != "lock_in":
        return
    if bot.locked:
        return
    await bot_choose(room, bot)
    await broadcast_state(room)
    await maybe_start_reveal(room)


async def schedule_bots(room: Room) -> None:
    for b in bot_players(room):
        delay = random.uniform(0.7, 2.0)
        asyncio.create_task(bot_lock_after_delay(room, b, delay))


async def start_game(room: Room) -> None:
    if room.phase != "lobby":
        return
    if room.starting:
        return
    if len(room.players) < 2:
        return

    room.starting = True
    await broadcast_state(room)
    await asyncio.sleep(0.2)

    room.round = 1
    room.rounds_played = 0
    room.phase = "lock_in"
    room.pending_plays = {}
    room.last_reveal = None
    room.history = []

    # reset scores each new game
    for p in room.players:
        p.score = 0

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
    room.phase = "reveal"

    order = [p.id for p in room.players]
    random.shuffle(order)

    plays = []
    for pid in order:
        card = room.pending_plays.get(pid)
        if card is None:
            card = {"id": "X", "suit": "S", "rank": 2, "label": "?", "symbol": "?", "color": "black"}
        plays.append({"playerId": pid, "card": card})

    values = [(x["playerId"], rank_value(x["card"])) for x in plays]
    max_val = max(v for _, v in values) if values else 0
    top_ids = [pid for pid, v in values if v == max_val]

    explosion = len(top_ids) > 1
    winner_id = None
    winner_card = None

    if not explosion and top_ids:
        winner_id = top_ids[0]
        w = find_player(room, winner_id)
        if w:
            w.score += 1
        for x in plays:
            if x["playerId"] == winner_id:
                winner_card = x["card"]
                break

    room.last_reveal = {
        "round": room.round,
        "order": order,
        "plays": plays,
        "winnerId": winner_id,
        "explosion": explosion,
        "topIds": top_ids,
        "topRank": max_val,
    }

    # History (keep last 10)
    room.history.insert(
        0,
        {
            "round": room.round,
            "winnerId": winner_id,
            "explosion": explosion,
            "topRank": max_val,
            "winnerCard": winner_card,
        },
    )
    room.history = room.history[:10]

    await broadcast_state(room)

    # reveal pacing
    wait_s = 1.8 + 0.55 * max(1, len(room.players))
    await asyncio.sleep(wait_s)

    room.rounds_played += 1

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
    <title>La Surprise</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      /* Fonts (film-noir + modern UI). Render will load these fine. */
      @import url('https://fonts.googleapis.com/css2?family=Cormorant+Garamond:wght@600;700&family=Inter:wght@400;600;800&display=swap');

      :root {
        --bg0: #07070a;
        --bg1: #0b0b10;
        --panel: rgba(255,255,255,0.06);
        --panel2: rgba(255,255,255,0.09);
        --text: rgba(255,255,255,0.92);
        --muted: rgba(255,255,255,0.62);
        --border: rgba(255,255,255,0.12);
        --shadow: 0 22px 70px rgba(0,0,0,0.65);
        --shadow2: 0 10px 34px rgba(0,0,0,0.50);
        --radius: 18px;
        --gold: rgba(218, 182, 122, 0.95);
        --danger: rgba(193,18,31,0.95);
      }

      body {
        font-family: Inter, ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
        color: var(--text);
        max-width: 1100px;
        margin: 30px auto;
        padding: 0 16px;

        /* Film noir */
        background:
          radial-gradient(circle at 15% 0%, rgba(218,182,122,0.14) 0%, rgba(0,0,0,0) 42%),
          radial-gradient(circle at 85% 10%, rgba(255,255,255,0.07) 0%, rgba(0,0,0,0) 50%),
          radial-gradient(circle at 55% 120%, rgba(255,255,255,0.05) 0%, rgba(0,0,0,0) 55%),
          linear-gradient(180deg, var(--bg1) 0%, var(--bg0) 100%);
      }

      h1 {
        font-family: "Cormorant Garamond", serif;
        font-size: 54px;
        margin: 0 0 6px;
        letter-spacing: 0.8px;
      }

      .sub {
        color: var(--muted);
        margin: 0 0 22px;
        max-width: 900px;
        line-height: 1.35;
      }

      .grid {
        display: grid;
        grid-template-columns: 1fr;
        gap: 16px;
      }
      @media (min-width: 900px) { .grid { grid-template-columns: 1fr 1fr; } }

      .box {
        border: 1px solid var(--border);
        border-radius: var(--radius);
        padding: 18px;
        box-shadow: var(--shadow);
        background: linear-gradient(180deg, var(--panel2), var(--panel));
        backdrop-filter: blur(10px);
      }

      input, button, select {
        padding: 10px 12px;
        font-size: 16px;
        border-radius: 12px;
        border: 1px solid var(--border);
        background: rgba(255,255,255,0.06);
        color: var(--text);
        outline: none;
      }
      input::placeholder { color: rgba(255,255,255,0.45); }

      button {
        cursor: pointer;
        border: 1px solid rgba(255,255,255,0.35);
        background: rgba(255,255,255,0.10);
        transition: transform .06s ease, border-color .10s ease, background .10s ease;
      }
      button:hover {
        border-color: rgba(255,255,255,0.60);
        background: rgba(255,255,255,0.14);
      }
      button:active { transform: translateY(1px); }
      button:disabled { opacity: 0.5; cursor: not-allowed; }

      .row { display: flex; gap: 10px; flex-wrap: wrap; align-items: center; }

      .pill {
        display: inline-flex;
        gap: 8px;
        align-items: center;
        border: 1px solid var(--border);
        border-radius: 999px;
        padding: 8px 12px;
        margin: 6px 6px 0 0;
        background: rgba(255,255,255,0.06);
      }

      .code { font-weight: 900; letter-spacing: 1px; }
      .status { margin-top: 10px; color: var(--muted); min-height: 20px; }

      .split {
        display: grid;
        grid-template-columns: 1fr;
        gap: 16px;
        margin-top: 14px;
      }
      @media (min-width: 900px) { .split { grid-template-columns: 360px 1fr; } }

      .section {
        border: 1px solid var(--border);
        border-radius: var(--radius);
        padding: 16px;
        box-shadow: var(--shadow2);
        background: linear-gradient(180deg, rgba(255,255,255,0.07), rgba(255,255,255,0.05));
        backdrop-filter: blur(10px);
      }

      .big { font-size: 18px; font-weight: 900; }
      .muted { color: var(--muted); }
      .hide { display: none !important; }

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
        border: 1px solid rgba(0,0,0,0.35);
        background: rgba(255,255,255,0.96);
        box-shadow: 0 10px 26px rgba(0,0,0,0.32);
        display: flex;
        flex-direction: column;
        justify-content: space-between;
        padding: 10px;
        user-select: none;
        transition: transform 0.10s ease, outline 0.10s ease, box-shadow 0.10s ease;
      }

      .card:hover { transform: translateY(-2px); }
      .card.selected { outline: 3px solid rgba(255,255,255,0.45); }

      .card.back {
        background: radial-gradient(circle at 30% 30%, rgba(255,255,255,0.10), rgba(0,0,0,0) 55%),
                    linear-gradient(135deg, #0f0f14 0%, #2a2a33 100%);
        border-color: rgba(255,255,255,0.12);
        box-shadow: 0 10px 26px rgba(0,0,0,0.60);
      }

      .corner { font-weight: 900; font-size: 18px; }
      .suit { font-size: 24px; align-self: flex-end; }
      .red { color: #c1121f; }
      .black { color: #0f0f14; }

      /* Reveal drama */
      @keyframes pulse {
        0% { opacity: 0.35; }
        50% { opacity: 1; }
        100% { opacity: 0.35; }
      }
      .revealing {
        animation: pulse 1.1s ease-in-out infinite;
        font-weight: 900;
        color: rgba(255,255,255,0.85);
      }

      .banner {
        margin-top: 12px;
        padding: 10px 12px;
        border-radius: 14px;
        border: 1px solid var(--border);
        background: rgba(255,255,255,0.06);
        font-weight: 900;
      }
      .banner.win { color: var(--gold); }
      .banner.tie { color: var(--danger); }

      .winGlow {
        outline: 3px solid rgba(218,182,122,0.95);
        box-shadow: 0 0 0 4px rgba(218,182,122,0.10), 0 16px 44px rgba(0,0,0,0.50);
      }
      .tieGlow {
        outline: 3px solid rgba(193,18,31,0.95);
        box-shadow: 0 0 0 4px rgba(193,18,31,0.10), 0 16px 44px rgba(0,0,0,0.50);
      }

      .history {
        margin-top: 16px;
        border-top: 1px dashed var(--border);
        padding-top: 14px;
      }

      .historyItem {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        padding: 8px 10px;
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 12px;
        background: rgba(0,0,0,0.18);
        margin-top: 8px;
        font-size: 14px;
        color: rgba(255,255,255,0.80);
      }
      .historyRight {
        color: rgba(255,255,255,0.60);
        white-space: nowrap;
      }

      /* ---- GAME OVER OVERLAY: less lame, more "final scene" ---- */
      #gameOver {
        background: radial-gradient(circle at 50% 15%, rgba(255,255,255,0.10), rgba(0,0,0,0) 60%),
                    rgba(0,0,0,0.72);
      }

      .finalCard {
        border: 1px solid var(--border);
        border-radius: 18px;
        background: linear-gradient(180deg, rgba(255,255,255,0.10), rgba(255,255,255,0.06));
        box-shadow: var(--shadow);
        backdrop-filter: blur(10px);
        padding: 18px;
      }

      .finalTitle {
        font-family: "Cormorant Garamond", serif;
        font-size: 34px;
        font-weight: 700;
        letter-spacing: 0.6px;
        margin: 0;
      }

      .finalSub {
        color: rgba(255,255,255,0.70);
        margin-top: 6px;
        line-height: 1.35;
      }

      .finalRow {
        display: flex;
        justify-content: space-between;
        gap: 12px;
        padding: 12px 12px;
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 12px;
        background: rgba(0,0,0,0.18);
        margin-top: 10px;
        font-size: 14px;
        color: rgba(255,255,255,0.88);
      }
      .finalRowRight { color: rgba(255,255,255,0.62); white-space: nowrap; }

      .podium {
        display: grid;
        grid-template-columns: 1fr;
        gap: 10px;
        margin-top: 14px;
      }
      @media (min-width: 720px) { .podium { grid-template-columns: 1fr 1fr 1fr; } }

      .podiumBox {
        border: 1px solid rgba(255,255,255,0.12);
        border-radius: 16px;
        padding: 12px;
        background: rgba(0,0,0,0.22);
      }
      .podiumRank { font-weight: 900; color: rgba(255,255,255,0.75); }
      .podiumName { margin-top: 6px; font-weight: 900; }
      .podiumPts  { margin-top: 4px; color: rgba(255,255,255,0.60); }

      @keyframes fadeUp {
        0% { transform: translateY(10px); opacity: 0; }
        100% { transform: translateY(0px); opacity: 1; }
      }
      .fadeUp { animation: fadeUp .22s ease-out; }
    </style>
  </head>

  <body>
    <h1>La Surprise</h1>
    <p class="sub">A game of nerve and timing. Everyone locks one card. Highest wins a point. Tie = explosion. Now with A and B.</p>

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

    <!-- GAME OVER OVERLAY -->
    <div id="gameOver" class="hide" style="position:fixed; inset:0; display:flex; align-items:center; justify-content:center; padding:18px; z-index:50;">
      <div class="finalCard fadeUp" style="width:min(840px, 96vw);">
        <div style="display:flex; align-items:flex-start; justify-content:space-between; gap:12px;">
          <div>
            <div class="finalTitle">Finale</div>
            <div id="gameOverWinner" class="finalSub"></div>
          </div>
          <button onclick="closeGameOver()">Close</button>
        </div>

        <div id="podium" class="podium"></div>

        <div style="margin-top:14px;">
          <div class="big">Final leaderboard</div>
          <div id="gameOverBoard" style="margin-top:8px;"></div>
        </div>

        <div class="row" style="margin-top:14px;">
          <button onclick="newGameSameSize()">New game</button>
          <button onclick="backToLobby()">Back to lobby</button>
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

          <div class="history">
            <div class="big">Round history</div>
            <div id="history"></div>
          </div>

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

      function phaseLabel(p){
        if (p === "lobby") return "Lobby";
        if (p === "lock_in") return "Lock in";
        if (p === "reveal") return "Reveal";
        if (p === "finished") return "Finished";
        return p;
      }

      function closeGameOver(){
        byId("gameOver").classList.add("hide");
      }

      function showGameOver(players){
        const sorted = [...players].sort((a,b) => b.score - a.score);
        const topScore = sorted.length ? sorted[0].score : 0;
        const winners = sorted.filter(p => p.score === topScore);

        const winnerText = winners.length === 1
          ? `🏆 ${winners[0].name} wins the night with ${topScore} points.`
          : `🤝 A tie at the top: ${winners.map(w => w.name).join(", ")} with ${topScore} points.`;

        byId("gameOverWinner").textContent = winnerText;

        // Podium (top 3)
        const podium = byId("podium");
        podium.innerHTML = "";
        const top3 = sorted.slice(0,3);
        const labels = ["1st", "2nd", "3rd"];
        for (let i = 0; i < top3.length; i++){
          const p = top3[i];
          const box = document.createElement("div");
          box.className = "podiumBox";
          const icon = p.isBot ? "🤖" : "🧑";
          box.innerHTML = `
            <div class="podiumRank">${labels[i]}</div>
            <div class="podiumName">${icon} ${p.name}</div>
            <div class="podiumPts">${p.score} pts</div>
          `;
          podium.appendChild(box);
        }

        const board = byId("gameOverBoard");
        board.innerHTML = "";
        for (let i = 0; i < sorted.length; i++){
          const p = sorted[i];
          const row = document.createElement("div");
          row.className = "finalRow";
          const icon = p.isBot ? "🤖" : "🧑";
          row.innerHTML = `<div>${i+1}. ${icon} ${p.name}</div><div class="finalRowRight">${p.score} pts</div>`;
          board.appendChild(row);
        }

        byId("gameOver").classList.remove("hide");
      }

      async function newGameSameSize(){
        const size = (lastState && lastState.targetSize) ? lastState.targetSize : 4;
        const res = await fetch(`/create?size=${size}`);
        const data = await res.json();

        backToLobby();
        byId("code").value = data.code;
        byId("status").textContent = `New room created: ${data.code}. Enter your name and join.`;
      }

      function backToLobby(){
        closeGameOver();
        byId("game").classList.add("hide");
        byId("lobby").classList.remove("hide");
        byId("status").textContent = "";
        try { if (ws) ws.close(); } catch(e){}
        ws = null;
        lastState = null;
        selectedCardId = null;
      }

      function cardEl(card, clickable, selected, extraClass) {
        const el = document.createElement("div");
        el.className = "card " + (card.color === "red" ? "red" : "black");
        if (selected) el.classList.add("selected");
        if (extraClass) el.classList.add(extraClass);

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

      function renderPlayers(players, phase) {
        const el = byId("players");
        el.innerHTML = "";

        const sorted = [...players].sort((a,b) => b.score - a.score);

        for (const p of sorted) {
          const pill = document.createElement("div");
          pill.className = "pill";
          const icon = p.isBot ? "🤖" : "🧑";

          let status = "";
          if (phase === "finished") status = "Final";
          else if (phase === "reveal") status = "Revealing…";
          else status = p.locked ? "Locked" : "Choosing…";

          pill.textContent = `${icon} ${p.name} — ${p.score} pts — ${status}`;
          el.appendChild(pill);
        }
      }

      function renderHistory(history, playersById){
        const el = byId("history");
        el.innerHTML = "";
        if (!history || history.length === 0) {
          el.innerHTML = `<div class="status muted">No rounds yet.</div>`;
          return;
        }

        for (const h of history) {
          const row = document.createElement("div");
          row.className = "historyItem";

          let left = `Round ${h.round}`;
          let right = "";

          if (h.explosion) {
            left += ` — 💥 Explosion`;
            right = `Top: ${h.topRank}`;
          } else {
            const winner = playersById[h.winnerId];
            const wname = winner ? winner.name : "Winner";
            const wc = h.winnerCard;
            const cardTxt = wc ? `${wc.label}${wc.symbol}` : "";
            left += ` — 🏆 ${wname}`;
            right = cardTxt ? `Won with ${cardTxt}` : "";
          }

          row.innerHTML = `<div>${left}</div><div class="historyRight">${right}</div>`;
          el.appendChild(row);
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
          for (const pid in playersById) {
            const wrap = document.createElement("div");
            wrap.style.display = "flex";
            wrap.style.flexDirection = "column";
            wrap.style.gap = "6px";
            wrap.style.alignItems = "center";

            const name = document.createElement("div");
            name.style.fontSize = "12px";
            name.style.color = "rgba(255,255,255,0.62)";
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

        hint.innerHTML = `<span class="revealing">Revealing…</span>`;

        const order = lastReveal.order || [];
        const playMap = {};
        for (const x of (lastReveal.plays || [])) playMap[x.playerId] = x.card;

        const topIds = lastReveal.topIds || [];
        const winnerId = lastReveal.winnerId;

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
            name.style.color = "rgba(255,255,255,0.62)";
            name.textContent = player ? player.name : pid;

            wrap.appendChild(name);

            if (j <= i) {
              let glow = "";
              if (i >= order.length - 1) {
                if (lastReveal.explosion && topIds.includes(pid)) glow = "tieGlow";
                if (!lastReveal.explosion && winnerId === pid) glow = "winGlow";
              }
              wrap.appendChild(cardEl(playMap[pid], false, false, glow));
            } else {
              wrap.appendChild(cardBackEl());
            }

            el.appendChild(wrap);
          }

          i++;
          if (i < order.length) {
            setTimeout(step, 520);
          } else {
            if (lastReveal.explosion) {
              rt.innerHTML = `<div class="banner tie">💥 Explosion — no point.</div>`;
            } else {
              const winner = playersById[lastReveal.winnerId];
              const name = winner ? winner.name : "Winner";
              rt.innerHTML = `<div class="banner win">🏆 ${name} wins the round (+1).</div>`;
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
          hint.textContent = "Game finished. The finale is waiting.";
          return;
        }

        if (phase !== "lock_in") {
          hint.textContent = "Waiting for the next lock in…";
          return;
        }

        hint.textContent = locked ? "Locked in. Waiting for reveal…" : "Select a card, then click Lock in.";

        const canSelect = (phase === "lock_in" && !locked);
        for (const c of hand) {
          const selected = (selectedCardId === c.id);
          el.appendChild(cardEl(c, canSelect, selected, ""));
        }

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
            byId("lobby").classList.add("hide");
            byId("game").classList.remove("hide");
            byId("roomcode").textContent = code;
            byId("status").textContent = "";
            return;
          }

          if (msg.type === "state") {
            lastState = msg;

            byId("phase").textContent = phaseLabel(msg.phase);
            byId("round").textContent = msg.round || "-";
            byId("roundsPlayed").textContent = msg.roundsPlayed || 0;

            const playersById = {};
            for (const p of msg.players) playersById[p.id] = p;

            renderPlayers(msg.players, msg.phase);
            renderLockStatus(msg.players, msg.phase);
            renderHistory(msg.history, playersById);

            if (msg.you && msg.you.hand) {
              const exists = msg.you.hand.some(c => c.id === selectedCardId);
              if (!exists) selectedCardId = null;
            }

            renderTable(msg.phase, msg.lastReveal, playersById);
            renderHand((msg.you && msg.you.hand) ? msg.you.hand : [], (msg.you && msg.you.locked) ? true : false, msg.phase);

            // Finale overlay
            if (msg.phase === "finished") {
              showGameOver(msg.players);
            } else {
              closeGameOver();
            }
          }
        };

        ws.onclose = () => {
          byId("status").textContent = "Disconnected. Refresh and rejoin.";
        };
      }

      // Convenience: ?code=XXXXX
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
                if not you or you.locked:
                    continue

                card_id = (msg.get("cardId") or "").strip()
                if not card_id:
                    continue

                idx = next((i for i, c in enumerate(you.hand) if c["id"] == card_id), None)
                if idx is None:
                    continue

                chosen = you.hand.pop(idx)
                room.pending_plays[you.id] = chosen
                you.locked = True

                # speed up bots once a human locks
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
