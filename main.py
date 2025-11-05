# main.py
# Town of Shadows - All-in-one backend (FastAPI + WebSocket)
# - Integrated roles and skills (Doctor, Beastman, Cupid, Janitor, Jester, Executioner, etc.)
# - Timers & phase segmentation: Day (60s discussion, 20s voting, 10s defence, 10s final), Night (40s)
# - Doctor protection: prevents death for healed target except Beastman kills bypass it
# - On game start all connected humans receive their private role immediately
#
# Deploy with:
#   requirements.txt: fastapi, uvicorn[standard]
#   start command: uvicorn main:app --host 0.0.0.0 --port 10000

import asyncio, json, random, traceback, time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, Optional
from uuid import uuid4

app = FastAPI(title="Town of Shadows - Complete Server")

# === CONFIG ===
FRONTEND_ORIGIN = "https://690a8382d00a2311478c4251--celebrated-bonbon-1fd3cf.netlify.app"
TOTAL_PLAYERS = 20
# Phase durations (seconds)
DAY_DISCUSS = 60
DAY_VOTE = 20
DAY_DEFENCE = 10
DAY_FINAL = 10
NIGHT_DURATION = 40

FACTION_COUNTS = {"town": 8, "mafia": 5, "cult": 4, "neutrals": 3}

ROLE_POOL = {
    "town": ["Detective","Sheriff","Investigator","Lookout","Tracker","Bodyguard","Doctor","Jailor","Cupid","Mayor","Vigilante","Escort","Medium","Soldier","Gossip"],
    "mafia": ["Godfather","Mafioso","Janitor","Spy","Beastman","Consort","Blackmailer","Framer","Disguiser","Forger"],
    "cult": ["Cult Leader","Fanatic","Infiltrator","Prophet","High Priest","Acolyte"],
    "neutrals": ["Jester","Executioner","Serial Killer","Arsonist","Survivor","Amnesiac","Witch","Guardian Angel"]
}

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === util helpers ===
def sample(a): return random.choice(a)
def shuffle(a): b=a[:]; random.shuffle(b); return b

def build_mafia_roles():
    roles=["Godfather","Mafioso"]
    others=[r for r in ROLE_POOL["mafia"] if r not in roles]
    roles.append(sample(others))
    while len(roles) < FACTION_COUNTS["mafia"]:
        roles.append("Mafioso" if random.random()<0.5 else sample(others))
    random.shuffle(roles); return roles

def build_cult_roles():
    roles=["Cult Leader","Fanatic"]
    pool=[r for r in ROLE_POOL["cult"] if r not in roles]
    while len(roles) < FACTION_COUNTS["cult"]:
        roles.append(sample(pool))
    random.shuffle(roles); return roles

def build_town_roles(): return shuffle(ROLE_POOL["town"])[:FACTION_COUNTS["town"]]
def build_neutrals(): return shuffle(ROLE_POOL["neutrals"])[:FACTION_COUNTS["neutrals"]]

def build_full_roles():
    mafia = build_mafia_roles(); cult = build_cult_roles(); town = build_town_roles(); neutrals = build_neutrals()
    all_roles = town + mafia + cult + neutrals
    while len(all_roles) < TOTAL_PLAYERS:
        all_roles.append(sample(ROLE_POOL["town"]))
    random.shuffle(all_roles); return all_roles

def role_to_faction(role):
    if role in ROLE_POOL["town"]: return "Town"
    if role in ROLE_POOL["mafia"]: return "Mafia"
    if role in ROLE_POOL["cult"]: return "Cult"
    if role in ROLE_POOL["neutrals"]: return "Neutral"
    return "Unknown"

# === in-memory state ===
rooms: Dict[str, Any] = {}
room_ws: Dict[str, Any] = {}   # room_id -> RoomWebsockets

class RoomWebsockets:
    def __init__(self):
        self.sockets: Dict[str, WebSocket] = {}
    async def send(self, message: dict):
        dead=[]
        for ws_id, ws in list(self.sockets.items()):
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(ws_id)
        for d in dead:
            self.sockets.pop(d, None)

async def broadcast(room_id: str, message: dict):
    mgr = room_ws.get(room_id)
    if mgr:
        await mgr.send(message)

