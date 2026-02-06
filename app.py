import asyncio
import json
import random
import string
import time
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

    # streak: consecutive wins
    win_streak: int = 0


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

    # history: only update AFTER reveal completes (no spoilers)
    history: List[dict] = field(default_factory=list)

    sockets: Set[WebSocket] = field(default_factory=set)
    socket_to_player: Dict[WebSocket, str] = field(default_factory=dict)

    starting: bool = False
    advancing: bool = False

    # --- New rule state ---
    aftershock_next: bool = False  # after explosion, next round worth 2
    timer_task: Optional[asyncio.Task] = None
    lock_deadline_ts: Optional[float] = None

    # pending scoring (to avoid spoiler scoreboard)
    pending_award: Optional[dict] = None  # {"winnerId":..., "points":..., "explosion":..., "reason":...}


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
    target = max(2, min(6, int(room.target_size)))
    while len(room.players) < target:
        bot_num = len([b for b in room.players if b.is_bot]) + 1
        room.players.append(Player(id=make_player_id(), name=f"Bot {bot_num}", is_bot=True))


def clear_bots(room: Room) -> None:
    room.players = [p for p in room.players if not p.is_bot]


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


def is_joker_round(room: Room) -> bool:
    # Every 5th round (5, 10, 15...)
    return room.round > 0 and room.round % 5 == 0


def room_public_players(room: Room) -> List[dict]:
    return [
        {
            "id": p.id,
            "name": p.name,
            "isBot": p.is_bot,
            "score": p.score,
            "locked": p.locked,
            "cardsLeft": len(p.hand),
            "winStreak": p.win_streak,
        }
        for p in room.players
    ]


def round_point_value(room: Room, winner: Player) -> int:
    # Base +1
    # Aftershock: +2 next round after explosion
    # Win streak: once you have 2 consecutive wins, NEXT wins are worth +2 (so 3rd+ consecutive)
    # Cap bonus at 2 total.
    if room.aftershock_next:
        return 2
    if winner.win_streak >= 2:
        return 2
    return 1


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
                    "winStreak": you.win_streak if you else 0,
                },
                # rules + timer visibility
                "jokerRound": is_joker_round(room),
                "aftershockNext": room.aftershock_next,
                "lockDeadlineTs": room.lock_deadline_ts,
                "pendingAward": room.pending_award,  # lets UI show “(+2)” without changing scoreboard early
            }
            await ws.send_text(json.dumps(payload))
        except Exception:
            dead.append(ws)

    for ws in dead:
        room.sockets.discard(ws)
        room.socket_to_player.pop(ws, None)


def cancel_timer(room: Room) -> None:
    if room.timer_task and not room.timer_task.done():
        room.timer_task.cancel()
    room.timer_task = None
    room.lock_deadline_ts = None


# ============================================================
# Stronger bot behavior (keeps your spirit, but handles new rules)
# ============================================================

def bot_pick_index(n: int, pressure: float) -> int:
    if n <= 1:
        return 0
    p = max(0.0, min(1.0, pressure))
    skew = p ** 1.2
    idx = int(skew * (n - 1))
    return max(0, min(n - 1, idx))


def estimate_tie_risk(rank: int, n_players: int) -> float:
    # medium ranks are more common tie-ish; extremes slightly safer
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

    joker = is_joker_round(room)
    hand_sorted = sorted(bot.hand, key=lambda c: c["rank"])
    n = len(hand_sorted)

    scores = [p.score for p in room.players]
    leader = max(scores) if scores else 0
    trailing_by = leader - bot.score

    rounds_left = len(bot.hand)

    # Pressure scale
    if trailing_by >= 3:
        pressure = 0.92
    elif trailing_by == 2:
        pressure = 0.82
    elif trailing_by == 1:
        pressure = 0.66
    elif trailing_by == 0:
        pressure = 0.46
    else:
        pressure = 0.30

    # Endgame ramps up
    if rounds_left <= 4:
        pressure = min(0.96, pressure + 0.18)
    if rounds_left <= 2:
        pressure = min(0.98, pressure + 0.12)

    # If Joker round, invert behavior (low wins)
    if joker:
        # If trailing, go lower even harder. If leading, avoid absolute minimum sometimes.
        if trailing_by >= 1:
            pressure_low = 0.10
        else:
            pressure_low = 0.22
        idx = bot_pick_index(n, pressure_low)
        chosen = hand_sorted[idx]
    else:
        idx = bot_pick_index(n, pressure)
        chosen = hand_sorted[idx]

        # Avoid tie-ish ranks if possible
        risk = estimate_tie_risk(rank_value(chosen), len(room.players))
        if risk > 0.28 and n >= 3:
            if trailing_by >= 1:
                idx = min(n - 1, idx + 1)
            else:
                idx = max(0, idx - 1)
            chosen = hand_sorted[idx]

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
# Table talk timer
# ============================================================

