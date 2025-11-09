# main.py - Town of Shadows (stable phase loop, faction visibility + voting payloads)
# Deploy with: uvicorn main:app --host 0.0.0.0 --port 10000

import asyncio
import json
import random
import time
from typing import Dict, Any, List, Set, Optional
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Town of Shadows - Stable Backend")

# Allow all origins for testing; restrict in production
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# TIMERS (use your specified values)
NIGHT_SECONDS = 40
DAY_DISCUSS = 60
DAY_VOTE = 20
DAY_DEFENCE = 10
DAY_FINAL = 10

TOTAL_PLAYERS = 20

# Role pools (no villager)
TOWN_POOL = ["Doctor","Detective","Bodyguard","Vigilante","Jailor","Soldier","Cupid","Gossip","Lookout","Mayor","Investigator","Escort","Medium"]
MAFIA_POOL = ["Godfather","Mafioso","Janitor","Spy","Beastman","Consigliere","Blackmailer","Framer"]
CULT_POOL = ["Cult Leader","Fanatic","Infiltrator","Prophet","Acolyte"]
NEUTRAL_POOL = ["Jester","Executioner","Serial Killer","Arsonist","Survivor","Amnesiac","Witch","Guardian Angel"]

def role_to_faction(role: str) -> str:
    if role in TOWN_POOL: return "Town"
    if role in MAFIA_POOL: return "Mafia"
    if role in CULT_POOL: return "Cult"
    if role in NEUTRAL_POOL: return "Neutral"
    return "Unknown"

# In-memory state
rooms: Dict[str, Dict[str, Any]] = {}
ws_managers: Dict[str, Dict[str, WebSocket]] = {}  # rooms -> wsid -> websocket

# Pydantic models for REST endpoints
class CreateRoomReq(BaseModel):
    host_name: Optional[str] = "Host"

class JoinReq(BaseModel):
    roomId: str
    name: Optional[str] = "Player"

class QueueActionReq(BaseModel):
    room_id: str
    actor: str
    target: str
    type: str

# Utility: create role list for a full game
def sample_roles_for_game() -> List[str]:
    roles: List[str] = []
    # Town: exactly 8 (as requested previously)
    town_count = min(8, len(TOWN_POOL))
    roles.extend(random.sample(TOWN_POOL, town_count))
    # Mafia: ensure at least Godfather and one Mafioso, and total mafia count around 5
    mafia = ["Godfather", "Mafioso"]
    remaining_mafia_slots = 5 - len(mafia)
    remaining_mafia_roles = [r for r in MAFIA_POOL if r not in mafia]
    for _ in range(remaining_mafia_slots):
        if random.random() < 0.5:
            mafia.append("Mafioso")
        else:
            mafia.append(random.choice(remaining_mafia_roles))
    roles.extend(mafia)
    # Cult: ensure leader+fanatic + 2 others (total 4)
    cult = ["Cult Leader", "Fanatic"]
    other_cult = [r for r in CULT_POOL if r not in cult]
    while len(cult) < 4:
        cult.append(random.choice(other_cult))
    roles.extend(cult)
    # Neutrals: exactly 3
    roles.extend(random.sample(NEUTRAL_POOL, 3))
    # Fill remaining slots with random town roles
    while len(roles) < TOTAL_PLAYERS:
        roles.append(random.choice(TOWN_POOL))
    random.shuffle(roles)
    return roles

def create_room_obj(host_name: str = "Host", total_slots: int = TOTAL_PLAYERS) -> Dict[str, Any]:
    rid = str(uuid4())[:6].upper()
    roles = sample_roles_for_game()
    players = []
    for i in range(1, total_slots+1):
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
            "vigilante_last_shot_day": -99,
            "jailor_has_execute": True if r == "Jailor" else False,
            "soldier_used": False,
            "doused": False,
            "contacted": False,   # Fanatic/Spy contact state
        })
    room = {
        "id": rid,
        "host": host_name,
        "players": players,
        "state": "waiting",
        "phase": "waiting",
        "day": 0,
        "actions": [],
        "votes": {},
        "lovers": {},
        "controller_task": None,
        "cleaned_bodies": set(),
        "accused": None,
        "verdict_votes": {},
        "accusation_history": [],
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
            {"slot": p["slot"], "name": p["name"], "alive": p["alive"], "revealed": p["revealed"], "is_bot": p["is_bot"]}
            for p in room["players"]
        ],
        "accused": room.get("accused")
    }