# === create room ===
def create_room_object(host_name="Host"):
    room_id = str(uuid4())[:6].upper()
    roles = build_full_roles()
    players=[]
    for i in range(1, TOTAL_PLAYERS+1):
        r = roles[i-1]
        p = {
            "slot": i,
            "name": f"Player {i}",
            "role": r,
            "faction": role_to_faction(r),
            "is_bot": True,
            "alive": True,
            "revealed": False,
            "doused": False,
            "ws_id": None,
            # role flags
            "doctor_self_heals_left": 9999 if r=="Doctor" else 0,  # unlimited as you specified
            "cupid_used": False,
            "executioner_target": None,
            "janitor_used": False,
        }
        players.append(p)
    room = {
        "id": room_id, "host": host_name, "players": players,
        "state": "waiting", "phase": "day", "day": 1,
        "actions": [], "votes": {}, "sockets": {},
        "lovers": {}, "winners": [], "execution_targets": {},
        # per-phase countdown control
        "phase_controller": None,
        "phase_deadline": None
    }
    # pre-assign executioner targets (if any)
    for p in room["players"]:
        if p["role"] == "Executioner":
            possible = [x["name"] for x in room["players"] if x["name"] != p["name"]]
            if possible:
                room["execution_targets"][p["name"]] = sample(possible)
    rooms[room_id] = room
    room_ws[room_id] = RoomWebsockets()
    return room

def room_summary(room):
    return {
        "id": room["id"], "host": room["host"], "state": room["state"],
        "phase": room["phase"], "day": room["day"],
        "players": [{ "slot":p["slot"], "name":p["name"], "alive":p["alive"], "revealed":p["revealed"], "symbol": p["role"] if p["revealed"] else ("?" if p["alive"] else None), "is_bot":p["is_bot"] } for p in room["players"]]
    }

def role_explain(role, faction):
    explanations = {
        "Doctor":"Town. Heals a player each night; protection prevents kills except Beastman bypass.",
        "Detective":"Town. Investigates whether target is Mafia.",
        "Beastman":"Mafia-affiliated; kills bypass Doctor protection after contact.",
        "Jester":"Neutral. Wins if lynched; game continues.",
        "Executioner":"Neutral. Picks a target; wins if target is lynched; game continues."
    }
    return explanations.get(role, f"{role} — {faction} role.")

# === REST endpoints ===
class JoinRequest(BaseModel):
    roomId: str
    name: Optional[str] = "Player"

@app.get("/test")
def test():
    return {"message":"Hello from Town of Shadows backend!"}

@app.post("/create-room")
def create_room(name: Optional[str] = "Host"):
    room = create_room_object(name)
    return {"roomId": room["id"], "room": room_summary(room)}

@app.post("/join-room")
def join_room(req: JoinRequest):
    room = rooms.get(req.roomId)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    slot = next((p for p in room["players"] if p["is_bot"]), None)
    if not slot: raise HTTPException(status_code=400, detail="Room full")
    slot["is_bot"] = False; slot["name"] = req.name or slot["name"]
    # return private role and current room summary
    return {"slot": slot["slot"], "role": slot["role"], "faction": slot["faction"], "explain": role_explain(slot["role"], slot["faction"]), "room": room_summary(room)}

@app.get("/room/{room_id}")
def get_room(room_id: str):
    room = rooms.get(room_id)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    return room_summary(room)

class ActionRequest(BaseModel):
    roomId: str
    slot: int
    action: dict

@app.post("/player-action")
def player_action(req: ActionRequest):
    room = rooms.get(req.roomId)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["phase"] != "night": raise HTTPException(status_code=400, detail="Actions only at night")
    act = req.action; act["by"] = req.slot
    room.setdefault("actions", []).append(act)
    return {"status":"queued","action":act}

# === Start game: send private roles to every connected human immediately ===
@app.post("/start-game/{rid}")
async def start_game(rid: str):
    room = rooms.get(rid)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["state"] == "active":
        return {"ok": True, "message": "Game already running"}
    room["state"] = "active"; room["phase"] = "day"; room["day"] = 1
    # send private roles to all connected humans (ws_id present)
    for p in room["players"]:
        if not p["is_bot"] and p.get("ws_id"):
            ws = room_ws[rid].sockets.get(p["ws_id"])
            if ws:
                try:
                    await ws.send_text(json.dumps({"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"],"explain":role_explain(p["role"],p["faction"])}))
                except:
                    pass
    await broadcast(rid, {"type":"system","text":"Game started. Day 1 begins."})
    await broadcast(rid, {"type":"room","room": room_summary(room)})
    # begin the phase controller loop for this room
    asyncio.create_task(phase_controller_loop(rid))
    return {"status":"started","room": room_summary(room)}

