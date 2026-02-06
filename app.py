import asyncio
import json
import random
import string
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

app = FastAPI()

# ============================================================
# Cards
# ============================================================

SUITS = ["S", "H", "D", "C"]
SUIT_SYMBOL = {"S": "♠", "H": "♥", "D": "♦", "C": "♣"}
SUIT_COLOR = {"S": "black", "C": "black", "H": "red", "D": "red"}

RANKS = list(range(2, 15))  # 11 J, 12 Q, 13 K, 14 A
RANK_LABEL = {**{i: str(i) for i in range(2, 11)}, 11: "J", 12: "Q", 13: "K", 14: "A"}


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


# ============================================================
# Game state
# ============================================================

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

    host_id: Optional[str] = None

    phase: str = "lobby"  # lobby | lock_in | reveal | finished
    round: int = 0
    rounds_played: int = 0

    players: List[Player] = field(default_factory=list)

    pending_plays: Dict[str, dict] = field(default_factory=dict)
    last_reveal: Optional[dict] = None

    history: List[dict] = field(default_factory=list)

    sockets: Set[WebSocket] = field(default_factory=set)
    socket_to_player: Dict[WebSocket, str] = field(default_factory=dict)

    starting: bool = False
    advancing: bool = False


rooms: Dict[str, Room] = {}


# ============================================================
# Helpers
# ============================================================

def find_player(room: Room, player_id: str) -> Optional[Player]:
    for p in room.players:
        if p.id == player_id:
            return p
    return None


def human_players(room: Room) -> List[Player]:
    return [p for p in room.players if not p.is_bot]


def bot_players(room: Room) -> List[Player]:
    return [p for p in room.players if p.is_bot]


def ensure_bot_fill(room: Room) -> None:
    # Add bots only at game start (never while joining)
    target = max(2, min(6, int(room.target_size)))
    while len(room.players) < target:
        bot_num = len([b for b in room.players if b.is_bot]) + 1
        room.players.append(Player(id=make_player_id(), name=f"Bot {bot_num}", is_bot=True))


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
    return bool(room.players) and all(p.locked for p in room.players)


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
                "hostId": room.host_id,
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


# ============================================================
# Stronger bot behavior
# ============================================================

def bot_pick_index(n: int, pressure: float) -> int:
    # pressure in [0,1] where 1 is "play high"
    if n <= 1:
        return 0
    p = max(0.0, min(1.0, pressure))
    skew = p ** 1.2
    idx = int(skew * (n - 1))
    return max(0, min(n - 1, idx))


def estimate_tie_risk(rank: int, n_players: int) -> float:
    # Crude tie risk model: more players => more tie risk; extremes slightly safer
    mid = 8
    dist = abs(rank - mid)
    base = 0.10 + 0.05 * max(0, n_players - 2)
    adjust = -0.02 * min(dist, 6)
    return max(0.02, min(0.60, base + adjust))


async def bot_choose(room: Room, bot: Player) -> None:
    if room.phase != "lock_in":
        return
    if bot.locked or not bot.hand:
        return

    hand_sorted = sorted(bot.hand, key=lambda c: c["rank"])
    n = len(hand_sorted)

    scores = [p.score for p in room.players]
    leader = max(scores) if scores else 0
    trailing_by = leader - bot.score

    rounds_left = len(bot.hand)

    # Baseline pressure from score situation
    if trailing_by >= 3:
        pressure = 0.90
    elif trailing_by == 2:
        pressure = 0.80
    elif trailing_by == 1:
        pressure = 0.64
    elif trailing_by == 0:
        pressure = 0.44
    else:
        pressure = 0.28  # leading => conserve

    # Late game ramp: spend more when fewer rounds remain
    if rounds_left <= 4:
        pressure = min(0.95, pressure + 0.18)
    if rounds_left <= 2:
        pressure = min(0.98, pressure + 0.12)

    idx = bot_pick_index(n, pressure)
    chosen = hand_sorted[idx]

    # Tie avoidance nudge
    risk = estimate_tie_risk(rank_value(chosen), len(room.players))
    if risk > 0.28 and n >= 3:
        if trailing_by >= 1:
            idx = min(n - 1, idx + 1)
        else:
            idx = max(0, idx - 1)
        chosen = hand_sorted[idx]

    # Remove from actual hand
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
        delay = random.uniform(0.7, 2.5)
        asyncio.create_task(bot_lock_after_delay(room, b, delay))