# REST endpoints
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
async def queue_action(req: QueueActionReq):
    rid = req.room_id
    if rid not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    room = rooms[rid]
    if room["phase"].lower().startswith("day"):
        raise HTTPException(status_code=400, detail="Actions only allowed at night")
    room.setdefault("actions", []).append({"actor": req.actor, "target": req.target, "type": req.type, "ts": time.time()})
    return {"ok": True}

# --- WebSocket helpers ---
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
    if not room: return
    p = next((x for x in room["players"] if x["name"] == player_name), None)
    if not p: return
    wsid = p.get("ws_id")
    if not wsid: return
    await send_to_ws(room_id, wsid, message)

async def send_to_faction(room_id: str, faction: str, message: dict):
    room = rooms.get(room_id)
    if not room: return
    for p in room["players"]:
        if p.get("faction") == faction and p.get("ws_id"):
            await send_to_player(room_id, p["name"], message)

# Build faction mates list for viewer (hides Fanatic/Spy until contacted)
def build_faction_list_for_player(room: Dict[str, Any], viewer: Dict[str, Any]):
    faction = viewer.get("faction")
    items = []
    for p in room["players"]:
        if p["faction"] != faction:
            continue
        if p["role"] == "Fanatic" and not p.get("contacted", False):
            continue
        if p["role"] == "Spy" and not p.get("contacted", False):
            continue
        items.append({"slot": p["slot"], "role": p["role"].lower()})
    return items