async def timer_countdown(room: Room, seconds: int = 10) -> None:
    # Starts after first lock. If deadline hits, auto-lock remaining with random cards.
    room.lock_deadline_ts = time.time() + float(seconds)
    await broadcast_state(room)

    try:
        while True:
            await asyncio.sleep(0.2)
            if room.phase != "lock_in":
                return
            if all_locked(room):
                return
            if room.lock_deadline_ts is None:
                return
            if time.time() >= room.lock_deadline_ts:
                break

        # Auto-lock remaining
        for p in room.players:
            if not p.locked and p.hand:
                chosen = random.choice(p.hand)
                p.hand.remove(chosen)
                room.pending_plays[p.id] = chosen
                p.locked = True

        await broadcast_state(room)
        await maybe_start_reveal(room)
    finally:
        # timer ends either way
        room.lock_deadline_ts = None
        room.timer_task = None


def ensure_timer_started(room: Room) -> None:
    if room.timer_task is not None:
        return
    # Start only in lock_in once first lock happens
    if room.phase != "lock_in":
        return
    room.timer_task = asyncio.create_task(timer_countdown(room, seconds=10))


# ============================================================
# Game flow
# ============================================================

async def reset_to_lobby(room: Room) -> None:
    # Keep humans + host; clear bots; go lobby
    cancel_timer(room)
    room.phase = "lobby"
    room.round = 0
    room.rounds_played = 0
    room.pending_plays = {}
    room.last_reveal = None
    room.pending_award = None
    room.history = []
    room.aftershock_next = False

    for p in room.players:
        p.score = 0
        p.win_streak = 0
        p.locked = False
        p.hand = []

    await broadcast_state(room)


async def start_game(room: Room) -> None:
    if room.phase != "lobby":
        return
    if room.starting:
        return
    # allow solo start
    if len(human_players(room)) < 1:
        return

    room.starting = True
    await broadcast_state(room)
    await asyncio.sleep(0.12)

    # Fill bots now (not while joining)
    ensure_bot_fill(room)

    # reset
    cancel_timer(room)
    room.round = 1
    room.rounds_played = 0
    room.phase = "lock_in"
    room.pending_plays = {}
    room.last_reveal = None
    room.pending_award = None
    room.history = []
    room.aftershock_next = False

    for p in room.players:
        p.score = 0
        p.win_streak = 0
        p.locked = False
        p.hand = []

    deal_equally(room)
    await schedule_bots(room)

    room.starting = False
    await broadcast_state(room)


async def restart_same_lobby(room: Room) -> None:
    # Same humans, same sockets, same code.
    # Clear bots, then refill on start into lock_in.
    cancel_timer(room)

    # Keep humans as-is
    humans = human_players(room)
    room.players = humans

    # host remains if still present, else first human
    if room.host_id is None or find_player(room, room.host_id) is None:
        room.host_id = humans[0].id if humans else None

    # Now start a new game immediately (fills bots)
    await start_game(room)


async def maybe_start_reveal(room: Room) -> None:
    if room.phase != "lock_in":
        return
    if not all_locked(room):
        return
    if room.advancing:
        return

    room.advancing = True
    try:
        await do_reveal_and_advance(room)
    finally:
        room.advancing = False


