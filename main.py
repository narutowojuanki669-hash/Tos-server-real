# main.py
# Town of Shadows - Full backend (WebSocket + REST + AI + role mechanics)
# Deploy: uvicorn main:app --host 0.0.0.0 --port 10000
import asyncio, json, random, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, Optional
from uuid import uuid4

app = FastAPI(title="Town of Shadows - Full Server")

# === CONFIG ===
FRONTEND_ORIGIN = "https://690a8382d00a2311478c4251--celebrated-bonbon-1fd3cf.netlify.app"
TOTAL_PLAYERS = 20
FACTION_COUNTS = {"town": 8, "mafia": 5, "cult": 4, "neutrals": 3}

ROLE_POOL = {
    "town": ["Detective","Sheriff","Investigator","Lookout","Tracker","Bodyguard","Doctor","Jailor","Cupid","Mayor","Vigilante","Escort","Medium","Soldier","Gossip"],
    "mafia": ["Godfather","Mafioso","Janitor","Spy","Beastman","Consort","Blackmailer","Framer","Disguiser","Forger"],
    "cult": ["Cult Leader","Fanatic","Infiltrator","Prophet","High Priest","Acolyte"],
    "neutrals": ["Jester","Executioner","Serial Killer","Arsonist","Survivor","Amnesiac","Witch","Guardian Angel"]
}

# allow cors
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- utilities ---
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

# --- in-memory state ---
rooms: Dict[str, Any] = {}      # room_id -> room dict
room_ws: Dict[str, Any] = {}    # room_id -> RoomWebsockets instance

class RoomWebsockets:
    def __init__(self): self.sockets = {}  # ws_id -> websocket
    async def send(self, msg):
        dead=[]
        for ws_id, ws in list(self.sockets.items()):
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(ws_id)
        for d in dead: self.sockets.pop(d, None)

async def broadcast(room_id: str, message: dict):
    mgr = room_ws.get(room_id)
    if mgr:
        await mgr.send(message)

# === room creation ===
def create_room_object(host_name="Host"):
    room_id = str(uuid4())[:6].upper()
    roles = build_full_roles()
    players = []
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
            # role-specific flags
            "doctor_self_heals_left": 2 if r=="Doctor" else 0,
            "cupid_used": False,
            "executioner_target": None,
            "janitor_used": False
        }
        players.append(p)
    room = {
        "id": room_id,
        "host": host_name,
        "players": players,
        "state": "waiting",
        "phase": "day",
        "day": 1,
        "actions": [],
        "votes": {},
        "sockets": {},
        "lovers": {},               # name->lover_name
        "winners": [],              # individual winners (jester/executioner)
        "execution_targets": {},    # exec_name -> target_name
    }
    # assign executioner targets if any
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
        "id": room["id"],
        "host": room["host"],
        "state": room["state"],
        "phase": room["phase"],
        "day": room["day"],
        "players": [{
            "slot": p["slot"],
            "name": p["name"],
            "alive": p["alive"],
            "revealed": p["revealed"],
            "symbol": p["role"] if p["revealed"] else ("?" if p["alive"] else None),
            "is_bot": p["is_bot"]
        } for p in room["players"]]
    }

def role_explain(role, faction):
    explanations = {
        "Arsonist": "Neutral Killer. Douse players at night; ignite later to burn all doused.",
        "Jailor": "Town. Jail one player at night to interrogate or execute.",
        "Doctor": "Town. Heal players; can self-heal twice. Cannot prevent culting.",
        "Detective": "Town. Investigative role; check alignment.",
        "Godfather": "Mafia leader.",
        "Mafioso": "Mafia killer.",
        "Cult Leader": "Converts players to cult.",
        "Fanatic": "Oblivious until contacted.",
        "Jester": "Wins if lynched. Game continues.",
        "Executioner": "Has a target; wins if target is lynched. Game continues."
    }
    return explanations.get(role, f"{role} — {faction} role.")

# --- REST endpoints ---
class JoinRequest(BaseModel):
    roomId: str
    name: Optional[str] = "Player"

@app.get("/test")
def test():
    return {"message": "Hello from Town of Shadows backend!"}

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
    # return private role info
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

@app.post("/start-game/{rid}")
async def start_game(rid: str):
    room = rooms.get(rid)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["state"] == "active":
        return {"ok": True, "message": "Game already running"}
    room["state"] = "active"
    room["phase"] = "day"
    room["day"] = 1
    # send private roles to humans that are connected
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
    # start bot day actions asynchronously
    asyncio.create_task(perform_bot_day_actions(rid))
    return {"status":"started","room": room_summary(room)}

