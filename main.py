# main.py - Town of Shadows (complete backend)
# Run: uvicorn main:app --host 0.0.0.0 --port $PORT

import asyncio
import json
import random
import time
from typing import Dict, Any, List, Optional
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ---- Configuration ----
FRONTEND_ORIGINS = [
    "https://narutowjouanki669-hash.github.io",
    "https://narutowjouanki669-hash.github.io/game-trial",
    "http://localhost:5500",
    "http://127.0.0.1:5500",
]

NIGHT_SECONDS = 40
DAY_DISCUSS = 60
DAY_VOTE = 20
DAY_DEFENCE = 10
DAY_FINAL = 10
TOTAL_PLAYERS = 20

# ---- Role pools (Villager removed) ----
TOWN_POOL = [
    "Doctor", "Detective", "Bodyguard", "Vigilante", "Jailor", "Soldier",
    "Cupid", "Gossip", "Lookout", "Mayor", "Investigator", "Escort", "Medium"
]
MAFIA_POOL = [
    "Godfather", "Mafioso", "Janitor", "Spy", "Beastman", "Blackmailer", "Framer"
]
CULT_POOL = ["Cult Leader", "Fanatic", "Infiltrator", "Prophet", "Acolyte"]
NEUTRAL_POOL = ["Jester", "Executioner", "Serial Killer", "Arsonist", "Survivor", "Amnesiac", "Witch", "Guardian Angel"]

def role_to_faction(role: str) -> str:
    if role in TOWN_POOL:
        return "Town"
    if role in MAFIA_POOL:
        return "Mafia"
    if role in CULT_POOL:
        return "Cult"
    if role in NEUTRAL_POOL:
        return "Neutral"
    return "Unknown"

# ---- App & CORS ----
app = FastAPI(title="Town of Shadows")

app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS + ["*"],  # include wildcard while testing; remove "*" in production if desired
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---- In-memory state ----
rooms: Dict[str, Dict[str, Any]] = {}
ws_managers: Dict[str, Dict[str, WebSocket]] = {}  # room_id -> wsid -> websocket

# ---- Request models ----
class CreateRoomReq(BaseModel):
    host_name: Optional[str] = "Host"

class JoinReq(BaseModel):
    roomId: str
    name: Optional[str] = "Player"

class ActionReq(BaseModel):
    room_id: str
    actor: str
    target: str
    type: str

# ---- Helpers: role sampling & room creation ----
def sample_roles_for_game() -> List[str]:
    """
    Produce TOTAL_PLAYERS roles meeting constraints:
    - Town ≈ 8
    - Mafia exactly 4 with Godfather + >=1 Mafioso
    - Cult 3 (Leader + Fanatic + Acolyte)
    - Neutrals 3
    - Fill remainder with town roles
    """
    roles: List[str] = []
    # Town baseline (8)
    while len(roles) < 8:
        roles.append(random.choice(TOWN_POOL))
    # Mafia baseline: Godfather + Mafioso
    mafia = ["Godfather", "Mafioso"]
    remaining_mafia = [r for r in MAFIA_POOL if r not in mafia]
    while len(mafia) < 4:
        # allow mafioso duplication or add other role
        mafia.append(random.choice(remaining_mafia + ["Mafioso"]))
    # ensure at least one non-Godfather/Mafioso
    if all(m in ("Godfather", "Mafioso") for m in mafia):
        mafia[2] = random.choice([r for r in remaining_mafia if r != "Mafioso"])
    roles.extend(mafia)
    # Cult: leader + fanatic + acolyte
    roles.extend(["Cult Leader", "Fanatic", "Acolyte"])
    # Neutrals exactly 3
    roles.extend(random.sample(NEUTRAL_POOL, 3))
    # Fill remainder with town roles
    while len(roles) < TOTAL_PLAYERS:
        roles.append(random.choice(TOWN_POOL))
    random.shuffle(roles)
    return roles