# ============================================================
# Game flow
# ============================================================

async def start_game(room: Room) -> None:
    if room.phase != "lobby":
        return
    if room.starting:
        return

    # KEY FIX: allow solo starts
    if len(human_players(room)) < 1:
        return

    room.starting = True
    await broadcast_state(room)
    await asyncio.sleep(0.15)

    # Fill bots now (not during lobby join)
    ensure_bot_fill(room)

    room.round = 1
    room.rounds_played = 0
    room.phase = "lock_in"
    room.pending_plays = {}
    room.last_reveal = None
    room.history = []

    for p in room.players:
        p.score = 0
        p.locked = False
        p.hand = []

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
        "winnerCard": winner_card,
    }

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

    # pacing: server waits while client flips cards
    base = 1.8
    per_card = 0.65
    extra_pause_after = 1.9
    await asyncio.sleep(base + per_card * max(1, len(room.players)) + extra_pause_after)

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


# ============================================================
# UI
# ============================================================

HOME_HTML = r"""
<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>La Surprise</title>
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <style>
      :root{
        --bg:#07070a;
        --text:rgba(255,255,255,0.92);
        --muted:rgba(255,255,255,0.62);
        --border:rgba(255,255,255,0.14);
        --panel:rgba(255,255,255,0.07);
        --panel2:rgba(255,255,255,0.10);
        --shadow:0 22px 70px rgba(0,0,0,0.65);
        --radius:18px;
        --gold:rgba(218,182,122,0.95);
        --danger:rgba(193,18,31,0.95);

        --felt1: rgba(18, 92, 66, 0.95);
        --felt2: rgba(10, 60, 45, 0.95);
        --wood1: rgba(64, 36, 20, 0.85);
        --wood2: rgba(30, 16, 10, 0.85);
      }

      body{
        margin:0;
        font-family: ui-sans-serif, system-ui, -apple-system, Segoe UI, Roboto, Arial, sans-serif;
        color:var(--text);
        background:
          radial-gradient(circle at 20% 0%, rgba(218,182,122,0.12) 0%, rgba(0,0,0,0) 45%),
          radial-gradient(circle at 80% 10%, rgba(255,255,255,0.06) 0%, rgba(0,0,0,0) 50%),
          linear-gradient(180deg, #0b0b0f 0%, #07070a 100%);
      }

      .wrap{ max-width: 1200px; margin: 26px auto; padding: 0 16px 30px; }

      h1{
        font-family: ui-serif, Georgia, "Times New Roman", Times, serif;
        font-size: 54px; margin:0 0 8px;
        font-weight: 900; letter-spacing:0.6px;
      }
      .sub{ margin:0 0 18px; color:var(--muted); max-width: 920px; }

      .grid{ display:grid; grid-template-columns:1fr; gap:16px; }
      @media (min-width: 900px){ .grid{ grid-template-columns: 1fr 1fr; } }

      .box{
        border:1px solid var(--border);
        border-radius: var(--radius);
        padding:18px;
        background: linear-gradient(180deg, var(--panel2), var(--panel));
        box-shadow: var(--shadow);
        backdrop-filter: blur(10px);
      }

      input, button, select{
        padding: 10px 12px;
        font-size: 16px;
        border-radius: 12px;
        border:1px solid var(--border);
        background: rgba(255,255,255,0.06);
        color: var(--text);
        outline:none;
      }
      input::placeholder{ color: rgba(255,255,255,0.45); }
      button{ cursor:pointer; border-color: rgba(255,255,255,0.28); }
      button:hover{ border-color: rgba(255,255,255,0.55); }
      button:disabled{ opacity:0.45; cursor:not-allowed; }

      .row{ display:flex; gap:10px; flex-wrap:wrap; align-items:center; }
      .pill{
        display:inline-flex; gap:10px; align-items:center;
        border:1px solid var(--border);
        border-radius:999px; padding: 8px 12px;
        background: rgba(255,255,255,0.06);
      }
      .code{ font-weight: 900; letter-spacing:1px; }
      .status{ margin-top:10px; color: var(--muted); min-height:20px; }

      .hide{ display:none !important; }

      /* GAME TOP BAR */
      #gameTop{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-top: 10px; }

      /* TABLE */
      .tableShell{
        margin-top: 14px;
        border-radius: 26px;
        padding: 16px;
        border: 1px solid rgba(255,255,255,0.10);
        background:
          radial-gradient(circle at 50% 25%, rgba(255,255,255,0.08) 0%, rgba(0,0,0,0) 40%),
          linear-gradient(180deg, var(--wood1), var(--wood2));
        box-shadow: 0 26px 90px rgba(0,0,0,0.70);
      }

      .table{
        position: relative;
        height: 520px;
        border-radius: 22px;
        background:
          radial-gradient(circle at 50% 35%, rgba(255,255,255,0.08) 0%, rgba(0,0,0,0) 45%),
          linear-gradient(180deg, var(--felt1), var(--felt2));
        border: 1px solid rgba(0,0,0,0.35);
        overflow:hidden;
      }

      .table::before{
        content:"";
        position:absolute;
        inset: 14px;
        border-radius: 18px;
        border: 1px dashed rgba(255,255,255,0.14);
        pointer-events:none;
      }

      .seat{
        position:absolute;
        display:flex;
        flex-direction:column;
        align-items:center;
        gap:8px;
        min-width: 120px;
      }

      .seatName{
        font-size: 12px;
        color: rgba(255,255,255,0.70);
        text-align:center;
        white-space:nowrap;
        overflow:hidden;
        text-overflow:ellipsis;
        max-width: 140px;
      }

      .seatMeta{
        font-size: 12px;
        color: rgba(255,255,255,0.55);
      }

      /* positions for up to 6 seats */
      .pos0{ left: 50%; top: 12px; transform: translateX(-50%); }
      .pos1{ right: 18px; top: 80px; }
      .pos2{ right: 18px; bottom: 80px; }
      .pos3{ left: 50%; bottom: 12px; transform: translateX(-50%); }
      .pos4{ left: 18px; bottom: 80px; }
      .pos5{ left: 18px; top: 80px; }

      /* Center play area */
      .center{
        position:absolute;
        left:50%; top:50%;
        transform: translate(-50%,-50%);
        width: min(760px, 92%);
        text-align:center;
      }

      .centerHint{ color: rgba(255,255,255,0.70); font-size: 13px; margin-bottom: 10px; }

      .plays{
        display:flex;
        justify-content:center;
        align-items:flex-end;
        gap:12px;
        flex-wrap:wrap;
        min-height: 160px;
      }

      .playStack{
        display:flex;
        flex-direction:column;
        align-items:center;
        gap:6px;
      }

      .playName{ font-size:12px; color: rgba(255,255,255,0.70); max-width: 120px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

      /* Cards */
      .card{
        width: 74px;
        height: 104px;
        border-radius: 14px;
        border: 1px solid rgba(0,0,0,0.32);
        background: rgba(255,255,255,0.96);
        box-shadow: 0 10px 24px rgba(0,0,0,0.28);
        display:flex;
        flex-direction:column;
        justify-content:space-between;
        padding:10px;
        user-select:none;
        transition: transform 0.12s ease, outline 0.12s ease, box-shadow 0.12s ease;
      }
      .card:hover{ transform: translateY(-2px); }
      .card.selected{ outline: 3px solid rgba(255,255,255,0.45); }
      .corner{ font-weight: 900; font-size: 18px; }
      .suit{ font-size: 24px; align-self:flex-end; }
      .red{ color: #c1121f; }
      .black{ color: #0f0f14; }

      .card.back{
        background:
          radial-gradient(circle at 30% 30%, rgba(255,255,255,0.10) 0%, rgba(0,0,0,0) 45%),
          linear-gradient(135deg, #101016 0%, #2a2a33 100%);
        border-color: rgba(255,255,255,0.10);
        box-shadow: 0 10px 24px rgba(0,0,0,0.62);
      }

      /* Hand panel */
      .below{
        margin-top: 14px;
        display:grid;
        grid-template-columns: 1fr;
        gap: 16px;
      }
      @media (min-width: 900px){
        .below{ grid-template-columns: 1fr 380px; }
      }

      .panel{
        border:1px solid var(--border);
        border-radius: var(--radius);
        padding:16px;
        background: linear-gradient(180deg, rgba(255,255,255,0.08), rgba(255,255,255,0.05));
        backdrop-filter: blur(10px);
        box-shadow: 0 10px 30px rgba(0,0,0,0.55);
      }

      .big{ font-size: 18px; font-weight: 900; letter-spacing:0.2px; }

      .handCards{ display:flex; gap:12px; flex-wrap:wrap; margin-top: 10px; }

      .banner{
        margin-top: 12px;
        padding: 12px 14px;
        border-radius: 14px;
        border:1px solid var(--border);
        background: rgba(255,255,255,0.06);
        font-weight: 900;
      }
      .banner.win{ color: var(--gold); }
      .banner.tie{ color: var(--danger); }

      /* History */
      .history{
        margin-top: 10px;
        border-top: 1px dashed rgba(255,255,255,0.18);
        padding-top: 12px;
      }
      .historyItem{
        display:flex;
        justify-content:space-between;
        gap:12px;
        padding: 10px 12px;
        border: 1px solid rgba(255,255,255,0.10);
        border-radius: 12px;
        background: rgba(0,0,0,0.18);
        margin-top: 8px;
        font-size: 14px;
        color: rgba(255,255,255,0.84);
      }
      .historyRight{ color: rgba(255,255,255,0.62); white-space:nowrap; }

      /* Lobby host start button */
      .primary{ border-color: rgba(218,182,122,0.70); }
      .primary:hover{ border-color: rgba(218,182,122,1.0); }

      /* Game Over overlay */
      #gameOver{
        position:fixed; inset:0;
        display:flex; align-items:center; justify-content:center;
        padding:18px;
        z-index:60;
        background: rgba(0,0,0,0.72);
      }
      .gameOverCard{
        width:min(760px, 96vw);
        border:1px solid var(--border);
        border-radius: 18px;
        background: linear-gradient(180deg, rgba(255,255,255,0.10), rgba(255,255,255,0.06));
        box-shadow: var(--shadow);
        backdrop-filter: blur(10px);
        padding: 18px;
        position: relative;
        overflow:hidden;
      }
      .finalRow{
        display:flex;
        justify-content:space-between;
        gap:12px;
        padding: 12px 14px;
        border:1px solid rgba(255,255,255,0.10);
        border-radius: 12px;
        background: rgba(0,0,0,0.18);
        margin-top: 10px;
        font-size: 15px;
        color: rgba(255,255,255,0.88);
      }
      .finalRowRight{ color: rgba(255,255,255,0.62); white-space:nowrap; }

      /* Confetti */
      .confettiPiece{
        position:absolute;
        top:-10px;
        font-size: 18px;
        opacity: 0.95;
        animation: fall 2.8s linear forwards;
        pointer-events:none;
      }
      @keyframes fall{
        0%{ transform: translateY(-10px) rotate(0deg); opacity:1; }
        100%{ transform: translateY(820px) rotate(720deg); opacity:0.1; }
      }
    </style>
  </head>

  <body>
    <div class="wrap">
      <h1>La Surprise</h1>
      <p class="sub">A game of nerve and timing. Join the lobby. The host starts when ready. Highest card wins a point. Tie = explosion.</p>

      <!-- LOBBY -->
      <div id="lobby">
        <div class="grid">
          <div class="box">
            <h2 style="margin:0 0 10px;">Create a room</h2>
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
            <h2 style="margin:0 0 10px;">Join a room</h2>
            <div class="row">
              <input id="code" placeholder="Room code" />
              <input id="name" placeholder="Your name" />
              <button onclick="joinRoom()">Join</button>
            </div>
            <div class="status" id="status"></div>

            <div id="lobbyPanel" class="hide" style="margin-top:12px;">
              <div class="big">Lobby</div>
              <div class="status" id="lobbyHint"></div>
              <div id="lobbyPlayers"></div>
              <div class="row" style="margin-top:12px;">
                <button id="startBtn" class="primary" onclick="hostStart()" disabled>Host: Start game</button>
              </div>
              <div class="status" style="margin-top:10px;">
                Solo tip: you can start alone. The table will fill with bots.
              </div>
            </div>
          </div>
        </div>
      </div>

      <!-- GAME OVER -->
      <div id="gameOver" class="hide">
        <div class="gameOverCard">
          <div style="display:flex; align-items:flex-start; justify-content:space-between; gap:12px;">
            <div>
              <div style="font-size:24px; font-weight:900; letter-spacing:0.3px;">Game Over</div>
              <div id="gameOverWinner" class="status" style="margin-top:6px;"></div>
            </div>
            <button onclick="closeGameOver()">Close</button>
          </div>

          <div style="margin-top:14px;">
            <div class="big">Final leaderboard</div>
            <div id="gameOverBoard" style="margin-top:10px;"></div>
          </div>

          <div class="row" style="margin-top:14px;">
            <button onclick="newGameSameSize()">New game</button>
            <button onclick="backToLobby()">Back to lobby</button>
          </div>

          <div class="status" style="margin-top:10px;">Send the room link to friends, then start when ready.</div>
        </div>
      </div>

      <!-- GAME -->
      <div id="game" class="hide">
        <div id="gameTop">
          <div class="pill">Room: <span class="code" id="roomcode"></span></div>
          <div class="pill">Round: <span id="round">-</span></div>
          <div class="pill">Rounds played: <span id="roundsPlayed">0</span></div>
          <div class="pill">Phase: <span id="phase">-</span></div>
          <button onclick="copyRoom()">Copy room code</button>
        </div>

        <div class="tableShell">
          <div class="table">
            <!-- Seats -->
            <div id="seats"></div>

            <!-- Center -->
            <div class="center">
              <div class="centerHint" id="tableHint">Waiting…</div>
              <div class="plays" id="tablePlays"></div>
              <div id="resultText"></div>
            </div>
          </div>
        </div>

        <div class="below">
          <div class="panel">
            <div class="big">Your hand</div>
            <div class="status" id="handHint"></div>
            <div class="handCards" id="hand"></div>
          </div>

          <div class="panel">
            <div class="big">Scoreboard</div>
            <div class="status" id="lockStatus"></div>
            <div id="scoreRows"></div>

            <div class="history">
              <div class="big">Round history</div>
              <div id="history"></div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <script>
      let ws = null;
      let lastState = null;
      let selectedCardId = null;

      // OPTION A: hide history until reveal animation ends
      let revealDone = true;

      function byId(x){ return document.getElementById(x); }

      function phaseLabel(p){
        if (p === "lobby") return "Lobby";
        if (p === "lock_in") return "Lock in";
        if (p === "reveal") return "Reveal";
        if (p === "finished") return "Finished";
        return p;
      }

      function cardText(card){
        if (!card) return "";
        return `${card.label}${card.symbol}`;
      }

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

      function renderLobby(players, hostId, youId, phase){
        const panel = byId("lobbyPanel");
        if (!players || !youId) return;

        panel.classList.remove("hide");

        const humans = players.filter(p => !p.isBot);
        byId("lobbyHint").textContent = `Humans in lobby: ${humans.length}. Host starts when ready.`;

        const lp = byId("lobbyPlayers");
        lp.innerHTML = "";
        for (const p of humans){
          const pill = document.createElement("div");
          pill.className = "pill";
          const crown = (p.id === hostId) ? " 👑" : "";
          pill.textContent = `🧑 ${p.name}${crown}`;
          lp.appendChild(pill);
        }

        const startBtn = byId("startBtn");
        const iAmHost = (youId === hostId);

        // KEY FIX: allow solo start (>=1 human)
        startBtn.disabled = !(iAmHost && humans.length >= 1 && phase === "lobby");
      }

      function renderScoreboard(players, phase){
        const el = byId("scoreRows");
        el.innerHTML = "";
        const sorted = [...players].sort((a,b) => b.score - a.score);

        for (const p of sorted) {
          const row = document.createElement("div");
          row.className = "historyItem";
          const icon = p.isBot ? "🤖" : "🧑";

          let status = "";
          if (phase === "finished") status = "Final";
          else if (phase === "reveal") status = "Revealing…";
          else status = p.locked ? "Locked" : "Choosing…";

          row.innerHTML = `<div>${icon} <b>${p.name}</b> <span style="color:rgba(255,255,255,0.55);">(${status})</span></div>
                           <div class="historyRight"><b>${p.score}</b> pts</div>`;
          el.appendChild(row);
        }
      }

      function renderLockStatus(players, phase){
        const el = byId("lockStatus");
        if (phase === "lock_in") {
          const locked = players.filter(p => p.locked).length;
          el.textContent = `${locked}/${players.length} locked`;
        } else if (phase === "reveal") {
          el.textContent = "Reveal in progress…";
        } else if (phase === "finished") {
          el.textContent = "Finished";
        } else if (phase === "lobby") {
          el.textContent = "Lobby";
        } else {
          el.textContent = "";
        }
      }

      function seatClass(i){
        const map = ["pos0","pos1","pos2","pos3","pos4","pos5"];
        return map[i] || "pos0";
      }

      function renderSeats(players, phase, hostId){
        const seats = byId("seats");
        seats.innerHTML = "";

        // Show seats in a stable order: humans first, then bots, then by join order
        const list = [...players];
        const humans = list.filter(p => !p.isBot);
        const bots = list.filter(p => p.isBot);
        const ordered = [...humans, ...bots].slice(0, 6);

        ordered.forEach((p, i) => {
          const div = document.createElement("div");
          div.className = `seat ${seatClass(i)}`;
          const crown = (p.id === hostId && phase === "lobby") ? " 👑" : "";
          div.innerHTML = `
            <div class="seatName">${p.name}${crown}</div>
            <div class="seatMeta">${p.isBot ? "Bot" : "Human"} · ${p.score} pts · ${p.locked ? "Locked" : "…"}</div>
          `;
          seats.appendChild(div);
        });
      }

      function renderHistory(history, playersById){
        const el = byId("history");
        el.innerHTML = "";
        if (!history || history.length === 0) {
          el.innerHTML = `<div class="status">No rounds yet.</div>`;
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
            left += ` — 🏆 ${wname}`;
            right = wc ? `Won with ${cardText(wc)}` : "";
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
          hint.textContent = "Waiting for the host to start…";
          return;
        }

        if (phase === "lock_in") {
          hint.textContent = "Cards are face down. Reveal happens when everyone locks in.";

          const ids = Object.keys(playersById);
          for (const pid of ids) {
            const wrap = document.createElement("div");
            wrap.className = "playStack";

            const name = document.createElement("div");
            name.className = "playName";
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

        const order = lastReveal.order || [];
        const playMap = {};
        for (const x of (lastReveal.plays || [])) playMap[x.playerId] = x.card;

        const winnerId = lastReveal.winnerId;
        const explosion = lastReveal.explosion;

        let i = 0;
        function step(){
          el.innerHTML = "";

          for (let j = 0; j < order.length; j++){
            const pid = order[j];
            const player = playersById[pid];

            const wrap = document.createElement("div");
            wrap.className = "playStack";

            const name = document.createElement("div");
            name.className = "playName";
            name.textContent = player ? player.name : pid;

            wrap.appendChild(name);

            if (j <= i) wrap.appendChild(cardEl(playMap[pid], false, false));
            else wrap.appendChild(cardBackEl());

            el.appendChild(wrap);
          }

          i++;
          if (i < order.length) {
            setTimeout(step, 900);
          } else {
            // Winner banner
            if (explosion) {
              rt.innerHTML = `<div class="banner tie">💥 Explosion — tie for highest card. No point awarded.</div>`;
            } else {
              const winner = playersById[winnerId];
              const wname = winner ? winner.name : "Winner";
              const wc = lastReveal.winnerCard;
              const wct = wc ? ` with ${cardText(wc)}` : "";
              rt.innerHTML = `<div class="banner win">🏆 ${wname} wins the round (+1)${wct}.</div>`;
            }

            // OPTION A: reveal done -> now show history (no spoilers before this)
            revealDone = true;
            if (lastState) {
              renderHistory(lastState.history, playersById);
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
          hint.textContent = "Game finished. See the winner screen.";
          return;
        }

        if (phase !== "lock_in") {
          hint.textContent = "Waiting for the next lock in…";
          return;
        }

        if (locked) hint.textContent = "Locked in. Waiting for reveal…";
        else hint.textContent = "Select a card, then click Lock in.";

        const canSelect = (phase === "lock_in" && !locked);
        for (const c of hand) {
          const selected = (selectedCardId === c.id);
          el.appendChild(cardEl(c, canSelect, selected));
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

      function hostStart(){
        if (!ws) return;
        ws.send(JSON.stringify({ type: "start" }));
      }

      function clearConfetti(){
        const card = document.querySelector(".gameOverCard");
        if (!card) return;
        card.querySelectorAll(".confettiPiece").forEach(p => p.remove());
      }

      function launchConfetti(){
        const card = document.querySelector(".gameOverCard");
        if (!card) return;

        const chars = ["✨","🎉","✦","✧","❖","✺","★"];
        for (let i = 0; i < 90; i++){
          const p = document.createElement("div");
          p.className = "confettiPiece";
          p.textContent = chars[Math.floor(Math.random() * chars.length)];
          p.style.left = `${Math.random() * 100}%`;
          p.style.animationDelay = `${Math.random() * 0.7}s`;
          p.style.fontSize = `${14 + Math.random()*12}px`;
          card.appendChild(p);
          setTimeout(() => { try{ p.remove(); }catch(e){} }, 3800);
        }
      }

      function showGameOver(players){
        const sorted = [...players].sort((a,b) => b.score - a.score);
        const topScore = sorted.length ? sorted[0].score : 0;
        const winners = sorted.filter(p => p.score === topScore);

        const winnerText = winners.length === 1
          ? `🏆 ${winners[0].name} wins with ${topScore} points.`
          : `🤝 Tie! ${winners.map(w => w.name).join(", ")} share the win with ${topScore} points.`;

        byId("gameOverWinner").textContent = winnerText;

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
        launchConfetti();
      }

      function closeGameOver(){
        byId("gameOver").classList.add("hide");
        clearConfetti();
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
        byId("lobbyPanel").classList.add("hide");
        byId("status").textContent = "";
        try { if (ws) ws.close(); } catch(e){}
        ws = null;
        lastState = null;
        selectedCardId = null;
        revealDone = true;
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
            byId("status").textContent = "Connected. Waiting in lobby…";
            byId("lobbyPanel").classList.remove("hide");
            return;
          }

          if (msg.type === "state") {
            lastState = msg;

            // OPTION A: reveal phase starts -> hide history until the animation completes
            if (msg.phase === "reveal") revealDone = false;
            else revealDone = true;

            if (msg.phase === "lobby") {
              renderLobby(msg.players, msg.hostId, msg.you.id, msg.phase);
            }

            if (msg.phase !== "lobby") {
              byId("lobby").classList.add("hide");
              byId("game").classList.remove("hide");
              byId("roomcode").textContent = msg.code;
            }

            byId("phase").textContent = phaseLabel(msg.phase);
            byId("round").textContent = msg.round || "-";
            byId("roundsPlayed").textContent = msg.roundsPlayed || 0;

            const playersById = {};
            for (const p of msg.players) playersById[p.id] = p;

            renderSeats(msg.players, msg.phase, msg.hostId);
            renderScoreboard(msg.players, msg.phase);
            renderLockStatus(msg.players, msg.phase);

            // history: only show if not revealing OR reveal animation done
            if (msg.phase !== "reveal" || revealDone) {
              renderHistory(msg.history, playersById);
            } else {
              byId("history").innerHTML = `<div class="status">Revealing…</div>`;
            }

            if (msg.you && msg.you.hand) {
              const exists = msg.you.hand.some(c => c.id === selectedCardId);
              if (!exists) selectedCardId = null;
            }

            renderTable(msg.phase, msg.lastReveal, playersById);
            renderHand((msg.you && msg.you.hand) ? msg.you.hand : [], (msg.you && msg.you.locked) ? true : false, msg.phase);

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

      const params = new URLSearchParams(location.search);
      if (params.get("code")) {
        byId("code").value = params.get("code").toUpperCase();
      }
    </script>
  </body>
</html>
"""