@app.post("/next-phase/{rid}")
async def next_phase(rid: str):
    room = rooms.get(rid)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["state"] != "active": raise HTTPException(status_code=400, detail="Game not active")
    room["phase"] = "night" if room["phase"] == "day" else "day"
    if room["phase"] == "day":
        room["day"] += 1
    await broadcast(rid, {"type":"system","text":f"Phase changed to {room['phase']} (Day {room['day']})"})
    await broadcast(rid, {"type":"room","room": room_summary(room)})
    # if it's night now, run bot night actions
    if room["phase"] == "night":
        asyncio.create_task(perform_bot_night_actions(rid))
    else:
        asyncio.create_task(perform_bot_day_actions(rid))
    return {"ok": True, "phase": room["phase"], "day": room["day"]}

# --- Core night/day resolution logic ---

async def perform_bot_day_actions(room_id: str):
    """Bots cast votes automatically during day."""
    room = rooms.get(room_id)
    if not room or room["phase"] != "day" or room["state"] != "active": return
    await broadcast(room_id, {"type":"system","text":"🤖 Bots are voting..."})
    alive_players = [p for p in room["players"] if p["alive"]]
    for bot in [p for p in alive_players if p["is_bot"]]:
        choices = [p["name"] for p in alive_players if p["name"] != bot["name"]]
        if not choices: continue
        tgt = sample(choices)
        room.setdefault("votes", {})[bot["name"]] = tgt
        await broadcast(room_id, {"type":"system","text":f"🤖 {bot['name']} voted for {tgt}"})
    await asyncio.sleep(1)
    # resolve votes if majority/all
    await resolve_votes_if_ready(room_id)

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
    if not tally:
        return
    top = max(tally, key=lambda k: tally[k])
    victim = next((p for p in room["players"] if p["name"]==top and p["alive"]), None)
    if victim:
        # mark dead and reveal (unless janitor cleaned later)
        victim["alive"] = False
        # apply jester/executioner checks for lynch
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
        # reveal unless janitor cleaned (janitor_clean is a night action; lynch cleaning rarely applicable)
        await reveal_death(room_id, victim)
    room["votes"] = {}
    room["phase"] = "night"
    await broadcast(room_id, {"type":"system","text":"🌙 Night begins..."})
    await broadcast(room_id, {"type":"room","room": room_summary(room)})
    await asyncio.sleep(1)
    asyncio.create_task(perform_bot_night_actions(room_id))

async def perform_bot_night_actions(room_id: str):
    """Bots perform night actions, then all actions are resolved."""
    room = rooms.get(room_id)
    if not room or room["phase"] != "night" or room["state"] != "active": return
    await broadcast(room_id, {"type":"system","text":"🤖 Bots are acting at night..."})
    alive = [p for p in room["players"] if p["alive"]]
    # mafia kill
    mafia = [p for p in alive if p["faction"] == "Mafia"]
    if mafia:
        targets = [p for p in alive if p["faction"] != "Mafia"]
        if targets:
            victim = sample(targets)
            room.setdefault("actions", []).append({"actor": sample(mafia)["name"], "target": victim["name"], "type": "mafia_kill"})
            await broadcast(room_id, {"type":"system","text":f"🤖 Mafia targeted {victim['name']}"})
    # cult convert attempts
    cults = [p for p in alive if p["faction"] == "Cult"]
    if cults:
        if random.random() < 0.3:
            candidates = [p for p in alive if p["faction"] not in ("Cult","Mafia")]
            if candidates:
                t = sample(candidates)
                room.setdefault("actions", []).append({"actor": sample(cults)["name"], "target": t["name"], "type": "cult_convert"})
                await broadcast(room_id, {"type":"system","text":f"🤖 Cult attempted to convert {t['name']}"})
    # arsonist douse/ignite behavior (if present) - not implemented in depth here
    await asyncio.sleep(1)
    # now resolve queued actions
    await apply_player_actions(room_id)
    await asyncio.sleep(1)
    await check_victory(room_id)

# --- action queuing & resolution ---