def create_room_obj(host_name: str = "Host") -> Dict[str, Any]:
    rid = str(uuid4())[:6].upper()
    roles = sample_roles_for_game()
    players = []
    for i in range(1, TOTAL_PLAYERS + 1):
        r = roles[i-1]
        players.append({
            "slot": i,
            "name": f"Player {i}",
            "is_bot": True,
            "alive": True,
            "role": r,
            "faction": role_to_faction(r),
            "ws_id": None,
            "revealed": False,
            "jailor_has_execute": True if r == "Jailor" else False,
            "soldier_used": False,
            "doused": False,
            "contacted": False,
            "culted": False,
            "cleaned": False,
        })
    room = {
        "id": rid,
        "host": host_name,
        "players": players,
        "state": "waiting",   # waiting | active | ended
        "phase": "waiting",
        "day": 0,
        "actions": [],
        "votes": {},
        "lovers": {},         # cupid pairs
        "controller_task": None,
        "cleaned_bodies": set(),
        "accused": None,
        "verdict_votes": {},
        "accusation_history": [],
        "seen_tutorial": set(),
    }
    rooms[rid] = room
    ws_managers[rid] = {}
    return room

def room_summary(room: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": room["id"],
        "host": room["host"],
        "state": room["state"],
        "phase": room["phase"],
        "day": room["day"],
        "players": [
            {
                "slot": p["slot"],
                "name": p["name"],
                "alive": p["alive"],
                "revealed": p["revealed"],
                "is_bot": p["is_bot"],
                "role": p["role"] if p["revealed"] else None,
                "faction": p["faction"],
            } for p in room["players"]
        ],
        "accused": room.get("accused"),
    }

# ---- REST endpoints ----
@app.get("/test")
async def test():
    return {"message": "Hello from Town of Shadows backend"}

@app.post("/create-room")
async def create_room(req: CreateRoomReq):
    room = create_room_obj(req.host_name)
    return {"roomId": room["id"], "room": room_summary(room)}

@app.post("/join-room")
async def join_room(req: JoinReq):
    rid = req.roomId
    if rid not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    room = rooms[rid]
    slot = next((p for p in room["players"] if p["is_bot"]), None)
    if not slot:
        raise HTTPException(status_code=400, detail="Room full")
    slot["is_bot"] = False
    slot["name"] = req.name or slot["name"]
    return {"slot": slot["slot"], "role": slot["role"], "faction": slot["faction"], "room": room_summary(room)}

@app.get("/room/{room_id}")
async def get_room(room_id: str):
    if room_id not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    return room_summary(rooms[room_id])

@app.post("/queue-action")
async def queue_action(req: ActionReq):
    rid = req.room_id
    if rid not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    room = rooms[rid]
    if not room["phase"].startswith("night"):
        raise HTTPException(status_code=400, detail="Actions only allowed at night")
    room.setdefault("actions", []).append({
        "actor": req.actor, "target": req.target, "type": req.type, "ts": time.time(), "actor_role": None
    })
    return {"ok": True}

# ---- WebSocket helpers ----
async def send_to_ws(room_id: str, wsid: str, message: dict):
    mgr = ws_managers.get(room_id, {})
    ws = mgr.get(wsid)
    if not ws:
        return
    try:
        await ws.send_text(json.dumps(message))
    except Exception:
        mgr.pop(wsid, None)

async def broadcast(room_id: str, message: dict):
    mgr = ws_managers.get(room_id, {})
    dead = []
    for wsid, ws in list(mgr.items()):
        try:
            await ws.send_text(json.dumps(message))
        except Exception:
            dead.append(wsid)
    for d in dead:
        mgr.pop(d, None)

async def send_to_player(room_id: str, player_name: str, message: dict):
    room = rooms.get(room_id)
    if not room:
        return
    p = next((x for x in room["players"] if x["name"] == player_name), None)
    if not p:
        return
    wsid = p.get("ws_id")
    if not wsid:
        return
    await send_to_ws(room_id, wsid, message)

def build_faction_list_for_player(room: Dict[str, Any], viewer: Dict[str, Any]):
    """
    Returns faction mates with role visibility (Fanatic/Spy contact rules respected).
    """
    faction = viewer.get("faction")
    items = []
    for p in room["players"]:
        if p["faction"] != faction:
            continue
        # Fanatic hidden until contacted (unless viewer is Fanatic or Cult Leader)
        if p["role"] == "Fanatic" and not p.get("contacted", False):
            if viewer["role"] not in ("Fanatic", "Cult Leader"):
                continue
        # Spy hidden until contacted
        if p["role"] == "Spy" and not p.get("contacted", False):
            continue
        items.append({"slot": p["slot"], "role": p["role"], "name": p["name"], "alive": p["alive"]})
    return items

