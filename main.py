# main.py
# Town of Shadows - Completed integrated backend
# Deploy: uvicorn main:app --host 0.0.0.0 --port 10000
import asyncio, json, random, time, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, Optional
from uuid import uuid4

app = FastAPI(title="Town of Shadows - Complete Server")

# === CONFIG ===
FRONTEND_ORIGIN = "https://690a8382d00a2311478c4251--celebrated-bonbon-1fd3cf.netlify.app"
BACKEND_WS_BASE = "wss://town-of-shadows-server.onrender.com/ws"  # for clarity; clients use full URL
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

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# === helpers ===
def sample(a): return random.choice(a)
def shuffle(a): b=a[:]; random.shuffle(b); return b

def build_mafia_roles():
    roles=["Godfather","Mafioso"]
    others=[r for r in ROLE_POOL["mafia"] if r not in roles]
    roles.append(sample(others))
    while len(roles) < FACTION_COUNTS["mafia"]:
        roles.append("Mafioso" if random.random()<0.5 else sample(others))
    random.shuffle(roles)
    return roles

def build_cult_roles():
    roles=["Cult Leader","Fanatic"]
    pool=[r for r in ROLE_POOL["cult"] if r not in roles]
    while len(roles) < FACTION_COUNTS["cult"]:
        roles.append(sample(pool))
    random.shuffle(roles)
    return roles

def build_town_roles(): return shuffle(ROLE_POOL["town"])[:FACTION_COUNTS["town"]]
def build_neutrals(): return shuffle(ROLE_POOL["neutrals"])[:FACTION_COUNTS["neutrals"]]

def build_full_roles():
    mafia = build_mafia_roles(); cult = build_cult_roles(); town = build_town_roles(); neutrals = build_neutrals()
    all_roles = town + mafia + cult + neutrals
    while len(all_roles) < TOTAL_PLAYERS:
        all_roles.append(sample(ROLE_POOL["town"]))
    random.shuffle(all_roles)
    return all_roles

def role_to_faction(role):
    if role in ROLE_POOL["town"]: return "Town"
    if role in ROLE_POOL["mafia"]: return "Mafia"
    if role in ROLE_POOL["cult"]: return "Cult"
    if role in ROLE_POOL["neutrals"]: return "Neutral"
    return "Unknown"

# === in-memory state ===
rooms: Dict[str, Any] = {}
room_ws: Dict[str, Any] = {}

class RoomWebsockets:
    def __init__(self):
        self.sockets: Dict[str, WebSocket] = {}
    async def send(self, message: dict):
        dead=[]
        for wsid, ws in list(self.sockets.items()):
            try:
                await ws.send_text(json.dumps(message))
            except Exception:
                dead.append(wsid)
        for d in dead:
            self.sockets.pop(d, None)

async def broadcast(room_id: str, message: dict):
    mgr = room_ws.get(room_id)
    if mgr:
        await mgr.send(message)

# === room creation ===
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
            # mechanics flags
            "cupid_used": False,
            "janitor_used": False,
            "vigilante_last_shot_day": -99, # day index of last shot for cooldown
            "vigilante_shots_left": 1,      # not used, keeps flexible
            "jailor_has_execute": True if r=="Jailor" else False,
            "guardian_protects": None,
        }
        players.append(p)
    room = {
        "id": room_id, "host": host_name, "players": players,
        "state":"waiting", "phase":"day", "day":1,
        "actions":[], "votes":{}, "sockets":{},
        "lovers":{}, "winners":[], "execution_targets":{},
        # phase controller
        "controller_task": None,
    }
    # pre-assign executioner targets
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
        "players":[
            {"slot":p["slot"], "name":p["name"], "alive":p["alive"], "revealed":p["revealed"],
             "symbol": p["role"] if p["revealed"] else ("?" if p["alive"] else None),
             "is_bot": p["is_bot"]}
            for p in room["players"]
        ]
    }