async def send_faction_mates(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    for p in room["players"]:
        if not p.get("ws_id"):
            continue
        if p.get("faction") in ("Mafia", "Cult"):
            mates = build_faction_list_for_player(room, p)
            await send_to_player(room_id, p["name"], {"type": "faction_mates", "mates": mates})

# WebSocket endpoint
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    if room_id not in rooms:
        await websocket.send_text(json.dumps({"type":"system","text":"Room not found"}))
        await websocket.close()
        return
    wsid = str(uuid4())
    ws_managers[room_id][wsid] = websocket
    try:
        await websocket.send_text(json.dumps({"type":"system","text":f"Connected to {room_id}", "ws_id": wsid}))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await websocket.send_text(json.dumps({"type":"system","text":"Invalid JSON"}))
                continue
            await handle_ws_message(room_id, wsid, msg)
    except WebSocketDisconnect:
        ws_managers[room_id].pop(wsid, None)
    except Exception:
        ws_managers[room_id].pop(wsid, None)

# WebSocket message handling
async def handle_ws_message(room_id: str, wsid: str, msg: dict):
    mtype = msg.get("type")
    room = rooms.get(room_id)
    if not room:
        return

    # IDENTIFY: player binds websocket to a slot
    if mtype == "identify":
        slot = msg.get("slot")
        p = next((x for x in room["players"] if x["slot"] == slot), None)
        if p:
            p["ws_id"] = wsid
            p["is_bot"] = False
            # send private role info
            await send_to_player(room_id, p["name"], {"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"]})
            # broadcast room summary to everyone so frontend refreshes grid
            await broadcast(room_id, {"type":"room","room": room_summary(room)})
            # Send faction mates immediately in case player is mafia/cult
            await send_faction_mates(room_id)
        else:
            await send_to_ws(room_id, wsid, {"type":"system","text":"Slot not found"})
        return

    # CHAT: route to proper channel
    if mtype == "chat":
        ch = msg.get("channel","public")
        text = msg.get("text","")
        sender = msg.get("from","Anon")
        # voting shorthand: if in voting phase and message is a digit -> treat as vote
        if room["phase"].lower().find("vote") != -1 and text.strip().isdigit():
            voter = sender
            target_slot = int(text.strip())
            target_p = next((x for x in room["players"] if x["slot"] == target_slot), None)
            if target_p:
                room.setdefault("votes", {})[voter] = target_p["name"]
                await send_to_ws(room_id, wsid, {"type":"system","text":f"You voted for Player {target_slot}"})
                await broadcast(room_id, {"type":"system","text": f"{voter} voted (anonymous)."})
                return
        if ch == "mafia":
            await send_to_faction(room_id, "Mafia", {"type":"chat","from":sender,"text":text,"channel":"mafia"})
            return
        if ch == "cult":
            await send_to_faction(room_id, "Cult", {"type":"chat","from":sender,"text":text,"channel":"cult"})
            return
        if ch == "dead":
            for p in room["players"]:
                if not p["alive"] and p.get("ws_id"):
                    await send_to_player(room_id, p["name"], {"type":"chat","from":sender,"text":text,"channel":"dead"})
            return
        # public
        await broadcast(room_id, {"type":"chat","from":sender,"text":text,"channel":"public"})
        return

    # START GAME
    if mtype == "start_game":
        try:
            await start_game(room_id)
        except Exception as e:
            await send_to_ws(room_id, wsid, {"type":"system","text":str(e)})
        return

    # PLAYER ACTION (night)
    if mtype == "player_action":
        action = msg.get("action")
        if action:
            if room["phase"].lower().startswith("day"):
                await send_to_ws(room_id, wsid, {"type":"system","text":"Actions only allowed at night"})
                return
            room.setdefault("actions", []).append({"actor": action.get("actor"), "target": action.get("target"), "type": action.get("type"), "ts": time.time(), "actor_role": action.get("actor_role")})
            await send_to_ws(room_id, wsid, {"type":"system","text":"Action queued (private confirmation)."})
        return

    # ACCUSE / VOTE / VERDICT
    if mtype == "accuse":
        if not room["phase"].lower().startswith("day"):
            await send_to_ws(room_id, wsid, {"type":"system","text":"Accusations only during day voting period"}); return
        acc_from = msg.get("from"); acc_target = msg.get("target")
        room.setdefault("votes", {})[acc_from] = acc_target
        await broadcast(room_id, {"type":"system","text": f"{acc_from} accused {acc_target}"})
        return

    if mtype == "verdict_vote":
        if not room.get("accused"):
            await send_to_ws(room_id, wsid, {"type":"system","text":"No accused currently"}); return
        voter = msg.get("from"); choice = msg.get("choice")
        if choice not in ("guilty","innocent"):
            await send_to_ws(room_id, wsid, {"type":"system","text":"Invalid verdict choice"}); return
        room.setdefault("verdict_votes", {})[voter] = choice
        await broadcast(room_id, {"type":"system","text": f"{voter} voted {choice} on {room['accused']}"})
        return

    if mtype == "vote":
        if not room["phase"].lower().startswith("day"):
            await send_to_ws(room_id, wsid, {"type":"system","text":"Voting only during day"}); return
        voter = msg.get("from"); target = msg.get("target")
        # numeric shorthand
        if isinstance(target, str) and target.isdigit():
            target_slot = int(target); tgt = next((x for x in room["players"] if x["slot"]==target_slot), None)
            if tgt:
                room.setdefault("votes", {})[voter] = tgt["name"]
                await send_to_ws(room_id, wsid, {"type":"system","text":f"You voted for Player {target_slot}"})
                await broadcast(room_id, {"type":"system","text": f"{voter} voted (anonymous)."})
                return
        room.setdefault("votes", {})[voter] = target
        await broadcast(room_id, {"type":"system","text": f"{voter} voted for {target}"})
        return

    # Unknown
    await send_to_ws(room_id, wsid, {"type":"system","text":"Unknown message type"})

# START GAME route and controller
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
    # send private roles to connected players
    for p in room["players"]:
        if p.get("ws_id"):
            await send_to_player(room_id, p["name"], {"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"]})
    # send faction mates now
    await send_faction_mates(room_id)
    # broadcast start
    await broadcast(room_id, {"type":"system","text":"Game started. Night 1 begins."})
    # start controller
    if room.get("controller_task") is None or room.get("controller_task").done():
        room["controller_task"] = asyncio.create_task(phase_controller(room_id))
    return {"ok": True, "room": room_summary(room)}