# ============================================================
# Routes
# ============================================================

@app.get("/")
def home():
    return HTMLResponse(HOME_HTML)


@app.get("/health")
def health():
    return JSONResponse({"ok": True})


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

                # Only joinable in lobby
                if room.phase != "lobby":
                    await websocket.send_text(json.dumps({"type": "error", "message": "Game already started."}))
                    continue

                player_id = make_player_id()
                player = Player(id=player_id, name=name, is_bot=False)
                room.players.append(player)

                # First human becomes host
                if room.host_id is None:
                    room.host_id = player_id

                room.socket_to_player[websocket] = player_id
                await websocket.send_text(json.dumps({"type": "hello", "playerId": player_id}))

                await broadcast_state(room)
                continue

            if player_id is None:
                await websocket.send_text(json.dumps({"type": "error", "message": "Join first."}))
                continue

            if mtype == "start":
                # Only host can start
                if room.phase != "lobby":
                    continue
                if room.host_id != player_id:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Only the host can start."}))
                    continue

                # KEY FIX: allow solo start (>=1 human)
                if len(human_players(room)) < 1:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Need at least 1 human player."}))
                    continue

                asyncio.create_task(start_game(room))
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

                # speed up bots slightly after a human locks
                for b in bot_players(room):
                    if not b.locked:
                        asyncio.create_task(bot_lock_after_delay(room, b, delay_s=random.uniform(0.35, 1.05)))

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

            # If host left, assign a new host among humans (if any)
            if room.host_id == player_id:
                humans = human_players(room)
                room.host_id = humans[0].id if humans else None

            # If no humans left, freeze room
            if len(human_players(room)) < 1:
                room.phase = "finished"

        await broadcast_state(room)