@app.post("/queue-action")
async def queue_action(body: dict = Body(...)):
    room_id = body.get("room_id")
    actor = body.get("actor")
    target = body.get("target")
    atype = body.get("type")
    room = rooms.get(room_id)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["phase"] != "night": raise HTTPException(status_code=400, detail="Actions only at night")
    room.setdefault("actions", []).append({"actor": actor, "target": target, "type": atype})
    await broadcast(room_id, {"type":"system","text":f"{actor} queued an action: {atype} -> {target}"})
    return {"ok": True}

async def apply_player_actions(room_id: str):
    """Process queued night actions considering protection, janitor, cupid, etc."""
    room = rooms.get(room_id)
    if not room: return
    actions = room.get("actions", [])
    protected = set()
    janitor_cleans = set()
    queued_kills = []
    queued_converts = []

    # collect actions
    for action in actions:
        actor_name = action.get("actor")
        target_name = action.get("target")
        atype = action.get("type", "")
        actor = next((p for p in room["players"] if p["name"]==actor_name), None)
        target = next((p for p in room["players"] if p["name"]==target_name), None)
        if not actor or not target: continue

        if atype == "doctor_heal":
            protected.add(target_name)
        elif atype == "bodyguard_protect":
            protected.add(target_name)
        elif atype == "janitor_clean":
            # can only clean if target is dead (we'll check later) - queue
            janitor_cleans.add(target_name)
        elif atype == "cupid_link":
            # cupid links two people: actor and target
            if actor and not actor.get("cupid_used"):
                room["lovers"][actor_name] = target_name
                room["lovers"][target_name] = actor_name
                actor["cupid_used"] = True
                await broadcast(room_id, {"type":"system","text":f"💞 {actor_name} and {target_name} are now linked by Cupid."})
        elif atype in ("mafia_kill","lynch_kill","serial_kill","beast_kill"):
            queued_kills.append({"victim": target_name, "by": actor_name, "type": atype})
        elif atype == "cult_convert":
            queued_converts.append({"target": target_name, "by": actor_name})
        elif atype == "executioner_set":
            room["execution_targets"][actor_name] = target_name
        # other custom actions could be added here

    # apply converts first (so conversions affect remaining logic)
    for c in queued_converts:
        target = next((p for p in room["players"] if p["name"]==c["target"] and p["alive"]), None)
        if target and target["role"] not in ("Godfather","Mafioso","Janitor","Beastman","Soldier"):
            target["faction"] = "Cult"
            target["role"] = "Acolyte"
            await broadcast(room_id, {"type":"system","text":f"✨ {target['name']} was converted to the Cult!"})

    # resolve kills with Cupid sacrifice and protections & janitor cleaning
    final_kill_names = []
    for k in queued_kills:
        victim = k["victim"]
        lover = room["lovers"].get(victim)
        if lover and any(p["name"]==lover and p["alive"] for p in room["players"]):
            # lover sacrifices themself instead
            final_kill_names.append(lover)
            await broadcast(room_id, {"type":"system","text":f"💔 {lover} sacrificed themself for their lover {victim}!"})
        else:
            final_kill_names.append(victim)

    applied_dead = set()
    for name in set(final_kill_names):
        if name in protected:
            await broadcast(room_id, {"type":"system","text":f"🛡️ {name} was attacked but survived (protected)."})
            continue
        victim = next((p for p in room["players"] if p["name"]==name and p["alive"]), None)
        if victim:
            victim["alive"] = False
            # reveal unless cleaned by janitor
            if name not in janitor_cleans:
                await reveal_death(room_id, victim)
            else:
                victim["revealed"] = False
                await broadcast(room_id, {"type":"system","text":f"⚠️ A corpse was found but it's been cleaned; role hidden."})
            applied_dead.add(name)

    # clear actions
    room["actions"] = []
    # broadcast updated room
    await broadcast(room_id, {"type":"room","room": room_summary(room)})

# reveal helper
async def reveal_death(room_id: str, player):
    if not player.get("revealed", False):
        player["revealed"] = True
    msg = f"💀 {player['name']} was the {player['role']} ({player['faction']})!"
    await broadcast(room_id, {"type":"system","text": msg})