def role_explain(role, faction):
    explanations = {
        "Doctor":"Town. Heals a player each night; protection prevents kills except Beastman bypass.",
        "Detective":"Town. Investigates alignment.",
        "Beastman":"Mafia. Kill bypasses Doctor protection after contact.",
        "Vigilante":"Town. Can shoot every 2 nights. Kills only succeed vs Mafia members.",
        "Jailor":"Town. Jail one player each night; can execute once.",
        "Medium":"Town. Can talk with the dead at night.",
        "Cupid":"Neutral/Town. Link two players as lovers."
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
    slot["is_bot"] = False
    slot["name"] = req.name or slot["name"]
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

# === START GAME (send private roles immediately and start controller) ===
@app.post("/start-game/{rid}")
async def start_game(rid: str):
    room = rooms.get(rid)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["state"] == "active":
        return {"ok": True, "message":"Game already running"}
    room["state"] = "active"; room["phase"] = "day"; room["day"] = 1; room["actions"]=[]
    # send private roles to connected humans
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
    # launch controller
    room["controller_task"] = asyncio.create_task(phase_controller_loop(rid))
    return {"status":"started","room": room_summary(room)}

@app.post("/next-phase/{rid}")
async def next_phase(rid: str):
    room = rooms.get(rid)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["state"] != "active": raise HTTPException(status_code=400, detail="Game not active")
    # move immediately to night or day; used mainly for testing
    if room["phase"] == "day":
        # end day, start night
        room["phase"] = "night"
    else:
        room["phase"] = "day"; room["day"] += 1
    await broadcast(rid, {"type":"system","text":f"Phase forcibly changed to {room['phase']} (Day {room['day']})"})
    await broadcast(rid, {"type":"room","room": room_summary(room)})
    return {"ok": True}

# === Phase controller ===
async def phase_controller_loop(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    try:
        while room["state"] == "active":
            # DAY DISCUSSION
            room["phase"] = "day"
            await broadcast(room_id, {"type":"system","text":"🌞 Discussion time begins."})
            await broadcast(room_id, {"type":"phase","phase":"discussion","seconds": DAY_DISCUSS})
            # simulate bots voting/discussing mid-phase
            asyncio.create_task(simulate_bot_discussion_and_votes(room_id))
            await countdown_broadcast(room_id, DAY_DISCUSS)
            # VOTING
            await broadcast(room_id, {"type":"system","text":"🗳️ Voting time begins."})
            await broadcast(room_id, {"type":"phase","phase":"voting","seconds": DAY_VOTE})
            # bots will cast some votes during this subphase
            asyncio.create_task(simulate_bot_votes(room_id))
            await countdown_broadcast(room_id, DAY_VOTE)
            # DEFENCE
            await broadcast(room_id, {"type":"system","text":"🗣️ Defence time for the accused."})
            await broadcast(room_id, {"type":"phase","phase":"defence","seconds": DAY_DEFENCE})
            await countdown_broadcast(room_id, DAY_DEFENCE)
            # FINAL DECISION
            await broadcast(room_id, {"type":"system","text":"⏱️ Final decision time."})
            await broadcast(room_id, {"type":"phase","phase":"final","seconds": DAY_FINAL})
            await countdown_broadcast(room_id, DAY_FINAL)
            # resolve votes after final
            await resolve_votes_if_ready(room_id)
            # NIGHT START
            room["phase"] = "night"
            await broadcast(room_id, {"type":"system","text":"🌙 Night begins. Use your night abilities."})
            # tell clients who can private-chat at night
            await broadcast(room_id, {"type":"phase","phase":"night","seconds": NIGHT_DURATION, "allow": {"mafia": True, "cult": True, "medium": True, "lovers": True, "jailor_chat": True}})
            # let bots act some time during the night
            asyncio.create_task(simulate_bot_night_actions(room_id))
            # wait NIGHT_DURATION then resolve queued actions
            await countdown_broadcast(room_id, NIGHT_DURATION)
            # resolve night actions
            await apply_player_actions(room_id)
            # check victory
            await check_victory(room_id)
            if room.get("state") == "ended":
                break
            # continue loop -> next day
            room["day"] += 1
            await broadcast(room_id, {"type":"system","text":f"🌞 Day {room['day']} begins."})
    except Exception:
        traceback.print_exc()

async def countdown_broadcast(room_id: str, duration: int):
    deadline = time.time() + duration
    while True:
        remaining = int(deadline - time.time())
        if remaining < 0:
            await broadcast(room_id, {"type":"phase","seconds": 0})
            break
        await broadcast(room_id, {"type":"phase","seconds": remaining})
        await asyncio.sleep(1)

# === Bot simulation helpers ===
async def simulate_bot_discussion_and_votes(room_id: str):
    # bots may send system notes and register tentative votes during discussion
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    await asyncio.sleep(DAY_DISCUSS//2)
    alive = [p for p in room["players"] if p["alive"]]
    for bot in [p for p in alive if p["is_bot"]]:
        # sometimes bots vote even in discussion for realism
        if random.random() < 0.3:
            choices = [x["name"] for x in alive if x["name"] != bot["name"]]
            if choices:
                target = sample(choices)
                room.setdefault("votes", {})[bot["name"]] = target
                await broadcast(room_id, {"type":"system","text":f"🤖 {bot['name']} is leaning towards {target}"})

async def simulate_bot_votes(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    alive = [p for p in room["players"] if p["alive"]]
    await asyncio.sleep(DAY_VOTE//2)
    for bot in [p for p in alive if p["is_bot"]]:
        if bot["name"] not in room.get("votes", {}):
            choices = [x["name"] for x in alive if x["name"] != bot["name"]]
            if choices:
                room.setdefault("votes", {})[bot["name"]] = sample(choices)
                await broadcast(room_id, {"type":"system","text":f"🤖 {bot['name']} voted for {room['votes'][bot['name']]} (auto)"})

async def simulate_bot_night_actions(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    await asyncio.sleep(NIGHT_DURATION//3)
    alive = [p for p in room["players"] if p["alive"]]
    # mafia action
    mafia = [p for p in alive if p["faction"]=="Mafia"]
    if mafia:
        targets = [p for p in alive if p["faction"]!="Mafia"]
        if targets:
            victim = sample(targets)
            room.setdefault("actions", []).append({"actor": sample(mafia)["name"], "target": victim["name"], "type":"mafia_kill", "actor_role":"Mafioso"})
            await broadcast(room_id, {"type":"system","text":f"🤖 Mafia decided to target {victim['name']}"})
    # cult tries to convert
    cults = [p for p in alive if p["faction"]=="Cult"]
    if cults and random.random() < 0.4:
        candidates = [p for p in alive if p["faction"] not in ("Cult","Mafia")]
        if candidates:
            t = sample(candidates)
            room.setdefault("actions", []).append({"actor": sample(cults)["name"], "target": t["name"], "type":"cult_convert"})
            await broadcast(room_id, {"type":"system","text":f"🤖 Cult attempted to convert {t['name']}"})
    # serial killer
    sk = [p for p in alive if p["role"]=="Serial Killer"]
    if sk:
        killer = sk[0]
        targets = [p for p in alive if p["name"] != killer["name"]]
        if targets:
            t = sample(targets)
            room.setdefault("actions", []).append({"actor": killer["name"], "target": t["name"], "type":"serial_kill", "actor_role":"Serial Killer"})
            await broadcast(room_id, {"type":"system","text":f"🤖 Serial Killer chose a target."})
    # other bot actions could be added similarly

# === Voting resolution ===
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
        tally[v] = tally.get(v,0) + 1
    room["votes"] = {}
    if not tally:
        await broadcast(room_id, {"type":"system","text":"No consensus — no lynch."})
        return
    top = max(tally, key=lambda k: tally[k])
    victim = next((p for p in room["players"] if p["name"]==top and p["alive"]), None)
    if victim:
        victim["alive"] = False
        await broadcast(room_id, {"type":"system","text":f"⚖️ {victim['name']} was lynched!"})
        # jester check
        if victim["role"] == "Jester":
            if victim["name"] not in room["winners"]:
                room["winners"].append(victim["name"])
                await broadcast(room_id, {"type":"system","text":f"😈 {victim['name']} (Jester) achieved their win!"})
        # executioner check
        for exec_name, targ in list(room.get("execution_targets", {}).items()):
            if targ == victim["name"]:
                if exec_name not in room["winners"]:
                    room["winners"].append(exec_name)
                    await broadcast(room_id, {"type":"system","text":f"🔨 {exec_name} (Executioner) achieved their win!"})
        await reveal_death(room_id, victim)

# === Queue action endpoint ===
@app.post("/queue-action")
async def queue_action(body: dict = Body(...)):
    room_id = body.get("room_id"); actor = body.get("actor"); target = body.get("target"); atype = body.get("type")
    room = rooms.get(room_id)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["phase"] != "night": raise HTTPException(status_code=400, detail="Actions only at night")
    room.setdefault("actions", []).append({"actor": actor, "target": target, "type": atype})
    await broadcast(room_id, {"type":"system","text":f"{actor} queued action {atype} -> {target}"})
    return {"ok": True}

# === Apply night actions with doctor logic and beastman bypass + other mechanics ===
async def apply_player_actions(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    actions = room.get("actions", [])
    protected_by_doctor = set()
    protected_by_bodyguard = set()
    janitor_cleans = set()
    queued_kills = []
    queued_converts = []
    queued_jails = []   # tuple (jailor_name, target_name, execute_flag)
    queued_investigations = []
    queued_medium_msgs = []
    # Collect actions
    for action in actions:
        actor = action.get("actor"); target = action.get("target"); atype = action.get("type",""); actor_role = action.get("actor_role")
        actor_p = next((p for p in room["players"] if p["name"]==actor), None)
        target_p = next((p for p in room["players"] if p["name"]==target), None)
        if not actor_p:
            continue
        # Doctor heal: protection applied regardless of order (doctor heal prevents kills except Beastman)
        if atype == "doctor_heal":
            protected_by_doctor.add(target)
            await broadcast(room_id, {"type":"system","text":f"🩺 {actor} healed {target} (doctor)."})
        elif atype == "bodyguard_protect":
            protected_by_bodyguard.add(target)
            await broadcast(room_id, {"type":"system","text":f"🛡️ {actor} is protecting {target} (bodyguard)."})
        elif atype == "janitor_clean":
            janitor_cleans.add(target)
            await broadcast(room_id, {"type":"system","text":f"🧹 {actor} intends to clean {target}'s body."})
        elif atype == "cupid_link":
            if not actor_p.get("cupid_used", False):
                room["lovers"][actor] = target
                room["lovers"][target] = actor
                actor_p["cupid_used"] = True
                await broadcast(room_id, {"type":"system","text":f"💞 Cupid linked {actor} and {target}."})
                # private message to lovers
                for lover in (actor, target):
                    p = next((pp for pp in room["players"] if pp["name"]==lover), None)
                    if p and p.get("ws_id"):
                        ws = room_ws[room_id].sockets.get(p["ws_id"])
                        if ws:
                            try: await ws.send_text(json.dumps({"type":"private","to":lover,"text":"You are now lovers."}))
                            except: pass
        elif atype == "mafia_kill":
            queued_kills.append({"victim": target, "by": actor, "type":"mafia_kill", "actor_role": actor_p.get("role")})
        elif atype in ("beast_kill","beastman_kill"):
            queued_kills.append({"victim": target, "by": actor, "type":"beast_kill", "actor_role": actor_p.get("role")})
        elif atype == "serial_kill":
            queued_kills.append({"victim": target, "by": actor, "type":"serial_kill", "actor_role": actor_p.get("role")})
        elif atype == "cult_convert":
            queued_converts.append({"target": target, "by": actor})
        elif atype == "jail":
            queued_jails.append({"jailor": actor, "target": target, "execute": action.get("execute", False)})
        elif atype == "detective_check":
            queued_investigations.append({"investigator": actor, "target": target, "role": actor_p.get("role")})
        # other abilities (witch, arsonist ignite, guardian, vigilante, etc.) represented as actions too

        elif atype == "vigilante_shot":
            # vigilante: allowed only if off cooldown and target is mafia (including culted mafia)
            queued_kills.append({"victim": target, "by": actor, "type":"vigilante_shot", "actor_role": actor_p.get("role")})

    # Apply converts first (affects faction checks later)
    for c in queued_converts:
        t = next((p for p in room["players"] if p["name"]==c["target"] and p["alive"]), None)
        if t and t["role"] not in ("Godfather","Mafioso","Beastman","Soldier"):
            t["faction"] = "Cult"
            t["role"] = "Acolyte"
            await broadcast(room_id, {"type":"system","text":f"✨ {t['name']} was converted to the Cult!"})

    # Apply jails: jailed players cannot be targeted by others
    jailed_names = set()
    for j in queued_jails:
        jailor = j["jailor"]; tgt = j["target"]; exec_flag = j["execute"]
        jailed_names.add(tgt)
        # if execute flag set, process execution immediately (perform a lynch-style result)
        jailor_p = next((p for p in room["players"] if p["name"]==jailor), None)
        tgt_p = next((p for p in room["players"] if p["name"]==tgt and p["alive"]), None)
        if exec_flag and jailor_p and jailor_p.get("jailor_has_execute", True):
            # execute the jailed target
            if tgt_p:
                tgt_p["alive"] = False
                await broadcast(room_id, {"type":"system","text":f"⚖️ {tgt_p['name']} was executed by Jailor {jailor}!"})
                await reveal_death(room_id, tgt_p)
            # if executed a Town member and jailor misused, lose execution permanently
            if tgt_p and tgt_p["faction"] == "Town":
                jailor_p["jailor_has_execute"] = False
                await broadcast(room_id, {"type":"system","text":f"⚠️ {jailor}'s execute targeted a Town member — they lose execution ability."})

    # Resolve kills with Cupid sacrifice and Doctor protection (beast bypass)
    final_kills = []
    for k in queued_kills:
        victim = k["victim"]
        actor_role = k.get("actor_role","")
        kill_type = k.get("type","")
        # if target is jailed, skip (protected)
        if victim in jailed_names:
            await broadcast(room_id, {"type":"system","text":f"🔒 {victim} was jailed and could not be targeted."})
            continue
        # Cupid sacrifice: if victim has lover alive -> lover dies instead
        lover = room["lovers"].get(victim)
        if lover and any(p["name"]==lover and p["alive"] for p in room["players"]):
            final_kills.append({"name": lover, "killed_by": k["by"], "type": kill_type, "actor_role": actor_role})
            await broadcast(room_id, {"type":"system","text":f"💔 {lover} sacrificed themselves for their lover {victim}!"})
        else:
            final_kills.append({"name": victim, "killed_by": k["by"], "type": kill_type, "actor_role": actor_role})

    applied_dead = set()
    for entry in final_kills:
        name = entry["name"]
        kill_type = entry["type"]
        actor_role = entry.get("actor_role","")
        # determine if doctor protection applies and whether beastman bypasses it
        bypass = (actor_role == "Beastman") or (kill_type in ("beast_kill","beastman_kill"))
        if (name in protected_by_doctor or name in protected_by_bodyguard) and (not bypass):
            await broadcast(room_id, {"type":"system","text":f"🛡️ {name} was attacked but survived (protected)."})
            continue
        # victim dies
        victim = next((p for p in room["players"] if p["name"]==name and p["alive"]), None)
        if victim:
            victim["alive"] = False
            # if janitor cleaned, hide reveal
            if name not in janitor_cleans:
                await reveal_death(room_id, victim)
            else:
                victim["revealed"] = False
                await broadcast(room_id, {"type":"system","text": f"⚠️ A corpse was found but has been cleaned; role hidden."})
            applied_dead.add(name)
            # extra checks: if victim role Jester, Executioner etc. handled on reveal/lynch earlier
    # clear actions
    room["actions"] = []
    # broadcast room change
    await broadcast(room_id, {"type":"room","room": room_summary(room)})

# reveal helper
async def reveal_death(room_id: str, player):
    if not player.get("revealed", False):
        player["revealed"] = True
    msg = f"💀 {player['name']} was the {player['role']} ({player['faction']})!"
    await broadcast(room_id, {"type":"system","text": msg})

# === Victory & end game ===
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
    if len(cult_alive) >= len(mafia_alive) + len(town_alive):
        await end_game(room_id, "Cult"); return
    if not any([mafia_alive, cult_alive, town_alive]) and neutral_alive:
        await end_game(room_id, "Neutral"); return

async def end_game(room_id: str, winner_faction: str):
    room = rooms.get(room_id)
    if not room: return
    room["state"] = "ended"; room["winner"] = winner_faction
    if winner_faction == "Town":
        msg = "🌼 The Town has purged the darkness and won!"
    elif winner_faction == "Mafia":
        msg = "💀 The Mafia controls the shadows — they win!"
    elif winner_faction == "Cult":
        msg = "🔮 The Cult dominates!"
    else:
        msg = "⚖️ Neutrals outlived all others!"
    await broadcast(room_id, {"type":"system","text": msg})
    await asyncio.sleep(1)
    if room.get("winners"):
        await broadcast(room_id, {"type":"system","text": "🏆 Individual winners: " + ", ".join(room["winners"])})
    recap = "\n".join([f"{p['name']}: {p['role']} ({p['faction']}) {'✅' if p['alive'] else '💀'}" for p in room["players"]])
    await broadcast(room_id, {"type":"system","text": "📜 Final Roles:\n" + recap})
    await broadcast(room_id, {"type":"room","room": room_summary(room)})

# === WebSocket endpoint ===
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    wsid = str(uuid4())
    room = rooms.get(room_id)
    if not room:
        await websocket.send_text(json.dumps({"type":"system","text":"Room not found"}))
        await websocket.close(); return
    room["sockets"][wsid] = None
    room_ws[room_id].sockets[wsid] = websocket
    try:
        await websocket.send_text(json.dumps({"type":"system","text":f"Connected to room {room_id}", "ws_id": wsid}))
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except:
                await websocket.send_text(json.dumps({"type":"system","text":"Invalid JSON"})); continue
            mtype = msg.get("type")
            if mtype == "identify":
                slot = msg.get("slot")
                p = next((pp for pp in room["players"] if pp["slot"]==slot), None)
                if p:
                    p["ws_id"] = wsid; p["is_bot"] = False; room["sockets"][wsid] = p["name"]
                    await websocket.send_text(json.dumps({"type":"system","text":f"You are {p['name']} (slot {p['slot']})"}))
                    if room["state"] == "active":
                        await websocket.send_text(json.dumps({"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"],"explain":role_explain(p["role"],p["faction"])}))
                    await broadcast(room_id, {"type":"room","room": room_summary(room)})
            elif mtype == "create_room":
                r = create_room_object(msg.get("name","Host"))
                await websocket.send_text(json.dumps({"type":"room_info","room": room_summary(r), "roomId": r["id"]}))
            elif mtype == "join_room":
                slot = next((p for p in room["players"] if p["is_bot"]), None)
                if not slot:
                    await websocket.send_text(json.dumps({"type":"system","text":"Room full"})); continue
                slot["is_bot"] = False; slot["name"] = msg.get("name","Player"); slot["ws_id"] = wsid; room["sockets"][wsid] = slot["name"]
                await websocket.send_text(json.dumps({"type":"private_role","slot":slot["slot"],"role":slot["role"],"faction":slot["faction"],"explain":role_explain(slot["role"],slot["faction"])}))
                await broadcast(room_id, {"type":"system","text":f"{slot['name']} joined as {slot['slot']}"})
                await broadcast(room_id, {"type":"room","room": room_summary(room)})
            elif mtype == "start_game":
                try:
                    await start_game(room_id)
                except Exception as e:
                    await websocket.send_text(json.dumps({"type":"system","text":str(e)}))
            elif mtype == "player_action":
                action = msg.get("action")
                if action:
                    room.setdefault("actions", []).append(action)
                    await websocket.send_text(json.dumps({"type":"system","text":"Action queued"}))
            elif mtype == "vote":
                slot = msg.get("slot"); target = msg.get("target")
                p = next((pp for pp in room["players"] if pp["slot"]==slot), None)
                if p:
                    room.setdefault("votes", {})[p["name"]] = target
                    await broadcast(room_id, {"type":"system","text":f"{p['name']} voted for {target}"})
                    await resolve_votes_if_ready(room_id)
            elif mtype == "chat":
                # chat routing: include 'channel' optional: 'public', 'mafia', 'cult', 'lovers', 'medium'
                channel = msg.get("channel", "public")
                text = msg.get("text","")
                sender = msg.get("from","")
                # broadcast based on channel
                if channel == "public":
                    await broadcast(room_id, {"type":"chat","from":sender,"text":text,"channel":"public"})
                elif channel == "mafia":
                    # send to mafia members only
                    for p in room["players"]:
                        if p["faction"] == "Mafia" and p.get("ws_id"):
                            ws = room_ws[room_id].sockets.get(p["ws_id"])
                            if ws:
                                try: await ws.send_text(json.dumps({"type":"chat","from":sender,"text":text,"channel":"mafia"}))
                                except: pass
                elif channel == "cult":
                    for p in room["players"]:
                        if p["faction"] == "Cult" and p.get("ws_id"):
                            ws = room_ws[room_id].sockets.get(p["ws_id"])
                            if ws:
                                try: await ws.send_text(json.dumps({"type":"chat","from":sender,"text":text,"channel":"cult"}))
                                except: pass
                elif channel == "lovers":
                    # lovers chat: send only to lover pair
                    lover = next((pp for pp in room["players"] if pp["name"]==sender), None)
                    if lover:
                        partner = room["lovers"].get(sender)
                        for nm in (sender, partner):
                            p = next((pp for pp in room["players"] if pp["name"]==nm and pp.get("ws_id")), None)
                            if p:
                                ws = room_ws[room_id].sockets.get(p["ws_id"])
                                if ws:
                                    try: await ws.send_text(json.dumps({"type":"chat","from":sender,"text":text,"channel":"lovers"}))
                                    except: pass
                elif channel == "medium":
                    # medium chat with dead: send to mediums (alive) and dead players' ws (if they had ws)
                    for p in room["players"]:
                        if (p["role"]=="Medium" and p.get("ws_id")) or (not p["alive"] and p.get("ws_id")):
                            ws = room_ws[room_id].sockets.get(p["ws_id"])
                            if ws:
                                try: await ws.send_text(json.dumps({"type":"chat","from":sender,"text":text,"channel":"medium"}))
                                except: pass
                else:
                    await broadcast(room_id, {"type":"chat","from":sender,"text":text,"channel":channel})
            else:
                await websocket.send_text(json.dumps({"type":"system","text":"Unknown message type"}))
    except WebSocketDisconnect:
        s = room["sockets"].pop(wsid, None)
        room_ws[room_id].sockets.pop(wsid, None)
        if s:
            p = next((pp for pp in room["players"] if pp["name"]==s), None)
            if p:
                p["is_bot"] = True; p["ws_id"] = None
        await broadcast(room_id, {"type":"system","text":"A player disconnected"})
    except Exception as e:
        traceback.print_exc()
        try: await websocket.send_text(json.dumps({"type":"system","text":"Server error: "+str(e)}))
        except: pass
