# main.py — Town of Shadows full backend (Step 3 complete)
# Deploy with: uvicorn main:app --host 0.0.0.0 --port 10000

import asyncio
import json
import random
import time
from typing import Dict, Any, List, Set, Optional
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Town of Shadows - Full Backend (Step 3)")

# ---------------- CONFIG ----------------
# Update if your frontend URL changes
FRONTEND_URL = "https://effortless-cobbler-2ab85b.netlify.app"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TOTAL_PLAYERS = 20
NIGHT_SECONDS = 40
DAY_DISCUSS = 60
DAY_VOTE = 20
DAY_DEFENCE = 10
DAY_FINAL = 10
DAY_SECONDS = DAY_DISCUSS + DAY_VOTE + DAY_DEFENCE + DAY_FINAL

# ---------------- MODELS ----------------
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

# ---------------- IN-MEMORY STATE ----------------
rooms: Dict[str, Dict[str, Any]] = {}
ws_managers: Dict[str, Dict[str, WebSocket]] = {}  # room_id -> {wsid: websocket}

# ---------------- ROLE POOLS ----------------
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

# ---------------- HELPERS ----------------
def sample_roles_for_game() -> List[str]:
    """Return roles matching: Town 8 / Mafia 5 / Cult 4 / Neutrals 3 (total 20)."""
    roles: List[str] = []
    roles.extend(random.sample(TOWN_POOL, min(8, len(TOWN_POOL))))
    mafia = ["Godfather","Mafioso"]
    remaining = [r for r in MAFIA_POOL if r not in mafia]
    while len(mafia) < 5:
        mafia.append(random.choice(remaining) if random.random() < 0.5 else "Mafioso")
    roles.extend(mafia)
    cult = ["Cult Leader","Fanatic"]
    other_cult = [r for r in CULT_POOL if r not in cult]
    while len(cult) < 4:
        cult.append(random.choice(other_cult))
    roles.extend(cult)
    roles.extend(random.sample(NEUTRAL_POOL, 3))
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
            "contacted": False,
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
            {"slot": p["slot"], "name": p["name"], "alive": p["alive"], "revealed": p["revealed"], "is_bot": p["is_bot"], "faction": p.get("faction","?")}
            for p in room["players"]
        ],
        "accused": room.get("accused")
    }

# ---------------- REST ROUTES ----------------
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
    await broadcast(room["id"], {"type":"system", "text": f"{req.actor} queued {req.type} -> {req.target}"})
    return {"ok": True}

# ---------------- WEBSOCKET ----------------
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
            mtype = msg.get("type")
            # identify
            if mtype == "identify":
                slot = msg.get("slot")
                p = next((x for x in rooms[room_id]["players"] if x["slot"] == slot), None)
                if p:
                    p["ws_id"] = wsid
                    p["is_bot"] = False
                    try:
                        await websocket.send_text(json.dumps({"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"],"explain":f"Faction: {p['faction']}"}))
                    except:
                        pass
                    await broadcast(room_id, {"type":"room","room": room_summary(rooms[room_id])})
                else:
                    await websocket.send_text(json.dumps({"type":"system","text":"Slot not found"}))
            elif mtype == "player_action":
                action = msg.get("action")
                if action:
                    room = rooms[room_id]
                    if room["phase"].lower().startswith("day"):
                        await websocket.send_text(json.dumps({"type":"system","text":"Actions only allowed at night"}))
                        continue
                    room.setdefault("actions", []).append({"actor": action.get("actor"), "target": action.get("target"), "type": action.get("type"), "ts": time.time(), "actor_role": action.get("actor_role")})
                    await websocket.send_text(json.dumps({"type":"system","text":"Action queued"}))
            elif mtype == "chat":
                ch = msg.get("channel","public")
                text = msg.get("text","")
                sender = msg.get("from","Anon")
                await broadcast(room_id, {"type":"chat","from":sender,"text":text,"channel":ch})
            elif mtype == "start_game":
                try:
                    await start_game_ws(room_id)
                except Exception as e:
                    await websocket.send_text(json.dumps({"type":"system","text":str(e)}))
            elif mtype == "accuse":
                room = rooms[room_id]
                if not room["phase"].lower().startswith("day"):
                    await websocket.send_text(json.dumps({"type":"system","text":"Accusations only during day voting period"})); continue
                acc_from = msg.get("from"); acc_target = msg.get("target")
                room.setdefault("votes", {})[acc_from] = acc_target
                await broadcast(room_id, {"type":"system","text": f"{acc_from} accused {acc_target}"})
            elif mtype == "verdict_vote":
                room = rooms[room_id]
                if not room.get("accused"):
                    await websocket.send_text(json.dumps({"type":"system","text":"No accused currently"})); continue
                voter = msg.get("from"); choice = msg.get("choice")
                if choice not in ("guilty","innocent"):
                    await websocket.send_text(json.dumps({"type":"system","text":"Invalid verdict choice"})); continue
                room.setdefault("verdict_votes", {})[voter] = choice
                await broadcast(room_id, {"type":"system","text": f"{voter} voted {choice} on {room['accused']}"})
            elif mtype == "vote":
                room = rooms[room_id]
                if not room["phase"].lower().startswith("day"):
                    await websocket.send_text(json.dumps({"type":"system","text":"Voting only during day"})); continue
                voter = msg.get("from"); target = msg.get("target")
                room.setdefault("votes", {})[voter] = target
                await broadcast(room_id, {"type":"system","text": f"{voter} voted for {target}"})
            else:
                await websocket.send_text(json.dumps({"type":"system","text":"Unknown ws message type"}))
    except WebSocketDisconnect:
        ws_managers[room_id].pop(wsid, None)
    except Exception:
        ws_managers[room_id].pop(wsid, None)