@app.post("/next-phase/{rid}")
async def next_phase(rid: str):
    room = rooms.get(rid)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["state"] != "active": raise HTTPException(status_code=400, detail="Game not active")
    # toggle to next logical phase: if day -> move to night immediately
    room["phase"] = "night" if room["phase"] == "day" else "day"
    if room["phase"] == "day": room["day"] += 1
    await broadcast(rid, {"type":"system","text":f"Phase changed to {room['phase']} (Day {room['day']})"})
    await broadcast(rid, {"type":"room","room": room_summary(room)})
    return {"ok": True, "phase": room["phase"], "day": room["day"]}

# === Phase controller: orchestrates segmented day timers and night timers ===
async def phase_controller_loop(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    # Day sequence: discussion -> voting -> defence -> final -> process lynch -> night
    # Each subphase broadcasts the current subphase name and remaining seconds every 5s
    async def broadcast_countdown(subphase_name, duration):
        deadline = time.time() + duration
        await broadcast(room_id, {"type":"phase","phase": subphase_name, "seconds": duration})
        interval = 5
        while True:
            remaining = int(deadline - time.time())
            if remaining <= 0:
                await broadcast(room_id, {"type":"phase","phase": subphase_name, "seconds": 0})
                break
            if remaining % interval == 0:
                await broadcast(room_id, {"type":"phase","phase": subphase_name, "seconds": remaining})
            await asyncio.sleep(1)
    try:
        while room["state"] == "active":
            # DAY DISCUSSION
            room["phase"] = "day"
            await broadcast(room_id, {"type":"system","text":"🌞 Discussion time begins."})
            await broadcast_countdown("discussion", DAY_DISCUSS)
            # VOTING
            await broadcast(room_id, {"type":"system","text":"🗳️ Voting time begins."})
            await broadcast_countdown("voting", DAY_VOTE)
            # DEFENCE
            await broadcast(room_id, {"type":"system","text":"🗣️ Defence time for the accused."})
            await broadcast_countdown("defence", DAY_DEFENCE)
            # FINAL DECISION
            await broadcast(room_id, {"type":"system","text":"⏱️ Final decision time."})
            await broadcast_countdown("final", DAY_FINAL)
            # after final decision resolve votes (if majority)
            await resolve_votes_if_ready(room_id)
            # NIGHT START
            room["phase"] = "night"
            await broadcast(room_id, {"type":"system","text":"🌙 Night begins. Use your night abilities."})
            await broadcast(room_id, {"type":"room","room": room_summary(room)})
            # allow faction chats: server trusts frontend to only show allowed chats; but we can broadcast allowed flags
            await broadcast(room_id, {"type":"phase","phase":"night","seconds":NIGHT_DURATION, "allow": {"mafia":True,"cult":True,"medium":True,"cupid":True}})
            # bots and humans can queue actions during night; wait NIGHT_DURATION then resolve
            await asyncio.sleep(NIGHT_DURATION)
            # resolve queued actions
            await apply_player_actions(room_id)
            await check_victory(room_id)
            # loop continues; if game ended check_victory sets room state to ended
            if room.get("state") == "ended":
                break
    except Exception as e:
        traceback.print_exc()

# === Bot behavior (day votes & night acts) ===
async def perform_bot_day_actions(room_id: str):
    room = rooms.get(room_id)
    if not room or room["phase"] != "day" or room["state"] != "active": return
    alive_players = [p for p in room["players"] if p["alive"]]
    for bot in [p for p in alive_players if p["is_bot"]]:
        choices = [p["name"] for p in alive_players if p["name"] != bot["name"]]
        if not choices: continue
        # simulate timing: bots vote mid-way through voting period; but since controller runs everything,
        # they will be added whenever perform_bot_day_actions is triggered.
        tgt = sample(choices)
        room.setdefault("votes", {})[bot["name"]] = tgt
        await broadcast(room_id, {"type":"system","text":f"🤖 {bot['name']} voted for {tgt}"})

async def resolve_votes_if_ready(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    alive = [p for p in room["players"] if p["alive"]]
    if len(room.get("votes", {})) >= len(alive):
        await resolve_votes(room_id)

async def resolve_votes(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    tally = {}
    for v in room.get("votes", {}).values():
        tally[v] = tally.get(v,0)+1
    room["votes"] = {}
    if not tally:
        # no lynch
        await broadcast(room_id, {"type":"system","text":"No consensus reached — no lynch."})
        return
    # pick top vote
    top = max(tally, key=lambda k: tally[k])
    victim = next((p for p in room["players"] if p["name"] == top and p["alive"]), None)
    if victim:
        victim["alive"] = False
        await broadcast(room_id, {"type":"system","text":f"⚖️ {victim['name']} was lynched!"})
        # jester check
        if victim["role"] == "Jester":
            if victim["name"] not in room["winners"]:
                room["winners"].append(victim["name"])
                await broadcast(room_id, {"type":"system","text":f"😈 {victim['name']} (Jester) achieved their win!"})
        # executioner checks
        for exec_name, targ in list(room.get("execution_targets", {}).items()):
            if targ == victim["name"]:
                if exec_name not in room["winners"]:
                    room["winners"].append(exec_name)
                    await broadcast(room_id, {"type":"system","text":f"🔨 {exec_name} (Executioner) achieved their win!"})
        # reveal by default (lynch reveal)
        await reveal_death(room_id, victim)
    # after lynch, phase will proceed to night by controller

# === Action queue endpoint (allow clients to queue night actions) ===
@app.post("/queue-action")
async def queue_action(body: dict = Body(...)):
    room_id = body.get("room_id"); actor = body.get("actor"); target = body.get("target"); atype = body.get("type")
    room = rooms.get(room_id)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["phase"] != "night": raise HTTPException(status_code=400, detail="Actions only at night")
    room.setdefault("actions", []).append({"actor": actor, "target": target, "type": atype})
    await broadcast(room_id, {"type":"system","text":f"{actor} queued action {atype} -> {target}"})
    return {"ok": True}

# === Core action resolution: doctor protection and beastman bypass ===
async def apply_player_actions(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    actions = room.get("actions", [])
    protected_by_doctor = set()
    protected_by_bodyguard = set()
    janitor_cleans = set()
    queued_kills = []
    queued_converts = []
    # first pass: collect actions
    for action in actions:
        actor_name = action.get("actor"); target_name = action.get("target"); atype = action.get("type","")
        actor = next((p for p in room["players"] if p["name"]==actor_name), None)
        target = next((p for p in room["players"] if p["name"]==target_name), None)
        if not actor or not target:
            continue
        # Doctor heal: adds to protected set that prevents kills (except beast_kill)
        if atype == "doctor_heal":
            protected_by_doctor.add(target_name)
        elif atype == "bodyguard_protect":
            protected_by_bodyguard.add(target_name)
        elif atype == "janitor_clean":
            # janitor intends to clean corpse; we'll check that corpse exists and was killed
            janitor_cleans.add(target_name)
        elif atype == "cupid_link":
            # store lovers mapping (actor <-> target)
            if actor and not actor.get("cupid_used"):
                room["lovers"][actor_name] = target_name
                room["lovers"][target_name] = actor_name
                actor["cupid_used"] = True
                # private messages to lovers if connected
                for lover in (actor_name, target_name):
                    p = next((pp for pp in room["players"] if pp["name"]==lover), None)
                    if p and p.get("ws_id"):
                        ws = room_ws[room_id].sockets.get(p["ws_id"])
                        if ws:
                            try: await ws.send_text(json.dumps({"type":"private","to":lover,"text":"You are now linked as lovers."}))
                            except: pass
                await broadcast(room_id, {"type":"system","text":f"💞 {actor_name} and {target_name} are linked by Cupid."})
        elif atype in ("mafia_kill","lynch_kill","serial_kill","beast_kill","beastman_kill"):
            queued_kills.append({"victim": target_name, "by": actor_name, "type": atype, "actor_role": actor.get("role")})
        elif atype == "cult_convert":
            queued_converts.append({"target": target_name, "by": actor_name})
        elif atype == "executioner_set":
            room["execution_targets"][actor_name] = target_name
    # apply converts first
    for c in queued_converts:
        target = next((p for p in room["players"] if p["name"]==c["target"] and p["alive"]), None)
        if target and target["role"] not in ("Godfather","Mafioso","Janitor","Beastman","Soldier"):
            target["faction"] = "Cult"
            target["role"] = "Acolyte"
            await broadcast(room_id, {"type":"system","text":f"✨ {target['name']} was converted to the Cult!"})
    # resolve kills with doctor protection rule: patient in protected_by_doctor survives all kills except when killer is Beastman or kill type is beast_kill
    final_kill_names = []
    for k in queued_kills:
        victim = k["victim"]
        killer_role = k.get("actor_role","")
        kill_type = k.get("type","")
        # Cupid sacrifice rule: if victim has lover who is alive -> lover dies instead (sacrifice)
        lover = room["lovers"].get(victim)
        if lover and any(p["name"]==lover and p["alive"] for p in room["players"]):
            final_kill_names.append(lover)
            await broadcast(room_id, {"type":"system","text":f"💔 {lover} sacrificed themself for their lover {victim}!"})
        else:
            final_kill_names.append(victim)
    applied_dead = set()
    for name in set(final_kill_names):
        # check protections
        bypass = False
        # if ANY queued kill on this name