async def broadcast_phase(room_id: str, phase_name: str, seconds: int):
    room = rooms.get(room_id)
    payload = {"type":"phase","phase":phase_name,"seconds":seconds}
    # include players when it's voting so frontend can fill dropdown
    if "vote" in phase_name.lower() or "voting" in phase_name.lower():
        payload["players"] = [{"slot": p["slot"], "name": p["name"], "alive": p["alive"]} for p in room["players"]]
    await broadcast(room_id, payload)
    # broadcast current room summary too
    await broadcast(room_id, {"type":"room","room": room_summary(room)})

async def phase_controller(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    cycle = 1
    while room["state"] == "active":
        # NIGHT phase
        room["phase"] = "night"
        # send faction mates at start of night (fanatic/spy contact visibility considered)
        await send_faction_mates(room_id)
        await broadcast_phase(room_id, f"Night {cycle}", NIGHT_SECONDS)
        # simulate bots (non-blocking)
        asyncio.create_task(simulate_bot_night_actions(room_id))
        # wait night duration
        await asyncio.sleep(NIGHT_SECONDS)
        # resolve actions (simple resolution implemented to keep cycle moving)
        await apply_player_actions(room_id)
        # check victory
        await check_victory(room_id)
        if room["state"] != "active":
            break

        # DAY: discussion
        room["day"] += 1
        room["phase"] = "day"
        await broadcast_phase(room_id, f"Day {room['day']} (Discussion)", DAY_DISCUSS)
        await asyncio.sleep(DAY_DISCUSS)

        # VOTING
        room["votes"] = {}
        await broadcast_phase(room_id, f"Day {room['day']} (Voting)", DAY_VOTE)
        # start bot accusations/votes
        asyncio.create_task(simulate_bot_day_votes_and_accusations(room_id))
        await asyncio.sleep(DAY_VOTE)

        # determine accused (simple plurality)
        await determine_accused(room_id)

        await broadcast_phase(room_id, f"Day {room['day']} (Defence)", DAY_DEFENCE)
        await asyncio.sleep(DAY_DEFENCE)

        if room.get("accused"):
            room["verdict_votes"] = {}
            await broadcast(room_id, {"type":"verdict_phase","accused": room["accused"], "seconds": DAY_FINAL})
            await broadcast_phase(room_id, f"Day {room['day']} (Final Verdict)", DAY_FINAL)
            asyncio.create_task(simulate_bot_verdict_votes(room_id))
            await asyncio.sleep(DAY_FINAL)
            await resolve_verdict(room_id)
        else:
            await broadcast(room_id, {"type":"system","text":"No accused this day."})
            await asyncio.sleep(DAY_FINAL)

        cycle += 1

# --- Bot simulation (keeps game lively); simplified but functional ---
async def simulate_bot_night_actions(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    await asyncio.sleep(max(1, NIGHT_SECONDS//3))
    alive = [p for p in room["players"] if p["alive"]]
    # simple mafia kill
    mafia = [p for p in alive if p["faction"]=="Mafia"]
    if mafia:
        candidates = [p for p in alive if p["faction"]!="Mafia"]
        if candidates:
            victim = random.choice(candidates)
            attacker = random.choice(mafia)
            room.setdefault("actions", []).append({"actor": attacker["name"], "target": victim["name"], "type":"mafia_kill", "actor_role": attacker["role"]})
            # notify mafia privately
            await send_to_faction(room_id, "Mafia", {"type":"system","text": f"Mafia targeted {victim['name']} (private)."})
    # simple cult convert attempt
    cults = [p for p in alive if p["faction"]=="Cult"]
    if cults and random.random() < 0.4:
        candidates = [p for p in alive if p["faction"] not in ("Cult","Mafia")]
        if candidates:
            t = random.choice(candidates)
            room.setdefault("actions", []).append({"actor": random.choice(cults)["name"], "target": t["name"], "type":"cult_convert"})
            await send_to_faction(room_i