# victory & end game
async def check_victory(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    alive = [p for p in room["players"] if p["alive"]]
    mafia_alive = [p for p in alive if p["faction"]=="Mafia"]
    cult_alive = [p for p in alive if p["faction"]=="Cult"]
    town_alive = [p for p in alive if p["faction"]=="Town"]
    neutral_alive = [p for p in alive if p["faction"]=="Neutral"]

    # Win checks
    if not mafia_alive and not cult_alive:
        await end_game(room_id, "Town"); return
    if not town_alive and len(mafia_alive) >= len(cult_alive):
        await end_game(room_id, "Mafia"); return
    if len(cult_alive) >= len(mafia_alive) + len(town_alive):
        await end_game(room_id, "Cult"); return
    if not any([mafia_alive, cult_alive, town_alive]) and neutral_alive:
        await end_game(room_id, "Neutral"); return

    # continue next day
    room["phase"] = "day"
    room["day"] += 1
    await broadcast(room_id, {"type":"system","text": f"🌞 Day {room['day']} begins!"})
    await broadcast(room_id, {"type":"room","room": room_summary(room)})
    # bots vote automatically
    asyncio.create_task(perform_bot_day_actions(room_id))

async def end_game(room_id: str, winner_faction: str):
    room = rooms.get(room_id)
    if not room: return
    room["state"] = "ended"
    room["winner"] = winner_faction
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
    # announce individual winners (jester/executioner)
    if room.get("winners"):
        await broadcast(room_id, {"type":"system","text": "🏆 Individual winners: " + ", ".join(room["winners"])})
    # reveal all roles
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
                # bind socket to slot name
                slot = msg.get("slot")
                p = next((pp for pp in room["players"] if pp["slot"]==slot), None)
                if p:
                    p["ws_id"] = wsid
                    p["is_bot"] = False
                    room["sockets"][wsid] = p["name"]
                    await websocket.send_text(json.dumps({"type":"system","text":f"You are {p['name']} (slot {p['slot']})"}))
                    if room["state"]=="active":
                        await websocket.send_text(json.dumps({"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"],"explain":role_explain(p["role"],p["faction"])}))
                    await broadcast(room_id, {"type":"room","room": room_summary(room)})
            elif mtype == "create_room":
                r = create_room_object(msg.get("name","Host"))
                await websocket.send_text(json.dumps({"type":"room_info","room": room_summary(r), "roomId": r["id"]}))
            elif mtype == "join_room":
                # join via ws - claim first bot slot
                slot = next((p for p in room["players"] if p["is_bot"]), None)
                if not slot:
                    await websocket.send_text(json.dumps({"type":"system","text":"Room full"})); continue
                slot["is_bot"]=False; slot["name"]=msg.get("name","Player"); slot["ws_id"]=wsid; room["sockets"][wsid]=slot["name"]
                await websocket.send_text(json.dumps({"type":"private_role","slot":slot["slot"],"role":slot["role"],"faction":slot["faction"],"explain":role_explain(slot["role"],slot["faction"])}))
                await broadcast(room_id, {"type":"system","text":f"{slot['name']} joined as {slot['slot']}"})
                await broadcast(room_id, {"type":"room","room": room_summary(room)})
            elif mtype == "start_game":
                try:
                    await start_game(room_id)
                except Exception as e:
                    await websocket.send_text(json.dumps({"type":"system","text":str(e)}))
            elif mtype == "player_action":
                # expected: { action: { actor, target, type } }
                action = msg.get("action")
                if action:
                    room.setdefault("actions", []).append(action)
                    await websocket.send_text(json.dumps({"type":"system","text":"Action queued"}))
            elif mtype == "vote":
                slot = msg.get("slot"); target = msg.get("target")
                # map slot -> player name
                p = next((pp for pp in room["players"] if pp["slot"]==slot), None)
                if p:
                    room.setdefault("votes", {})[p["name"]] = target
                    await broadcast(room_id, {"type":"system","text":f"{p['name']} voted for {target}"})
                    await resolve_votes_if_ready(room_id)
            elif mtype == "chat":
                await broadcast(room_id, {"type":"chat","from": msg.get("from"), "text": msg.get("text")})
            else:
                await websocket.send_text(json.dumps({"type":"system","text":"Unknown message type"}))
    except WebSocketDisconnect:
        s = room["sockets"].pop(wsid, None)
        room_ws[room_id].sockets.pop(wsid, None)
        if s:
            p = next((pp for pp in room["players"] if pp["name"]==s), None)
            if p: p["is_bot"]=True; p["ws_id"]=None
        await broadcast(room_id, {"type":"system","text":"A player disconnected"})
    except Exception as e:
        traceback.print_exc()
        try: await websocket.send_text(json.dumps({"type":"system","text":"Server error: "+str(e)}))
        except: pass