# ---------------- BROADCAST HELPERS ----------------
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

async def broadcast_phase(room_id: str, phase_name: str, seconds: int):
    await broadcast(room_id, {"type":"phase","phase":phase_name,"seconds":seconds})
    await broadcast(room_id, {"type":"room","room": room_summary(rooms[room_id])})

# ---------------- GAME START (REST) ----------------
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
    # send private roles to connected humans
    mgr = ws_managers.get(room_id, {})
    for wsid, ws in mgr.items():
        p = next((x for x in room["players"] if x.get("ws_id") == wsid), None)
        if p:
            try:
                await ws.send_text(json.dumps({"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"],"explain":f"Faction: {p['faction']}"}))
            except:
                pass
    await broadcast(room_id, {"type":"system","text":"Game started. Night 1 begins."})
    await broadcast_phase(room_id, "Night 1", NIGHT_SECONDS)
    if room.get("controller_task") is None:
        room["controller_task"] = asyncio.create_task(phase_controller(room_id))
    return {"ok": True, "room": room_summary(room)}

# ---------------- GAME START (WS wrapper) ----------------
async def start_game_ws(room_id: str):
    # wrapper to call start_game logic from WS
    return await start_game(room_id)

# ---------------- PHASE CONTROLLER ----------------
async def phase_controller(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    cycle = 1
    while room["state"] == "active":
        # NIGHT
        room["phase"] = "night"
        await broadcast_phase(room_id, f"Night {cycle}", NIGHT_SECONDS)
        asyncio.create_task(simulate_bot_night_actions(room_id))
        await asyncio.sleep(NIGHT_SECONDS)
        await apply_player_actions(room_id)
        await check_victory(room_id)
        if room["state"] != "active":
            break
        # DAY
        room["day"] += 1
        room["phase"] = "day"
        # discussion
        await broadcast_phase(room_id, f"Day {room['day']} (Discussion)", DAY_DISCUSS)
        await asyncio.sleep(DAY_DISCUSS)
        # voting
        room["votes"] = {}
        await broadcast_phase(room_id, f"Day {room['day']} (Voting)", DAY_VOTE)
        asyncio.create_task(simulate_bot_day_votes_and_accusations(room_id))
        await asyncio.sleep(DAY_VOTE)
        await determine_accused(room_id)
        # defence
        await broadcast_phase(room_id, f"Day {room['day']} (Defence)", DAY_DEFENCE)
        await asyncio.sleep(DAY_DEFENCE)
        # final verdict
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

# ---------------- BOT ACTIONS (night) ----------------
async def simulate_bot_night_actions(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    await asyncio.sleep(max(1, NIGHT_SECONDS//3))
    alive = [p for p in room["players"] if p["alive"]]
    # Mafia kill
    mafia = [p for p in alive if p["faction"]=="Mafia"]
    if mafia:
        candidates = [p for p in alive if p["faction"]!="Mafia"]
        if candidates:
            victim = random.choice(candidates)
            attacker = random.choice(mafia)
            room.setdefault("actions", []).append({"actor": attacker["name"], "target": victim["name"], "type":"mafia_kill", "actor_role": attacker["role"]})
            await broadcast(room_id, {"type":"system","text": f"🤖 Mafia targeted {victim['name']}"})
    # Cult convert attempt sometimes
    cults = [p for p in alive if p["faction"]=="Cult"]
    if cults and random.random() < 0.45:
        candidates = [p for p in alive if p["faction"] not in ("Cult","Mafia")]
        if candidates:
            t = random.choice(candidates)
            room.setdefault("actions", []).append({"actor": random.choice(cults)["name"], "target": t["name"], "type":"cult_convert"})
            await broadcast(room_id, {"type":"system","text": f"🤖 Cult attempted to convert {t['name']}"})
    # Doctor bots heal
    for d in [p for p in alive if p["role"]=="Doctor"]:
        if random.random() < 0.6:
            tgt = random.choice(alive)["name"]
            room.setdefault("actions", []).append({"actor": d["name"], "target": tgt, "type":"doctor_heal"})
    # Bodyguard bots protect
    for bg in [p for p in alive if p["role"]=="Bodyguard"]:
        tgt = random.choice(alive)["name"]
        room.setdefault("actions", []).append({"actor": bg["name"], "target": tgt, "type":"bodyguard_protect"})
    # Vigilante bots (shoot mafia only)
    for v in [p for p in alive if p["role"]=="Vigilante"]:
        if room["day"] - v.get("vigilante_last_shot_day", -99) >= 2:
            known_maf = [p for p in alive if p["faction"]=="Mafia"]
            if known_maf and random.random() < 0.45:
                tgt = random.choice(known_maf)["name"]
                room.setdefault("actions", []).append({"actor": v["name"], "target": tgt, "type":"vigilante_shot"})
                v["vigilante_last_shot_day"] = room["day"]
    # Jailor bots
    for j in [p for p in alive if p["role"]=="Jailor"]:
        tgt = random.choice([p for p in alive if p["name"]!=j["name"]])["name"]
        if j.get("jailor_has_execute", True) and random.random() < 0.12:
            room.setdefault("actions", []).append({"actor": j["name"], "target": tgt, "type":"jail_execute"})
        else:
            room.setdefault("actions", []).append({"actor": j["name"], "target": tgt, "type":"jail"})
    # Janitor bots clean
    for jan in [p for p in alive if p["role"]=="Janitor"]:
        if random.random() < 0.3:
            dead = [d for d in room["players"] if not d["alive"] and d["name"] not in room.get("cleaned_bodies", set())]
            if dead:
                room.setdefault("actions", []).append({"actor": jan["name"], "target": dead[0]["name"], "type":"janitor_clean"})
    # Serial killer bots
    for sk in [p for p in alive if p["role"]=="Serial Killer"]:
        tgt = random.choice([p for p in alive if p["name"]!=sk["name"]])["name"]
        room.setdefault("actions", []).append({"actor": sk["name"], "target": tgt, "type":"serial_kill", "actor_role":"Serial Killer"})

# ---------------- NIGHT RESOLUTION ----------------
async def apply_player_actions(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    actions = room.get("actions", [])[:]
    protected_by_doctor: Set[str] = set()
    protected_by_bodyguard: Dict[str,str] = {}
    janitor_cleans: Set[str] = set()
    queued_kills = []
    queued_converts = []
    jailed_targets: Set[str] = set()
    jailor_execs = []

    # collect actions
    for act in actions:
        a_type = act.get("type")
        actor = act.get("actor")
        target = act.get("target")
        actor_role = act.get("actor_role")
        if not actor_role:
            actor_p = next((p for p in room["players"] if p["name"]==actor), None)
            actor_role = actor_p["role"] if actor_p else actor_role
        if a_type == "doctor_heal":
            protected_by_doctor.add(target)
            await broadcast(room_id, {"type":"system","text": f"🩺 {actor} healed {target}."})
        elif a_type == "bodyguard_protect":
            protected_by_bodyguard[target] = actor
            await broadcast(room_id, {"type":"system","text": f"🛡️ {actor} is guarding {target}."})
        elif a_type == "janitor_clean":
            janitor_cleans.add(target)
            await broadcast(room_id, {"type":"system","text": f"🧹 {actor} will clean {target}'s body."})
        elif a_type in ("mafia_kill","beast_kill","serial_kill","vigilante_shot"):
            queued_kills.append({"victim": target, "by": actor, "type": a_type, "actor_role": actor_role})
        elif a_type == "cult_convert":
            queued_converts.append({"target": target, "by": actor})
        elif a_type == "jail":
            jailed_targets.add(target)
            await broadcast(room_id, {"type":"system","text": f"🔒 {actor} jailed {target}."})
        elif a_type == "jail_execute":
            jailor_execs.append({"jailor": actor, "target": target})
        elif a_type == "douse":
            t = next((p for p in room["players"] if p["name"]==target), None)
            if t:
                t["doused"] = True
                await broadcast(room_id, {"type":"system","text": f"🔥 {actor} doused {target}."})
        else:
            pass

    # converts first
    for conv in queued_converts:
        tname = conv["target"]
        target_p = next((p for p in room["players"] if p["name"]==tname and p["alive"]), None)
        if target_p:
            if target_p["role"] not in ("Godfather","Mafioso","Beastman","Soldier"):
                target_p["faction"] = "Cult"
                target_p["role"] = "Acolyte"
                await broadcast(room_id, {"type":"system","text": f"✨ {tname} was converted to the Cult!"})

    # jailor executes
    for je in jailor_execs:
        jailor = je["jailor"]; target = je["target"]
        jailor_p = next((p for p in room["players"] if p["name"]==jailor), None)
        tgt_p = next((p for p in room["players"] if p["name"]==target), None)
        if tgt_p and tgt_p["alive"]:
            tgt_p["alive"] = False
            if tgt_p["name"] not in room.get("cleaned_bodies", set()):
                tgt_p["revealed"] = True
                await broadcast(room_id, {"type":"system","text": f"⚖️ {tgt_p['name']} was executed by Jailor {jailor} — {tgt_p['role']} ({tgt_p['faction']})"})
            else:
                await broadcast(room_id, {"type":"system","text": f"⚠️ {tgt_p['name']}'s body was cleaned after execution."})
            if jailor_p and tgt_p and tgt_p.get("faction") == "Town":
                jailor_p["jailor_has_execute"] = False
                await broadcast(room_id, {"type":"system","text": f"⚠️ {jailor} executed a Town member and lost execution ability."})

    # prepare final kills (handle cupid sacrifice)
    final_kills = []
    for k in queued_kills:
        victim = k["victim"]
        if victim in jailed_targets:
            await broadcast(room_id, {"type":"system","text": f"🔒 {victim} was jailed and could not be targeted."})
            continue
        lover = room.get("lovers", {}).get(victim)
        if lover:
            lover_p = next((p for p in room["players"] if p["name"]==lover and p["alive"]), None)
            if lover_p:
                final_kills.append({"name": lover, "killed_by": k["by"], "actor_role": k.get("actor_role"), "type": k.get("type")})
                await broadcast(room_id, {"type":"system","text": f"💔 {lover} sacrificed themselves for their lover {victim}!"})
                continue
        final_kills.append({"name": victim, "killed_by": k["by"], "actor_role": k.get("actor_role"), "type": k.get("type")})

    # apply kills with protections
    deaths = []
    for ent in final_kills:
        name = ent["name"]; actor_role = ent.get("actor_role",""); ktype = ent.get("type","")
        # bodyguard protect
        if name in protected_by_bodyguard:
            bg_name = protected_by_bodyguard[name]
            bg_p = next((p for p in room["players"] if p["name"]==bg_name and p["alive"]), None)
            if bg_p:
                if actor_role == "Beastman" or ktype in ("beast_kill","beastman_kill"):
                    bg_p["alive"] = False
                    deaths.append(bg_p["name"])
                    await broadcast(room_id, {"type":"system","text": f"🛡️ {bg_p['name']} (Bodyguard) died protecting {name} — Beastman bypassed protection."})
                else:
                    bg_p["alive"] = False
                    deaths.append(bg_p["name"])
                    await broadcast(room_id, {"type":"system","text": f"🛡️ {bg_p['name']} (Bodyguard) died protecting {name} — {name} survived."})
                    continue

        # soldier special
        target_p = next((p for p in room["players"] if p["name"]==name and p["alive"]), None)
        if target_p and target_p.get("role") == "Soldier" and not target_p.get("soldier_used", False):
            if actor_role == "Beastman" or ktype in ("beast_kill","beastman_kill"):
                target_p["alive"] = False
                deaths.append(name)
                await broadcast(room_id, {"type":"system","text": f"⚔️ {name} (Soldier) was killed by Beastman — Soldier's protect couldn't save them."})
                continue
            else:
                target_p["soldier_used"] = True
                await broadcast(room_id, {"type":"system","text": f"🛡️ {name} (Soldier) used their one-time protection and survived."})
                continue

        # doctor protection
        bypass = (actor_role == "Beastman") or (ktype in ("beast_kill","beastman_kill"))
        if (name in protected_by_doctor) and (not bypass):
            await broadcast(room_id, {"type":"system","text": f"🛡️ {name} was attacked but survived due to Doctor protection."})
            continue

        # apply kill
        if target_p and target_p["alive"]:
            target_p["alive"] = False
            deaths.append(name)
            if name not in room.get("cleaned_bodies", set()):
                target_p["revealed"] = True
                await broadcast(room_id, {"type":"system","text": f"💀 {name} was killed — {target_p['role']} ({target_p['faction']})"})
            else:
                await broadcast(room_id, {"type":"system","text": f"⚠️ A corpse was found for {name}, but it was cleaned; role hidden."})

    # janitor cleans
    for cleaned in janitor_cleans:
        room.setdefault("cleaned_bodies", set()).add(cleaned)
        await broadcast(room_id, {"type":"system","text": f"🧹 {cleaned}'s body will be cleaned (role hidden)."})

    # clear actions and broadcast state
    room["actions"] = []
    await broadcast(room_id, {"type":"room","room": room_summary(room)})
    await check_victory(room_id)

# ---------------- DAY BOT BEHAVIOR ----------------
async def simulate_bot_day_votes_and_accusations(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    await asyncio.sleep(max(1, DAY_VOTE//3))
    alive = [p for p in room["players"] if p["alive"]]
    for bot in [p for p in alive if p["is_bot"]]:
        if random.random() < 0.55:
            if random.random() < 0.6:
                candidates = [c for c in alive if c["name"] != bot["name"]]
                if not candidates: continue
                weights = []
                for c in candidates:
                    w = 1.0
                    if c["faction"] in ("Mafia","Cult"): w = 3.0
                    w *= (0.8 + random.random()*0.8)
                    weights.append((c, w))
                total = sum(w for _,w in weights)
                r = random.random()*total
                upto = 0
                pick = weights[-1][0]
                for c,w in weights:
                    upto += w
                    if r <= upto:
                        pick = c; break
                room.setdefault("votes", {})[bot["name"]] = pick["name"]
                await broadcast(room_id, {"type":"system","text": f"🤖 {bot['name']} accused {pick['name']}"})
            else:
                pass

async def simulate_bot_verdict_votes(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    accused = room.get("accused")
    if not accused: return
    alive = [p for p in room["players"] if p["alive"]]
    await asyncio.sleep(max(1, DAY_FINAL//2))
    for bot in [p for p in alive if p["is_bot"]]:
        if bot["faction"] == "Mafia":
            choice = "innocent" if random.random() < 0.7 else "guilty"
        elif bot["faction"] == "Cult":
            choice = "innocent" if random.random() < 0.6 else "guilty"
        else:
            choice = "guilty" if random.random() < 0.55 else "innocent"
        room.setdefault("verdict_votes", {})[bot["name"]] = choice
        await broadcast(room_id, {"type":"system","text": f"🤖 {bot['name']} voted {choice} on {accused}"})

# ---------------- DETERMINE ACCUSED ----------------
async def determine_accused(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    votes = room.get("votes", {}) or {}
    if not votes:
        room["accused"] = None
        await broadcast(room_id, {"type":"system","text":"No accusations were made."})
        await broadcast(room_id, {"type":"accused_update","accused": None})
        return
    tally = {}
    for v in votes.values():
        tally[v] = tally.get(v, 0) + 1
    if not tally:
        room["accused"] = None
        await broadcast(room_id, {"type":"accused_update","accused": None})
        return
    top = max(tally, key=lambda k: tally[k])
    counts = sorted(tally.values(), reverse=True)
    if len(counts) > 1 and counts[0] == counts[1]:
        room["accused"] = None
        await broadcast(room_id, {"type":"system","text":"Tie in accusations — no one accused."})
        await broadcast(room_id, {"type":"accused_update","accused": None})
        return
    room["accused"] = top
    room.setdefault("accusation_history", []).append((room["day"], top))
    await broadcast(room_id, {"type":"system","text": f"{top} has been accused and will defend themselves."})
    await broadcast(room_id, {"type":"accused_update","accused": top})

# ---------------- VERDICT RESOLUTION ----------------
async def resolve_verdict(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    accused = room.get("accused")
    if not accused:
        await broadcast(room_id, {"type":"system","text":"No accused to judge."})
        return
    votes = room.get("verdict_votes", {}) or {}
    if not votes:
        await broadcast(room_id, {"type":"system","text":"No verdict votes — no lynch."})
        room["accused"] = None
        await broadcast(room_id, {"type":"accused_update","accused": None})
        return
    tally = {"guilty":0, "innocent":0}
    for v in votes.values():
        tally[v] = tally.get(v,0) + 1
    if tally["guilty"] > tally["innocent"]:
        victim = next((p for p in room["players"] if p["name"]==accused and p["alive"]), None)
        if victim:
            victim["alive"] = False
            if accused not in room.get("cleaned_bodies", set()):
                victim["revealed"] = True
                await broadcast(room_id, {"type":"system","text": f"⚖️ {accused} was found GUILTY — {victim['role']} ({victim['faction']})"})
            else:
                await broadcast(room_id, {"type":"system","text": f"⚠️ {accused} was found guilty but corpse cleaned; role hidden."})
            room["accused"] = None
            room["verdict_votes"] = {}
            await broadcast(room_id, {"type":"room","room": room_summary(room)})
            await check_victory(room_id)
            return
    else:
        await broadcast(room_id, {"type":"system","text": f"{accused} was found INNOCENT (tie or more innocent votes)."})
    room["accused"] = None
    room["verdict_votes"] = {}
    await broadcast(room_id, {"type":"room","room": room_summary(room)})

# ---------------- VOTES / DAY HELPERS ----------------
async def resolve_votes_if_ready(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    votes = room.get("votes", {}) or {}
    if not votes:
        await broadcast(room_id, {"type":"system","text":"No votes cast."})
        return
    tally = {}
    for v in votes.values():
        tally[v] = tally.get(v,0) + 1
    room["votes"] = {}
    if not tally:
        await broadcast(room_id, {"type":"system","text":"No valid votes."}); return
    top = max(tally, key=lambda k: tally[k])
    counts = sorted(tally.values(), reverse=True)
    if len(counts) > 1 and counts[0] == counts[1]:
        await broadcast(room_id, {"type":"system","text":"Vote tie — no accused chosen."})
        room["accused"] = None
        await broadcast(room_id, {"type":"accused_update","accused": None})
        return
    room["accused"] = top
    room.setdefault("accusation_history", []).append((room["day"], top))
    await broadcast(room_id, {"type":"system","text": f"{top} has been accused."})
    await broadcast(room_id, {"type":"accused_update","accused": top})

# ---------------- VICTORY ----------------
async def check_victory(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    alive = [p for p in room["players"] if p["alive"]]
    mafia_alive = [p for p in alive if p["faction"]=="Mafia"]
    cult_alive = [p for p in alive if p["faction"]=="Cult"]
    town_alive = [p for p in alive if p["faction"]=="Town"]
    neutral_alive = [p for p in alive if p["faction"]=="Neutral"]
    if not mafia_alive and not cult_alive:
        await end_game(room_id, "Town"); return
    if not town_alive and len(mafia_alive) >= len(cult_alive):
        await end_game(room_id, "Mafia"); return
    if len(cult_alive) >= (len(mafia_alive) + len(town_alive) + len(neutral_alive)):
        await end_game(room_id, "Cult"); return
    if (not mafia_alive and not cult_alive and not town_alive) and neutral_alive:
        await end_game(room_id, "Neutral"); return

async def end_game(room_id: str, winner_faction: str):
    room = rooms.get(room_id)
    if not room: return
    room["state"] = "ended"
    await broadcast(room_id, {"type":"system","text": f"🏆 {winner_faction} win!"})
    recap = "\n".join([f"{p['name']}: {p['role']} ({p['faction']}) {'Alive' if p['alive'] else 'Dead'}" for p in room["players"]])
    await broadcast(room_id, {"type":"system","text": "📜 Final Roles:\n" + recap})
    await broadcast(room_id, {"type":"room","room": room_summary(room)})

# ---------------- UTILS / STARTUP ----------------
@app.on_event("startup")
async def startup_event():
    # create a sample room so there's at least one on first boot
    if not rooms:
        r = create_room_obj("Host")
        print("Created sample room:", r["id"])

# ---------------- END OF FILE ----------------