async def send_faction_mates(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    for p in room["players"]:
        if not p.get("ws_id"):
            continue
        if p.get("faction") in ("Mafia", "Cult"):
            mates = build_faction_list_for_player(room, p)
            await send_to_player(room_id, p["name"], {"type": "faction_mates", "mates": mates})

# ---- WebSocket endpoint ----
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    if room_id not in rooms:
        await websocket.send_text(json.dumps({"type": "system", "text": "Room not found"}))
        await websocket.close()
        return
    wsid = str(uuid4())
    ws_managers[room_id][wsid] = websocket
    try:
        await websocket.send_text(json.dumps({"type": "system", "text": f"Connected to {room_id}", "ws_id": wsid}))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await websocket.send_text(json.dumps({"type": "system", "text": "Invalid JSON"}))
                continue
            await handle_ws_message(room_id, wsid, msg)
    except WebSocketDisconnect:
        ws_managers[room_id].pop(wsid, None)
    except Exception:
        ws_managers[room_id].pop(wsid, None)

# ---- WS message handling ----
async def handle_ws_message(room_id: str, wsid: str, msg: dict):
    mtype = msg.get("type")
    room = rooms.get(room_id)
    if not room:
        return

    if mtype == "identify":
        slot = msg.get("slot")
        p = next((x for x in room["players"] if x["slot"] == slot), None)
        if p:
            p["ws_id"] = wsid
            p["is_bot"] = False
            await send_to_player(room_id, p["name"], {"type": "private_role", "slot": p["slot"], "role": p["role"], "faction": p["faction"]})
            show_tut = p["name"] not in room.get("seen_tutorial", set())
            if show_tut:
                room.setdefault("seen_tutorial", set()).add(p["name"])
            await send_to_player(room_id, p["name"], {"type": "tutorial", "show": show_tut})
            await broadcast(room_id, {"type": "room", "room": room_summary(room)})
            await send_faction_mates(room_id)
        else:
            await send_to_ws(room_id, wsid, {"type": "system", "text": "Slot not found"})
        return

    if mtype == "chat":
        ch = msg.get("channel", "public")
        text = msg.get("text", "")
        sender = msg.get("from", "Anon")
        # shorthand numeric vote during day_vote handled here
        if room["phase"] == "day_vote" and text.strip().isdigit():
            voter = sender
            target_slot = int(text.strip())
            target_p = next((x for x in room["players"] if x["slot"] == target_slot), None)
            if target_p:
                room.setdefault("votes", {})[voter] = target_p["name"]
                await send_to_ws(room_id, wsid, {"type": "system", "text": f"You voted for Player {target_slot}"})
                await broadcast(room_id, {"type": "system", "text": f"{voter} cast a vote (anonymous)."})
                return
        if ch == "mafia":
            await send_to_faction(room_id, "Mafia", {"type": "chat", "from": sender, "text": text, "channel": "mafia"})
            return
        if ch == "cult":
            await send_to_faction(room_id, "Cult", {"type": "chat", "from": sender, "text": text, "channel": "cult"})
            return
        if ch == "dead":
            for p in room["players"]:
                if not p["alive"] and p.get("ws_id"):
                    await send_to_player(room_id, p["name"], {"type": "chat", "from": sender, "text": text, "channel": "dead"})
            return
        await broadcast(room_id, {"type": "chat", "from": sender, "text": text, "channel": "public"})
        return

    if mtype == "player_action":
        action = msg.get("action")
        if action:
            if not room["phase"].startswith("night"):
                await send_to_ws(room_id, wsid, {"type": "system", "text": "Actions only allowed at night"})
                return
            room.setdefault("actions", []).append({
                "actor": action.get("actor"),
                "target": action.get("target"),
                "type": action.get("type"),
                "ts": time.time(),
                "actor_role": action.get("actor_role")
            })
            await send_to_ws(room_id, wsid, {"type": "system", "text": "Action queued."})
        return

    if mtype == "start_game":
        try:
            await start_game(room_id)
        except Exception as e:
            await send_to_ws(room_id, wsid, {"type": "system", "text": str(e)})
        return

    if mtype == "vote":
        if room["phase"] != "day_vote":
            await send_to_ws(room_id, wsid, {"type": "system", "text": "Voting only during vote phase"})
            return
        voter = msg.get("from")
        target = msg.get("target")
        if isinstance(target, str) and target.isdigit():
            tgt = next((x for x in room["players"] if x["slot"] == int(target)), None)
            if tgt:
                room.setdefault("votes", {})[voter] = tgt["name"]
                await send_to_ws(room_id, wsid, {"type": "system", "text": f"You voted for Player {tgt['slot']}"})
                await broadcast(room_id, {"type": "system", "text": f"{voter} cast a vote (anonymous)."})
                return
        if target == "skip" or target == "SKIP":
            room.setdefault("votes", {})[voter] = "SKIP"
            await broadcast(room_id, {"type": "system", "text": f"{voter} skipped voting."})
            return
        room.setdefault("votes", {})[voter] = target
        await broadcast(room_id, {"type": "system", "text": f"{voter} voted for {target}"})
        return

    if mtype == "accuse":
        if room["phase"].startswith("night"):
            await send_to_ws(room_id, wsid, {"type": "system", "text": "Accusations only during day"})
            return
        acc_from = msg.get("from")
        acc_target = msg.get("target")
        room.setdefault("votes", {})[acc_from] = acc_target
        await broadcast(room_id, {"type": "system", "text": f"{acc_from} accused {acc_target}"})
        return

    if mtype == "verdict_vote":
        if room["phase"] != "day_final":
            await send_to_ws(room_id, wsid, {"type": "system", "text": "Verdict voting only during final verdict phase"})
            return
        voter = msg.get("from")
        choice = msg.get("choice")
        if choice not in ("guilty", "innocent"):
            await send_to_ws(room_id, wsid, {"type": "system", "text": "Invalid verdict choice"})
            return
        room.setdefault("verdict_votes", {})[voter] = choice
        await broadcast(room_id, {"type": "system", "text": f"{voter} voted {choice} on {room.get('accused')}"})
        return

    await send_to_ws(room_id, wsid, {"type": "system", "text": "Unknown message type"})

# ---- Start game & controller ----
@app.post("/start-game/{room_id}")
async def start_game(room_id: str):
    if room_id not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    room = rooms[room_id]
    if room["state"] == "active":
        return {"ok": True, "message": "Game already active"}
    room["state"] = "active"
    room["day"] = 0
    room["phase"] = "night"
    for p in room["players"]:
        if p.get("ws_id"):
            await send_to_player(room_id, p["name"], {"type": "private_role", "slot": p["slot"], "role": p["role"], "faction": p["faction"]})
    await send_faction_mates(room_id)
    await broadcast(room_id, {"type": "system", "text": "Game started. Night 1 begins."})
    if room.get("controller_task") is None or room.get("controller_task").done():
        room["controller_task"] = asyncio.create_task(phase_controller(room_id))
    return {"ok": True, "room": room_summary(room)}

async def broadcast_phase(room_id: str, phase_name: str, seconds: int):
    room = rooms.get(room_id)
    payload = {"type": "phase", "phase": phase_name, "seconds": seconds}
    if phase_name == "day_vote":
        payload["players"] = [{"slot": p["slot"], "name": p["name"], "alive": p["alive"]} for p in room["players"]]
    await broadcast(room_id, payload)
    await broadcast(room_id, {"type": "room", "room": room_summary(room)})

async def phase_controller(room_id: str):
    """
    Strict phase flow:
    night -> day_discuss -> day_vote -> day_defence -> day_final -> next night
    Night 1 starts immediately.
    """
    room = rooms.get(room_id)
    if not room:
        return
    cycle = 1
    while room["state"] == "active":
        # NIGHT
        room["phase"] = "night"
        await send_faction_mates(room_id)
        await broadcast_phase(room_id, "night", NIGHT_SECONDS)
        asyncio.create_task(simulate_bot_night_actions(room_id))
        await asyncio.sleep(NIGHT_SECONDS)
        await apply_player_actions(room_id)
        await check_victory(room_id)
        if room["state"] != "active":
            break

        # DAY discussion
        room["day"] += 1
        room["phase"] = "day_discuss"
        await broadcast_phase(room_id, "day_discuss", DAY_DISCUSS)
        asyncio.create_task(simulate_bot_day_chat(room_id))
        await asyncio.sleep(DAY_DISCUSS)

        # VOTING
        room["phase"] = "day_vote"
        room["votes"] = {}
        await broadcast_phase(room_id, "day_vote", DAY_VOTE)
        asyncio.create_task(simulate_bot_day_votes_and_accusations(room_id))
        await asyncio.sleep(DAY_VOTE)

        # determine accused
        await determine_accused(room_id)

        # DEFENCE
        room["phase"] = "day_defence"
        await broadcast_phase(room_id, "day_defence", DAY_DEFENCE)
        await asyncio.sleep(DAY_DEFENCE)

        # FINAL verdict (if accused)
        if room.get("accused"):
            room["phase"] = "day_final"
            room["verdict_votes"] = {}
            await broadcast(room_id, {"type": "verdict_phase", "accused": room["accused"], "seconds": DAY_FINAL})
            await broadcast_phase(room_id, "day_final", DAY_FINAL)
            asyncio.create_task(simulate_bot_verdict_votes(room_id))
            await asyncio.sleep(DAY_FINAL)
            await resolve_verdict(room_id)
        else:
            await broadcast(room_id, {"type": "system", "text": "No accused this day."})
            await asyncio.sleep(DAY_FINAL)
        cycle += 1

# ---- Bot behavior (sensible bots) ----
async def simulate_bot_night_actions(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active":
        return
    await asyncio.sleep(max(1, NIGHT_SECONDS // 4))
    alive = [p for p in room["players"] if p["alive"]]
    mafia = [p for p in alive if p["faction"] == "Mafia"]
    if mafia:
        candidates = [p for p in alive if p["faction"] != "Mafia"]
        if candidates:
            victim = random.choice(candidates)
            attacker = random.choice(mafia)
            room.setdefault("actions", []).append({"actor": attacker["name"], "target": victim["name"], "type": "mafia_kill", "actor_role": attacker["role"]})
            await send_to_faction(room_id, "Mafia", {"type": "system", "text": "Mafia chose a target (private)."})
    # cult convert
    cults = [p for p in alive if p["faction"] == "Cult"]
    if cults and random.random() < 0.45:
        candidates = [p for p in alive if p["faction"] not in ("Cult", "Mafia")]
        if candidates:
            t = random.choice(candidates)
            room.setdefault("actions", []).append({"actor": random.choice(cults)["name"], "target": t["name"], "type": "cult_convert"})
            await send_to_faction(room_id, "Cult", {"type": "system", "text": f"Cult attempted to convert {t['name']} (private)."})
    # doctors heal
    for d in [p for p in alive if p["role"] == "Doctor"]:
        if random.random() < 0.6:
            tgt = random.choice(alive)["name"]
            room.setdefault("actions", []).append({"actor": d["name"], "target": tgt, "type": "doctor_heal"})
            await send_to_player(room_id, d["name"], {"type": "system", "text": f"You healed {tgt} tonight."})
    # contacts (Fanatic/Spy/Beastman)
    for f in [p for p in alive if p["role"] == "Fanatic"]:
        if random.random() < 0.35:
            candidates = [p for p in alive if p["name"] != f["name"]]
            if candidates:
                t = random.choice(candidates)
                room.setdefault("actions", []).append({"actor": f["name"], "target": t["name"], "type": "contact", "actor_role": "Fanatic"})
    for s in [p for p in alive if p["role"] == "Spy" and not p.get("contacted", False)]:
        if random.random() < 0.35:
            candidates = [p for p in alive if p["name"] != s["name"]]
            if candidates:
                t = random.choice(candidates)
                room.setdefault("actions", []).append({"actor": s["name"], "target": t["name"], "type": "contact", "actor_role": "Spy"})

async def simulate_bot_day_chat(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active":
        return
    await asyncio.sleep(1)
    alive = [p for p in room["players"] if p["alive"]]
    weights = {p["name"]: 1.0 for p in alive}
    for p in alive:
        if p["faction"] in ("Mafia", "Cult"):
            weights[p["name"]] += 0.5
    for bot in [p for p in alive if p["is_bot"]]:
        if random.random() < 0.6:
            choices = [(p, weights[p["name"]]) for p in alive if p["name"] != bot["name"]]
            total = sum(w for _, w in choices) if choices else 1.0
            r = random.random() * total
            upto = 0.0
            pick = choices[-1][0] if choices else bot
            for cand, w in choices:
                upto += w
                if r <= upto:
                    pick = cand
                    break
            txt = random.choice([
                f"I don't trust {pick['name']}.",
                f"{pick['name']} seemed odd to me today.",
                f"Why was {pick['name']} quiet?"
            ])
            await broadcast(room_id, {"type": "chat", "from": bot["name"], "text": txt, "channel": "public"})
            await asyncio.sleep(1)

async def simulate_bot_day_votes_and_accusations(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active" or room["phase"] != "day_vote":
        return
    await asyncio.sleep(max(1, DAY_VOTE // 3))
    alive = [p for p in room["players"] if p["alive"]]
    for bot in [p for p in alive if p["is_bot"]]:
        if random.random() < 0.55:
            candidates = [c for c in alive if c["name"] != bot["name"]]
            if not candidates:
                continue
            weights = []
            for c in candidates:
                w = 1.0
                if c["faction"] in ("Mafia", "Cult"):
                    w = 3.0
                w *= (0.8 + random.random() * 0.8)
                weights.append((c, w))
            total = sum(w for _, w in weights)
            r = random.random() * total
            upto = 0
            pick = weights[-1][0]
            for c, w in weights:
                upto += w
                if r <= upto:
                    pick = c
                    break
            room.setdefault("votes", {})[bot["name"]] = pick["name"]
            await broadcast(room_id, {"type": "system", "text": f"🤖 {bot['name']} accused {pick['name']}"})

async def simulate_bot_verdict_votes(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active" or room["phase"] != "day_final":
        return
    accused = room.get("accused")
    if not accused:
        return
    await asyncio.sleep(max(1, DAY_FINAL // 2))
    alive = [p for p in room["players"] if p["alive"]]
    for bot in [p for p in alive if p["is_bot"]]:
        if bot["faction"] == "Mafia":
            choice = "innocent" if random.random() < 0.7 else "guilty"
        elif bot["faction"] == "Cult":
            choice = "innocent" if random.random() < 0.6 else "guilty"
        else:
            choice = "guilty" if random.random() < 0.55 else "innocent"
        room.setdefault("verdict_votes", {})[bot["name"]] = choice
        await broadcast(room_id, {"type": "system", "text": f"🤖 {bot['name']} voted {choice} on {accused}"})

# ---- Action resolution (night) ----
async def apply_player_actions(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    actions = room.get("actions", [])[:]
    protected_by_doctor = set()
    bodyguard_map = {}
    queued_kills = []
    queued_converts = []
    jailed = set()
    jail_execs = []
    contact_events = []

    for act in actions:
        a_type = act.get("type")
        actor = act.get("actor")
        target = act.get("target")
        actor_role = act.get("actor_role")
        if a_type == "doctor_heal":
            protected_by_doctor.add(target)
        elif a_type == "bodyguard_protect":
            bodyguard_map[target] = actor
        elif a_type in ("mafia_kill", "serial_kill", "vigilante_shot", "beast_kill"):
            queued_kills.append({"victim": target, "by": actor, "actor_role": actor_role})
        elif a_type == "cult_convert":
            queued_converts.append({"target": target, "by": actor})
        elif a_type == "jail":
            jailed.add(target)
        elif a_type == "jail_execute":
            jail_execs.append({"jailor": actor, "target": target})
        elif a_type == "contact":
            contact_events.append({"actor": actor, "target": target, "actor_role": actor_role})

    # Resolve contacts
    for c in contact_events:
        actor_name = c["actor"]
        target_name = c["target"]
        arole = c.get("actor_role")
        actor_p = next((p for p in room["players"] if p["name"] == actor_name), None)
        target_p = next((p for p in room["players"] if p["name"] == target_name), None)
        if not actor_p or not target_p:
            continue
        if arole == "Fanatic":
            if target_p["faction"] == "Cult":
                target_p["contacted"] = True
                actor_p["contacted"] = True
                await send_to_player(room_id, actor_name, {"type": "system", "text": f"You contacted {target_name} (Fanatic)."})
                await send_faction_mates(room_id)
        if arole == "Spy":
            if target_p["faction"] == "Mafia":
                actor_p["contacted"] = True
                await send_to_player(room_id, actor_name, {"type": "system", "text": f"You contacted {target_name} (Spy)."})
                await send_faction_mates(room_id)
            else:
                await send_to_player(room_id, actor_name, {"type": "system", "text": f"You investigated {target_name}: {target_p['role']} ({target_p['faction']})"})
        if arole == "Beastman":
            actor_p["contacted"] = True
            await send_to_player(room_id, actor_name, {"type": "system", "text": f"You contacted {target_name} (Beastman). Beast kill ability unlocked."})

    # Process converts
    for conv in queued_converts:
        tname = conv["target"]
        tp = next((p for p in room["players"] if p["name"] == tname and p["alive"]), None)
        if tp and tp["role"] not in ("Godfather", "Mafioso", "Beastman", "Soldier"):
            tp["faction"] = "Cult"
            tp["role"] = "Acolyte"
            tp["culted"] = True
            await send_to_player(room_id, tname, {"type": "system", "text": "You were converted to Cult (Acolyte)."})
            await send_faction_mates(room_id)

    # Jailor executes
    for je in jail_execs:
        jailor_name = je["jailor"]
        tgt_name = je["target"]
        jailor = next((p for p in room["players"] if p["name"] == jailor_name), None)
        tgt = next((p for p in room["players"] if p["name"] == tgt_name and p["alive"]), None)
        if jailor and tgt:
            tgt["alive"] = False
            if tgt["name"] not in room.get("cleaned_bodies", set()):
                tgt["revealed"] = True
                await broadcast(room_id, {"type": "system", "text": f"⚖️ {tgt['name']} was executed by Jailor — {tgt['role']} ({tgt['faction']})"})
            else:
                await broadcast(room_id, {"type": "system", "text": "⚠️ Body cleaned after execution."})
            if tgt["faction"] != "Mafia":
                jailor["jailor_has_execute"] = False
                jailor["role"] = "Citizen"
                jailor["faction"] = "Town"
                await send_to_player(room_id, jailor_name, {"type": "system", "text": "You executed a non-mafia and lost your execute ability; you are now a Citizen."})

    # Process kills
    for k in queued_kills:
        victim_name = k["victim"]
        actor_role = k.get("actor_role", "")
        victim = next((p for p in room["players"] if p["name"] == victim_name and p["alive"]), None)
        if not victim:
            continue
        if victim_name in jailed:
            await send_to_player(room_id, victim_name, {"type": "system", "text": "You were jailed and protected."})
            continue
        bypass = (actor_role == "Beastman")
        if victim_name in protected_by_doctor and not bypass:
            await send_to_player(room_id, victim_name, {"type": "system", "text": "You were attacked but a Doctor healed you."})
            continue
        if victim_name in bodyguard_map and not bypass:
            bg_name = bodyguard_map[victim_name]
            bgp = next((p for p in room["players"] if p["name"] == bg_name and p["alive"]), None)
            if bgp:
                bgp["alive"] = False
                bgp["revealed"] = True
                await broadcast(room_id, {"type": "system", "text": f"🛡️ {bgp['name']} died protecting {victim_name}."})
                continue
        if victim.get("role") == "Soldier" and not victim.get("soldier_used", False):
            if actor_role == "Beastman":
                victim["alive"] = False
                if victim["name"] not in room.get("cleaned_bodies", set()):
                    victim["revealed"] = True
                    await broadcast(room_id, {"type": "system", "text": f"💀 {victim['name']} was killed by Beastman."})
                else:
                    await broadcast(room_id, {"type": "system", "text": "A Soldier died but role hidden (body cleaned)."})
            else:
                victim["soldier_used"] = True
                await send_to_player(room_id, victim["name"], {"type": "system", "text": "Your Soldier protection activated and you survived."})
            continue
        # Vigilante restriction: only kills if target is Mafia (including culted mafia)
        if actor_role == "Vigilante":
            attacker_is = next((p for p in room["players"] if p["name"] == k["by"]), None)
            # but actor_role provided, check victim faction (note: k may have actor_role set)
            if victim["faction"] != "Mafia":
                await send_to_player(room_id, k["by"], {"type": "system", "text": "Your Vigilante shot failed (target not Mafia)."})
                continue
        victim["alive"] = False
        if victim["name"] not in room.get("cleaned_bodies", set()):
            victim["revealed"] = True
            await broadcast(room_id, {"type": "system", "text": f"💀 {victim['name']} was killed — {victim['role']} ({victim['faction']})"})
        else:
            await broadcast(room_id, {"type": "system", "text": "💀 A player has died but their role is hidden."})

    # reset actions
    room["actions"] = []
    await broadcast(room_id, {"type": "room", "room": room_summary(room)})
    await send_faction_mates(room_id)
    await check_victory(room_id)

# ---- Accusation & Verdict flow ----
async def determine_accused(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    votes = room.get("votes", {}) or {}
    if not votes:
        room["accused"] = None
        await broadcast(room_id, {"type": "system", "text": "No accusations were made."})
        await broadcast(room_id, {"type": "accused_update", "accused": None})
        return
    tally: Dict[str, int] = {}
    for v in votes.values():
        tally[v] = tally.get(v, 0) + 1
    sorted_counts = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    if len(sorted_counts) > 1 and sorted_counts[0][1] == sorted_counts[1][1]:
        room["accused"] = None
        await broadcast(room_id, {"type": "system", "text": "Tie in accusations — no accused."})
        await broadcast(room_id, {"type": "accused_update", "accused": None})
        return
    top = sorted_counts[0][0]
    if top == "SKIP":
        room["accused"] = None
        await broadcast(room_id, {"type": "system", "text": "Voting resulted in Skip — no accused."})
        await broadcast(room_id, {"type": "accused_update", "accused": None})
        return
    room["accused"] = top
    room.setdefault("accusation_history", []).append((room["day"], top))
    await broadcast(room_id, {"type": "system", "text": f"{top} has been accused and will defend themselves."})
    await broadcast(room_id, {"type": "accused_update", "accused": top})

async def resolve_verdict(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    accused = room.get("accused")
    if not accused:
        await broadcast(room_id, {"type": "system", "text": "No accused to judge."})
        return
    votes = room.get("verdict_votes", {}) or {}
    if not votes:
        await broadcast(room_id, {"type": "system", "text": "No verdict votes — no lynch."})
        room["accused"] = None
        await broadcast(room_id, {"type": "accused_update", "accused": None})
        return
    tally = {"guilty": 0, "innocent": 0}
    for v in votes.values():
        tally[v] = tally.get(v, 0) + 1
    if tally["guilty"] > tally["innocent"]:
        victim = next((p for p in room["players"] if p["name"] == accused and p["alive"]), None)
        if victim:
            victim["alive"] = False
            if accused not in room.get("cleaned_bodies", set()):
                victim["revealed"] = True
                await broadcast(room_id, {"type": "system", "text": f"⚖️ {accused} was found GUILTY — {victim['role']} ({victim['faction']})"})
            else:
                await broadcast(room_id, {"type": "system", "text": f"⚠️ {accused} was found guilty but corpse cleaned; role hidden."})
            room["accused"] = None
            room["verdict_votes"] = {}
            await broadcast(room_id, {"type": "room", "room": room_summary(room)})
            await check_victory(room_id)
            return
    else:
        await broadcast(room_id, {"type": "system", "text": f"{accused} was found INNOCENT."})
    room["accused"] = None
    room["verdict_votes"] = {}
    await broadcast(room_id, {"type": "room", "room": room_summary(room)})

# ---- Victory checks ----
async def check_victory(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    alive = [p for p in room["players"] if p["alive"]]
    mafia = [p for p in alive if p["faction"] == "Mafia"]
    cult = [p for p in alive if p["faction"] == "Cult"]
    town = [p for p in alive if p["faction"] == "Town"]
    neutral = [p for p in alive if p["faction"] == "Neutral"]

    # Town wins when all mafia dead (they don't need to eliminate cult/neutrals)
    if not mafia and town:
        await end_game(room_id, "Town")
        return
    # Mafia wins if town eliminated and mafia >= cult
    if not town and len(mafia) >= len(cult):
        await end_game(room_id, "Mafia")
        return
    # Cult wins if cult >= (mafia + town + neutral)
    if len(cult) >= (len(mafia) + len(town) + len(neutral)):
        await end_game(room_id, "Cult")
        return
    # Neutral(s) win if only neutrals remain
    if neutral and not mafia and not town and not cult:
        await end_game(room_id, "Neutral")
        return

async def end_game(room_id: str, winner: str):
    room = rooms.get(room_id)
    if not room:
        return
    room["state"] = "ended"
    await broadcast(room_id, {"type": "system", "text": f"🏆 {winner} win!"})
    recap_lines = []
    for p in room["players"]:
        recap_lines.append(f"{p['name']}: {p['role']} ({p['faction']}) {'Alive' if p['alive'] else 'Dead'}")
    recap = "\n".join(recap_lines)
    await broadcast(room_id, {"type": "system", "text": "📜 Final Roles:\n" + recap})
    await broadcast(room_id, {"type": "room", "room": room_summary(room)})

# ---- Startup: create sample room for quick testing ----
@app.on_event("startup")
async def startup_event():
    if not rooms:
        r = create_room_obj("Host")
        print("Sample room created:", r["id"])