async def do_reveal_and_advance(room: Room) -> None:
    # Stop timer immediately once reveal begins
    cancel_timer(room)

    room.phase = "reveal"

    order = [p.id for p in room.players]
    random.shuffle(order)

    plays = []
    for pid in order:
        card = room.pending_plays.get(pid)
        if card is None:
            card = {"id": "X", "suit": "S", "rank": 2, "label": "?", "symbol": "?", "color": "black"}
        plays.append({"playerId": pid, "card": card})

    joker = is_joker_round(room)

    values = [(x["playerId"], rank_value(x["card"])) for x in plays]
    if values:
        if joker:
            best_val = min(v for _, v in values)
        else:
            best_val = max(v for _, v in values)
    else:
        best_val = 0

    top_ids = [pid for pid, v in values if v == best_val]

    explosion = len(top_ids) > 1
    winner_id = None
    winner_card = None
    points = 0

    # Compute outcome now, but DO NOT update score until reveal pacing finishes
    if not explosion and top_ids:
        winner_id = top_ids[0]
        w = find_player(room, winner_id)
        if w:
            points = round_point_value(room, w)
        for x in plays:
            if x["playerId"] == winner_id:
                winner_card = x["card"]
                break

    room.last_reveal = {
        "round": room.round,
        "joker": joker,
        "aftershockNext": room.aftershock_next,
        "order": order,
        "plays": plays,
        "winnerId": winner_id,
        "explosion": explosion,
        "topIds": top_ids,
        "topRank": best_val,
        "winnerCard": winner_card,
        "points": points,  # shown in banner without spoiling scoreboard
    }

    room.pending_award = {
        "winnerId": winner_id,
        "explosion": explosion,
        "points": points,
        "joker": joker,
    }

    await broadcast_state(room)

    # Reveal pacing
    base = 2.2
    per_card = 0.75
    extra_pause_after = 2.4
    wait_s = base + per_card * max(1, len(room.players)) + extra_pause_after
    await asyncio.sleep(wait_s)

    # Apply scoring AFTER reveal completes
    if explosion:
        # Explosion breaks streaks
        for p in room.players:
            p.win_streak = 0
        # Aftershock: next round worth 2
        room.aftershock_next = True
    else:
        if winner_id:
            w = find_player(room, winner_id)
            if w:
                w.score += points
                w.win_streak += 1
                # Everyone else streak resets
                for p in room.players:
                    if p.id != winner_id:
                        p.win_streak = 0
        # Aftershock consumed (if it was active)
        room.aftershock_next = False

    # Only now append history (no spoilers pre-reveal)
    room.history.insert(
        0,
        {
            "round": room.round,
            "winnerId": winner_id,
            "explosion": explosion,
            "topRank": best_val,
            "winnerCard": winner_card,
            "points": points,
            "joker": joker,
        },
    )
    room.history = room.history[:10]

    room.rounds_played += 1

    # Reset lock states
    for p in room.players:
        p.locked = False

    room.pending_plays = {}
    room.last_reveal = None
    room.pending_award = None

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
    <title>Sorprese</title>
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
      .sub{ margin:0 0 18px; color:var(--muted); max-width: 980px; line-height:1.35; }

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

      #gameTop{ display:flex; flex-wrap:wrap; gap:10px; align-items:center; margin-top: 10px; }

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
        box-shadow: inset 0 0 80px rgba(0,0,0,0.35);
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

      .pos0{ left: 50%; top: 12px; transform: translateX(-50%); }
      .pos1{ right: 18px; top: 80px; }
      .pos2{ right: 18px; bottom: 80px; }
      .pos3{ left: 50%; bottom: 12px; transform: translateX(-50%); }
      .pos4{ left: 18px; bottom: 80px; }
      .pos5{ left: 18px; top: 80px; }

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
        min-height: 170px;
      }

      .playStack{
        display:flex;
        flex-direction:column;
        align-items:center;
        gap:6px;
      }

      .playName{ font-size:12px; color: rgba(255,255,255,0.70); max-width: 120px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; }

      /* ============================================================
         CARDS: prettier + brighter selection ring
         ============================================================ */
      .card{
        width: 82px;
        height: 116px;
        border-radius: 16px;
        border: 1px solid rgba(0,0,0,0.22);
        background:
          radial-gradient(circle at 30% 20%, rgba(255,255,255,0.92) 0%, rgba(255,255,255,0.78) 45%, rgba(255,255,255,0.88) 100%);
        box-shadow:
          0 18px 40px rgba(0,0,0,0.30),
          0 2px 0 rgba(255,255,255,0.65) inset;
        display:flex;
        flex-direction:column;
        justify-content:space-between;
        padding:10px;
        user-select:none;
        transition: transform 0.12s ease, box-shadow 0.12s ease, outline 0.12s ease, filter 0.12s ease;
        position: relative;
      }

      .card:hover{
        transform: translateY(-3px);
        box-shadow:
          0 22px 52px rgba(0,0,0,0.36),
          0 2px 0 rgba(255,255,255,0.65) inset;
      }

      .card.selected{
        outline: 4px solid rgba(255,255,255,0.92);
        box-shadow:
          0 0 0 6px rgba(218,182,122,0.55),
          0 0 22px rgba(218,182,122,0.70),
          0 24px 60px rgba(0,0,0,0.38),
          0 2px 0 rgba(255,255,255,0.65) inset;
        transform: translateY(-4px);
        filter: brightness(1.04);
      }

      .card::after{
        content:"";
        position:absolute;
        inset: 10px;
        border-radius: 12px;
        background:
          radial-gradient(circle at 70% 30%, rgba(0,0,0,0.05) 0%, rgba(0,0,0,0) 42%),
          radial-gradient(circle at 30% 70%, rgba(0,0,0,0.04) 0%, rgba(0,0,0,0) 45%);
        pointer-events:none;
      }

      .corner{ font-weight: 900; font-size: 20px; }
      .suit{ font-size: 28px; align-self:flex-end; }

      .red{ color: #c1121f; }
      .black{ color: #0f0f14; }

      .card.back{
        background:
          radial-gradient(circle at 30% 30%, rgba(255,255,255,0.10) 0%, rgba(0,0,0,0) 45%),
          repeating-linear-gradient(45deg, rgba(255,255,255,0.06) 0 8px, rgba(0,0,0,0.00) 8px 16px),
          linear-gradient(135deg, #101016 0%, #2a2a33 100%);
        border-color: rgba(255,255,255,0.12);
        box-shadow: 0 14px 34px rgba(0,0,0,0.65);
      }

      /* Flow upgrades */
      .lockedStamp{
        position:absolute;
        right: 8px;
        bottom: 8px;
        font-size: 11px;
        font-weight: 900;
        letter-spacing: 0.6px;
        padding: 6px 8px;
        border-radius: 10px;
        background: rgba(0,0,0,0.72);
        border: 1px solid rgba(255,255,255,0.18);
        color: rgba(255,255,255,0.92);
        transform: rotate(-6deg);
        text-transform: uppercase;
        pointer-events:none;
      }

      .winnerGlow{
        outline: 4px solid rgba(218,182,122,0.95) !important;
        box-shadow:
          0 0 0 6px rgba(218,182,122,0.45),
          0 0 28px rgba(218,182,122,0.85),
          0 26px 70px rgba(0,0,0,0.40),
          0 2px 0 rgba(255,255,255,0.65) inset !important;
        animation: winnerPulse 1.5s ease-out forwards;
      }

      @keyframes winnerPulse{
        0%{ filter: brightness(1.02); }
        40%{ filter: brightness(1.12); }
        100%{ filter: brightness(1.02); }
      }

      .shake{
        animation: shake 520ms ease-in-out;
      }

      @keyframes shake{
        0%{ transform: translate(0,0); }
        15%{ transform: translate(-6px, 2px); }
        30%{ transform: translate(6px, -2px); }
        45%{ transform: translate(-5px, -1px); }
        60%{ transform: translate(5px, 2px); }
        75%{ transform: translate(-3px, 0px); }
        100%{ transform: translate(0,0); }
      }

      .below{
        margin-top: 14px;
        display:grid;
        grid-template-columns: 1fr;
        gap: 16px;
      }
      @media (min-width: 900px){
        .below{ grid-template-columns: 1fr 420px; }
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

      .primary{ border-color: rgba(218,182,122,0.70); }
      .primary:hover{ border-color: rgba(218,182,122,1.0); }

      /* Timer pill */
      .timerPill{
        border-color: rgba(218,182,122,0.35);
      }

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
      <h1>Sorprese</h1>
      <p class="sub">
        A game of nerve and timing. Highest card usually wins.
        <b>Every 5th round is Joker</b> (lowest wins).
        Tie at the top (or bottom on Joker) = <b>Explosion</b> — no points, and the next round is worth 2.
        Win two rounds in a row and your next wins become worth 2.
      </p>

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
            <button id="playAgainBtn" class="primary" onclick="playAgainSameLobby()" disabled>Play again (same lobby)</button>
            <button onclick="newGameSameSize()">New room</button>
            <button onclick="backToLobby()">Back to lobby</button>
          </div>

          <div class="status" style="margin-top:10px;">Tip: stay in the room and hit Play again to run it back.</div>
        </div>
      </div>

      <div id="game" class="hide">
        <div id="gameTop">
          <div class="pill">Room: <span class="code" id="roomcode"></span></div>
          <div class="pill">Round: <span id="round">-</span></div>
          <div class="pill">Rounds played: <span id="roundsPlayed">0</span></div>
          <div class="pill">Phase: <span id="phase">-</span></div>
          <div class="pill timerPill hide" id="timerPill">Timer: <span id="timerText">10.0</span>s</div>

          <button onclick="copyRoom()">Copy room code</button>
          <button id="musicBtn" onclick="toggleMusic()">Music: Off</button>
        </div>

        <div class="tableShell">
          <div class="table" id="tableEl">
            <div id="seats"></div>

            <div class="center">
              <div class="centerHint" id="tableHint">Waiting…</div>
              <div class="centerHint" id="dealerLine" style="margin-top:-2px; opacity:0.9;"></div>
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

      // Hide history until client reveal completes
      let revealDone = true;

      function byId(x){ return document.getElementById(x); }

      // -------------------
      // SOUND (Web Audio)
      // -------------------
      let audioCtx = null;

      function ensureAudio(){
        if (!audioCtx){
          audioCtx = new (window.AudioContext || window.webkitAudioContext)();
        }
        if (audioCtx.state === "suspended") audioCtx.resume();
      }

      function tone(freq, duration=0.08, type="sine", gain=0.05){
        ensureAudio();
        const t0 = audioCtx.currentTime;
        const osc = audioCtx.createOscillator();
        const g = audioCtx.createGain();
        osc.type = type;
        osc.frequency.value = freq;
        g.gain.setValueAtTime(gain, t0);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
        osc.connect(g);
        g.connect(audioCtx.destination);
        osc.start(t0);
        osc.stop(t0 + duration);
      }

      function noise(duration=0.12, gain=0.06){
        ensureAudio();
        const t0 = audioCtx.currentTime;
        const bufferSize = Math.floor(audioCtx.sampleRate * duration);
        const buffer = audioCtx.createBuffer(1, bufferSize, audioCtx.sampleRate);
        const data = buffer.getChannelData(0);
        for (let i = 0; i < bufferSize; i++) data[i] = (Math.random() * 2 - 1) * 0.9;

        const src = audioCtx.createBufferSource();
        src.buffer = buffer;
        const g = audioCtx.createGain();
        g.gain.setValueAtTime(gain, t0);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + duration);
        src.connect(g);
        g.connect(audioCtx.destination);
        src.start(t0);
        src.stop(t0 + duration);
      }

      function sfxChip(){
        tone(680, 0.04, "square", 0.03);
        tone(520, 0.06, "triangle", 0.025);
      }

      function sfxFlip(){
        noise(0.05, 0.04);
        tone(240, 0.05, "sine", 0.02);
      }

      function sfxWin(){
        tone(523, 0.09, "triangle", 0.04);
        tone(659, 0.10, "triangle", 0.035);
        tone(784, 0.12, "triangle", 0.03);
      }

      function sfxBoom(){
        noise(0.16, 0.08);
        ensureAudio();
        const t0 = audioCtx.currentTime;
        const osc = audioCtx.createOscillator();
        const g = audioCtx.createGain();
        osc.type = "sawtooth";
        osc.frequency.setValueAtTime(180, t0);
        osc.frequency.exponentialRampToValueAtTime(40, t0 + 0.18);
        g.gain.setValueAtTime(0.06, t0);
        g.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.20);
        osc.connect(g);
        g.connect(audioCtx.destination);
        osc.start(t0);
        osc.stop(t0 + 0.22);
      }

      // -------------------
      // Ambient music (no files)
      // -------------------
      let musicOn = false;
      let musicNodes = null;

      function startMusic(){
        ensureAudio();
        if (musicNodes) return;

        // gentle pad: two detuned sines + slow filter movement
        const osc1 = audioCtx.createOscillator();
        const osc2 = audioCtx.createOscillator();
        const gain = audioCtx.createGain();
        const filt = audioCtx.createBiquadFilter();

        osc1.type = "sine";
        osc2.type = "sine";
        osc1.frequency.value = 110; // A2
        osc2.frequency.value = 110.7;

        filt.type = "lowpass";
        filt.frequency.value = 520;

        gain.gain.value = 0.0;

        osc1.connect(filt);
        osc2.connect(filt);
        filt.connect(gain);
        gain.connect(audioCtx.destination);

        osc1.start();
        osc2.start();

        // fade in
        const t0 = audioCtx.currentTime;
        gain.gain.setValueAtTime(0.0001, t0);
        gain.gain.exponentialRampToValueAtTime(0.018, t0 + 1.2);

        // slow filter drift
        const lfo = audioCtx.createOscillator();
        const lfoGain = audioCtx.createGain();
        lfo.type = "sine";
        lfo.frequency.value = 0.06;
        lfoGain.gain.value = 220;
        lfo.connect(lfoGain);
        lfoGain.connect(filt.frequency);
        lfo.start();

        musicNodes = {osc1, osc2, gain, filt, lfo, lfoGain};
      }

      function stopMusic(){
        if (!musicNodes) return;
        ensureAudio();
        const {osc1, osc2, gain, lfo} = musicNodes;

        const t0 = audioCtx.currentTime;
        gain.gain.cancelScheduledValues(t0);
        gain.gain.setValueAtTime(gain.gain.value, t0);
        gain.gain.exponentialRampToValueAtTime(0.0001, t0 + 0.6);

        setTimeout(() => {
          try{ osc1.stop(); }catch(e){}
          try{ osc2.stop(); }catch(e){}
          try{ lfo.stop(); }catch(e){}
          musicNodes = null;
        }, 650);
      }

      function toggleMusic(){
        // user gesture required to start audio in most browsers
        musicOn = !musicOn;
        const btn = byId("musicBtn");
        if (musicOn){
          startMusic();
          btn.textContent = "Music: On";
          sfxChip();
        } else {
          stopMusic();
          btn.textContent = "Music: Off";
          sfxChip();
        }
      }

      // Dealer narration
      function setDealer(text){
        const el = byId("dealerLine");
        if (!el) return;
        el.textContent = text || "";
      }

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

      function cardEl(card, clickable, selected, showLockedStamp=false) {
        const el = document.createElement("div");
        el.className = "card " + (card.color === "red" ? "red" : "black");
        if (selected) el.classList.add("selected");

        el.innerHTML = `
          <div class="corner">${card.label}</div>
          <div class="suit">${card.symbol}</div>
        `;

        if (showLockedStamp){
          const stamp = document.createElement("div");
          stamp.className = "lockedStamp";
          stamp.textContent = "LOCKED";
          el.appendChild(stamp);
        }

        if (clickable) {
          el.style.cursor = "pointer";
          el.onclick = () => {
            sfxFlip();
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
        sfxChip();
      }

      function renderLobby(players, hostId, youId, phase){
        const panel = byId("lobbyPanel");
        if (!players || !youId) return;

        panel.classList.remove("hide");

        const humans = players.filter(p => !p.isBot);
        byId("lobbyHint").textContent = `Humans in lobby: ${humans.length}. Host starts when ready (solo allowed).`;

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

          let streak = "";
          if (p.winStreak >= 2) streak = " 🔥 streak";

          row.innerHTML = `<div>${icon} <b>${p.name}</b> <span style="color:rgba(255,255,255,0.55);">(${status}${streak})</span></div>
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

          if (h.joker) left += " — 🃏 Joker";
          if (h.explosion) {
            left += ` — 💥 Explosion`;
            right = `Top: ${h.topRank}`;
          } else {
            const winner = playersById[h.winnerId];
            const wname = winner ? winner.name : "Winner";
            const wc = h.winnerCard;
            left += ` — 🏆 ${wname}`;
            right = wc ? `Won with ${cardText(wc)} (+${h.points})` : `(+${h.points})`;
          }

          row.innerHTML = `<div>${left}</div><div class="historyRight">${right}</div>`;
          el.appendChild(row);
        }
      }

      function renderTimer(deadlineTs, phase){
        const pill = byId("timerPill");
        const txt = byId("timerText");

        if (phase !== "lock_in" || !deadlineTs){
          pill.classList.add("hide");
          return;
        }
        pill.classList.remove("hide");

        const now = Date.now() / 1000.0;
        const left = Math.max(0, deadlineTs - now);
        txt.textContent = left.toFixed(1);
      }

      function renderTable(phase, lastReveal, playersById, pendingAward) {
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
          // Rule hints
          if (lastState && lastState.jokerRound) {
            hint.textContent = "🃏 Joker Round: lowest card wins. Lock in fast.";
          } else if (lastState && lastState.aftershockNext) {
            hint.textContent = "⚡ Aftershock: this round is worth 2.";
          } else {
            hint.textContent = "Cards are face down. Reveal happens when everyone locks in.";
          }

          const ids = Object.keys(playersById);
          for (const pid of ids) {
            const wrap = document.createElement("div");
            wrap.className = "playStack";
            wrap.dataset.pid = pid;

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
            wrap.dataset.pid = pid;

            const name = document.createElement("div");
            name.className = "playName";
            name.textContent = player ? player.name : pid;

            wrap.appendChild(name);

            if (j <= i) {
              wrap.appendChild(cardEl(playMap[pid], false, false));
            } else {
              wrap.appendChild(cardBackEl());
            }

            el.appendChild(wrap);
          }

          sfxFlip();

          i++;
          if (i < order.length) {
            setTimeout(step, 900);
          } else {
            // Banner: use pendingAward points (score not updated yet, on purpose)
            const pts = (pendingAward && pendingAward.points) ? pendingAward.points : (lastReveal.points || 1);

            if (explosion) {
              sfxBoom();
              rt.innerHTML = `<div class="banner tie">💥 Explosion — tie at the top. No point awarded. Next round worth 2.</div>`;
              setDealer("Dealer: A tie at the top. Boom. Next hand is worth blood.");

              const t = byId("tableEl");
              if (t){
                t.classList.remove("shake");
                void t.offsetWidth;
                t.classList.add("shake");
                setTimeout(() => { try{ t.classList.remove("shake"); }catch(e){} }, 650);
              }
            } else {
              sfxWin();
              const winner = playersById[winnerId];
              const wname = winner ? winner.name : "Winner";
              const wc = lastReveal.winnerCard;
              const wct = wc ? ` with ${cardText(wc)}` : "";
              const jokerTag = lastReveal.joker ? " 🃏" : "";
              rt.innerHTML = `<div class="banner win">🏆 ${wname}${jokerTag} wins (+${pts})${wct}.</div>`;
              setDealer(`Dealer: ${wname} takes it. (+${pts})`);

              const winningWrap = el.querySelector(`[data-pid="${winnerId}"]`);
              if (winningWrap){
                const cardNode = winningWrap.querySelector(".card");
                if (cardNode){
                  cardNode.classList.add("winnerGlow");
                  setTimeout(() => { try{ cardNode.classList.remove("winnerGlow"); }catch(e){} }, 1500);
                }
              }
            }

            // Now allow history render (client-side)
            revealDone = true;
            if (lastState) renderHistory(lastState.history, playersById);
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

        // Sort high -> low for readability (A, K, Q...)
        const sorted = [...hand].sort((a,b) => (b.rank||0) - (a.rank||0));

        const canSelect = (phase === "lock_in" && !locked);
        for (const c of sorted) {
          const selected = (selectedCardId === c.id);
          const showStamp = (locked && selected);
          el.appendChild(cardEl(c, canSelect, selected, showStamp));
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

        sfxChip();
        ws.send(JSON.stringify({ type: "lock", cardId: selectedCardId }));
      }

      function hostStart(){
        if (!ws) return;
        sfxChip();
        ws.send(JSON.stringify({ type: "start" }));
      }

      function playAgainSameLobby(){
        if (!ws || !lastState) return;
        sfxChip();
        ws.send(JSON.stringify({ type: "restart" }));
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

      function showGameOver(players, hostId, youId){
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

        const playAgainBtn = byId("playAgainBtn");
        const iAmHost = (youId && hostId && youId === hostId);
        playAgainBtn.disabled = !iAmHost;

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
        setDealer("");
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

            // reveal phase -> hide history until animation completes
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

            // Dealer baseline lines with rule spice
            if (msg.phase === "lobby") setDealer("Dealer: Waiting on the table to fill.");
            if (msg.phase === "lock_in") {
              if (msg.jokerRound) setDealer("Dealer: Joker hand. Lowest wins. Watch your pride.");
              else if (msg.aftershockNext) setDealer("Dealer: Aftershock. This one’s worth 2.");
              else setDealer("Dealer: Choose carefully. Lock it in when you feel brave.");
            }
            if (msg.phase === "reveal") setDealer("Dealer: Cards on the felt. No blinking now.");
            if (msg.phase === "finished") setDealer("Dealer: That’s the last hand. Good game.");

            const playersById = {};
            for (const p of msg.players) playersById[p.id] = p;

            renderSeats(msg.players, msg.phase, msg.hostId);
            renderScoreboard(msg.players, msg.phase);
            renderLockStatus(msg.players, msg.phase);

            // Timer
            renderTimer(msg.lockDeadlineTs, msg.phase);

            // If revealing, don't show history until revealDone true
            if (msg.phase !== "reveal" || revealDone) {
              renderHistory(msg.history, playersById);
            } else {
              byId("history").innerHTML = `<div class="status">Revealing…</div>`;
            }

            // keep selected card valid
            if (msg.you && msg.you.hand) {
              const exists = msg.you.hand.some(c => c.id === selectedCardId);
              if (!exists) selectedCardId = null;
            }

            renderTable(msg.phase, msg.lastReveal, playersById, msg.pendingAward);
            renderHand((msg.you && msg.you.hand) ? msg.you.hand : [], (msg.you && msg.you.locked) ? true : false, msg.phase);

            // Game over overlay
            if (msg.phase === "finished") {
              showGameOver(msg.players, msg.hostId, msg.you.id);
            } else {
              closeGameOver();
            }
          }
        };

        ws.onclose = () => {
          byId("status").textContent = "Disconnected. Refresh and rejoin.";
        };
      }

      // keep timer updating smoothly client-side
      setInterval(() => {
        if (!lastState) return;
        renderTimer(lastState.lockDeadlineTs, lastState.phase);
      }, 120);

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
                if len(human_players(room)) < 1:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Need at least 1 human player."}))
                    continue

                asyncio.create_task(start_game(room))
                continue

            if mtype == "restart":
                # play again same lobby (only host, only when finished)
                if room.phase != "finished":
                    await websocket.send_text(json.dumps({"type": "error", "message": "Can only restart after the game ends."}))
                    continue
                if room.host_id != player_id:
                    await websocket.send_text(json.dumps({"type": "error", "message": "Only the host can restart."}))
                    continue

                asyncio.create_task(restart_same_lobby(room))
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

                # Start timer on first lock
                ensure_timer_started(room)

                # speed up bots once a human locks
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
            # remove player
            room.players = [p for p in room.players if p.id != player_id]

            # host handoff to first remaining human
            if room.host_id == player_id:
                humans = human_players(room)
                room.host_id = humans[0].id if humans else None

            # If nobody human remains, mark finished and cancel timer
            if len(human_players(room)) < 1:
                cancel_timer(room)
                room.phase = "finished"

        await broadcast_state(room)
