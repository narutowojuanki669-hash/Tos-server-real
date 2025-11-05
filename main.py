# main.py
# Town of Shadows - Updated server (responsive grid + smart bots + night-first start)
# Deploy: uvicorn main:app --host 0.0.0.0 --port 10000

import asyncio, json, random, time, traceback
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Body
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, Optional
from uuid import uuid4

app = FastAPI(title="Town of Shadows - Updated Server")

FRONTEND_URL = "https://690a8382d00a2311478c4251--celebrated-bonbon-1fd3cf.netlify.app"
BACKEND_HOST = "https://town-of-shadows-server.onrender.com"

# Players / roles config (unchanged from final spec)
TOTAL_PLAYERS = 20
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
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ---------- Utilities ----------
def sample(a): return random.choice(a)
def shuffle(a): b=a[:]; random.shuffle(b); return b

def role_to_faction(role):
    for k,v in ROLE_POOL.items():
        if role in v:
            return "Town" if k=="town" else ("Mafia" if k=="mafia" else ("Cult" if k=="cult" else "Neutral"))
    return "Unknown"

def build_full_roles():
    # similar to before: generate pools by faction counts
    mafia = ["Godfather","Mafioso"]
    # fill mafia
    other_mafia = [r for r in ROLE_POOL["mafia"] if r not in mafia]
    while len(mafia) < FACTION_COUNTS["mafia"]:
        mafia.append(sample(other_mafia) if random.random() < 0.4 else "Mafioso")
    cult = ["Cult Leader","Fanatic"]
    other_cult = [r for r in ROLE_POOL["cult"] if r not in cult]
    while len(cult) < FACTION_COUNTS["cult"]:
        cult.append(sample(other_cult))
    town = shuffle(ROLE_POOL["town"])[:FACTION_COUNTS["town"]]
    neutrals = shuffle(ROLE_POOL["neutrals"])[:FACTION_COUNTS["neutrals"]]
    all_roles = town + mafia + cult + neutrals
    while len(all_roles) < TOTAL_PLAYERS:
        all_roles.append(sample(ROLE_POOL["town"]))
    random.shuffle(all_roles)
    return all_roles

# ---------- In-memory state ----------
rooms: Dict[str, Any] = {}
room_ws: Dict[str, Any] = {}

class RoomWebsockets:
    def __init__(self):
        self.sockets = {}  # wsid -> WebSocket
    async def send(self, msg: dict):
        dead = []
        for wsid, ws in list(self.sockets.items()):
            try:
                await ws.send_text(json.dumps(msg))
            except Exception:
                dead.append(wsid)
        for d in dead:
            self.sockets.pop(d, None)

async def broadcast(room_id: str, message: dict):
    mgr = room_ws.get(room_id)
    if mgr:
        await mgr.send(message)

# ---------- Room creation ----------
def create_room_object(host_name="Host"):
    room_id = str(uuid4())[:6].upper()
    roles = build_full_roles()
    players=[]
    for i in range(1, TOTAL_PLAYERS+1):
        r = roles[i-1]
        players.append({
            "slot": i,
            "name": f"Player {i}",
            "role": r,
            "faction": role_to_faction(r),
            "is_bot": True,
            "alive": True,
            "revealed": False,
            "ws_id": None,
            "cupid_used": False,
            "jailor_has_execute": True if r=="Jailor" else False,
            "vigilante_last_shot_day": -99
        })
    room = {
        "id": room_id, "host": host_name, "players": players,
        "state":"waiting", "phase":"waiting", "day":0,
        "actions":[], "votes":{}, "sockets":{},
        "lovers":{}, "winners":[], "execution_targets":{},
        "controller_task": None
    }
    # set executioner targets
    for p in players:
        if p["role"] == "Executioner":
            choices = [x["name"] for x in players if x["name"] != p["name"]]
            if choices: room["execution_targets"][p["name"]] = sample(choices)
    rooms[room_id] = room
    room_ws[room_id] = RoomWebsockets()
    return room

def room_summary(room):
    return {
        "id": room["id"], "host": room["host"], "state": room["state"],
        "phase": room["phase"], "day": room["day"],
        "players":[{"slot":p["slot"], "name":p["name"], "alive":p["alive"], "revealed":p["revealed"], "symbol": p["role"] if p["revealed"] else ("?" if p["alive"] else None), "is_bot":p["is_bot"]} for p in room["players"]]
    }

def role_explain(role, faction):
    base = {
        "Doctor":"Heals one player each night; prevents kills except Beastman bypass.",
        "Vigilante":"Can shoot every 2 nights. Shots only kill Mafia (including culted-mafia).",
        "Jailor":"Jails a player at night; can execute once (loses execute if wrongly used).",
        "Medium":"Can speak to dead players during night."
    }
    return base.get(role, f"{role} — {faction}")

# ---------- REST endpoints ----------
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
    return {"slot": slot["slot"], "role": slot["role"], "faction": slot["faction"], "explain": role_explain(slot["role"], slot["faction"]), "room": room_summary(room)}

@app.get("/room/{room_id}")
def get_room(room_id: str):
    room = rooms.get(room_id)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    return room_summary(room)

@app.post("/player-action")
def player_action(req: ActionRequest):
    # kept for backward compatibility; prefer WS queue-action
    room = rooms.get(req.roomId)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["phase"] != "night": raise HTTPException(status_code=400, detail="Actions only at night")
    act = req.action; act["by"] = req.slot
    room.setdefault("actions", []).append(act)
    return {"status":"queued","action":act}

# ---------- Start game: start at NIGHT first ----------
@app.post("/start-game/{rid}")
async def start_game(rid: str):
    room = rooms.get(rid)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["state"] == "active":
        return {"ok": True, "message":"Game already running"}
    room["state"] = "active"
    room["day"] = 0
    room["phase"] = "night"   # start with night
    room["actions"] = []
    # send private roles to connected humans immediately
    for p in room["players"]:
        if not p["is_bot"] and p.get("ws_id"):
            ws = room_ws[rid].sockets.get(p["ws_id"])
            if ws:
                try:
                    await ws.send_text(json.dumps({"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"],"explain":role_explain(p["role"],p["faction"])}))
                except: pass
    await broadcast(rid, {"type":"system","text":"Game started. Night 1 begins."})
    await broadcast(rid, {"type":"room","room": room_summary(room)})
    room["controller_task"] = asyncio.create_task(phase_controller_loop(rid))
    return {"status":"started","room": room_summary(room)}

@app.post("/next-phase/{rid}")
async def next_phase(rid: str):
    # admin/test convenience
    room = rooms.get(rid)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["state"] != "active": raise HTTPException(status_code=400, detail="Game not active")
    if room["phase"] == "night":
        room["phase"] = "day"; room["day"] += 1
    else:
        room["phase"] = "night"
    await broadcast(rid, {"type":"system","text":f"Phase changed to {room['phase']} (day {room['day']})"})
    await broadcast(rid, {"type":"room","room": room_summary(room)})
    return {"ok": True}

# ---------- Phase controller (same segmentation) ----------
async def phase_controller_loop(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    try:
        while room["state"] == "active":
            if room["phase"] == "night":
                # night phase
                await broadcast(room_id, {"type":"system","text":"🌙 Night begins. Use your night abilities."})
                await broadcast(room_id, {"type":"phase","phase":"night","seconds": NIGHT_DURATION, "allow": {"mafia":True,"cult":True,"medium":True,"lovers":True,"jailor_chat":True}})
                # bots act at mid-night
                asyncio.create_task(simulate_bot_night_actions(room_id))
                await countdown_broadcast(room_id, NIGHT_DURATION)
                await apply_player_actions(room_id)
                await check_victory(room_id)
                if room.get("state") == "ended": break
                # move to day 1 (or next day)
                room["phase"] = "day"; room["day"] += 1
                await broadcast(room_id, {"type":"system","text":f"🌞 Day {room['day']} begins (Discussion)."})
            else:
                # day segmentation: discussion -> voting -> defence -> final -> resolve -> back to night
                room["phase"] = "day"
                await broadcast(room_id, {"type":"phase","phase":"discussion","seconds": DAY_DISCUSS})
                await asyncio.create_task(simulate_bot_discussion_and_votes(room_id))
                await countdown_broadcast(room_id, DAY_DISCUSS)
                await broadcast(room_id, {"type":"phase","phase":"voting","seconds": DAY_VOTE})
                await asyncio.create_task(simulate_bot_votes(room_id))
                await countdown_broadcast(room_id, DAY_VOTE)
                await broadcast(room_id, {"type":"phase","phase":"defence","seconds": DAY_DEFENCE})
                await countdown_broadcast(room_id, DAY_DEFENCE)
                await broadcast(room_id, {"type":"phase","phase":"final","seconds": DAY_FINAL})
                await countdown_broadcast(room_id, DAY_FINAL)
                # resolve votes
                await resolve_votes_if_ready(room_id)
                # go to night
                room["phase"] = "night"
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

# ---------- Bot behavior - smarter voting ----------
async def simulate_bot_discussion_and_votes(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    await asyncio.sleep(max(1, DAY_DISCUSS//2))
    alive = [p for p in room["players"] if p["alive"]]
    for bot in [p for p in alive if p["is_bot"]]:
        if random.random() < 0.25:
            choices = [x["name"] for x in alive if x["name"] != bot["name"]]
            if choices:
                target = sample(choices)
                room.setdefault("votes", {})[bot["name"]] = target
                await broadcast(room_id, {"type":"system","text":f"🤖 {bot['name']} is leaning towards {target}."})

async def simulate_bot_votes(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    await asyncio.sleep(max(1, DAY_VOTE//2))
    alive = [p for p in room["players"] if p["alive"]]
    # build suspiciousness weights: non-bots prefer to vote players with different faction
    for bot in [p for p in alive if p["is_bot"]]:
        # bots often abstain
        if random.random() < 0.6:
            continue
        # pick weighted target: prefer suspicious factions
        weights = []
        for cand in alive:
            if cand["name"] == bot["name"]: continue
            weight = 1.0
            # if candidate is mafia/cult, heavier
            if cand["faction"] in ("Mafia","Cult"): weight = 3.0
            # slight randomness
            weight *= (0.7 + random.random()*0.8)
            weights.append((cand["name"], weight))
        if not weights: continue
        total = sum(w for _,w in weights)
        r = random.random()*total
        upto = 0
        chosen = weights[-1][0]
        for nm,w in weights:
            upto += w
            if r <= upto:
                chosen = nm
                break
        room.setdefault("votes", {})[bot["name"]] = chosen
        await broadcast(room_id, {"type":"system","text":f"🤖 {bot['name']} voted for {chosen} (smart)"})


async def simulate_bot_night_actions(room_id: str):
    room = rooms.get(room_id)
    if not room or room["state"] != "active": return
    await asyncio.sleep(max(1, NIGHT_DURATION//3))
    alive = [p for p in room["players"] if p["alive"]]
    # mafia kill
    mafia = [p for p in alive if p["faction"]=="Mafia"]
    if mafia:
        targets = [p for p in alive if p["faction"]!="Mafia"]
        if targets:
            victim = sample(targets)
            room.setdefault("actions", []).append({"actor": sample(mafia)["name"], "target": victim["name"], "type":"mafia_kill", "actor_role":"Mafioso"})
            await broadcast(room_id, {"type":"system","text":f"🤖 Mafia targeted {victim['name']}"})
    # cult conversion
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

# ---------- Voting resolution ----------
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
        await broadcast(room_id, {"type":"system","text":"No consensus — no lynch."})
        return
    top = max(tally, key=lambda k: tally[k])
    victim = next((p for p in room["players"] if p["name"]==top and p["alive"]), None)
    if victim:
        victim["alive"] = False
        await broadcast(room_id, {"type":"system","text":f"⚖️ {victim['name']} was lynched!"})
        # jester/executioner handled
        if victim["role"] == "Jester":
            if victim["name"] not in room["winners"]:
                room["winners"].append(victim["name"])
                await broadcast(room_id, {"type":"system","text":f"😈 {victim['name']} (Jester) achieved their win!"})
        for execn, targ in list(room.get("execution_targets", {}).items()):
            if targ == victim["name"]:
                if execn not in room["winners"]:
                    room["winners"].append(execn)
                    await broadcast(room_id, {"type":"system","text":f"🔨 {execn} (Executioner) achieved their win!"})
        await reveal_death(room_id, victim)

# ---------- Queue actions endpoint ----------
@app.post("/queue-action")
async def queue_action(body: dict = Body(...)):
    room_id = body.get("room_id"); actor = body.get("actor"); target = body.get("target"); atype = body.get("type")
    room = rooms.get(room_id)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    if room["phase"] != "night": raise HTTPException(status_code=400, detail="Actions only at night")
    room.setdefault("actions", []).append({"actor": actor, "target": target, "type": atype})
    await broadcast(room_id, {"type":"system","text":f"{actor} queued action {atype} -> {target}"})
    return {"ok": True}

# ---------- Action resolution (doctor protection + beastman bypass + cupid + janitor + jail) ----------
async def apply_player_actions(room_id: str):
    room = rooms.get(room_id)
    if not room: return
    actions = room.get("actions", [])
    protected_by_doctor = set()
    protected_by_bodyguard = set()
    janitor_cleans = set()
    queued_kills = []
    queued_converts = []
    queued_jails = []
    queued_investigations = []

    for action in actions:
        actor = action.get("actor"); target = action.get("target"); atype = action.get("type",""); actor_role = action.get("actor_role")
        actor_p = next((p for p in room["players"] if p["name"]==actor), None)
        target_p = next((p for p in room["players"] if p["name"]==target), None)
        if not actor_p: continue
        if atype == "doctor_heal":
            protected_by_doctor.add(target)
            await broadcast(room_id, {"type":"system","text":f"🩺 {actor} healed {target} (doctor)."})
        elif atype == "bodyguard_protect":
            protected_by_bodyguard.add(target)
            await broadcast(room_id, {"type":"system","text":f"🛡️ {actor} protects {target}."})
        elif atype == "janitor_clean":
            janitor_cleans.add(target)
            await broadcast(room_id, {"type":"system","text":f"🧹 {actor} will clean {target}'s body."})
        elif atype == "cupid_link":
            if not actor_p.get("cupid_used"):
                room["lovers"][actor] = target; room["lovers"][target] = actor; actor_p["cupid_used"] = True
                await broadcast(room_id, {"type":"system","text":f"💞 Cupid linked {actor} and {target}."})
        elif atype in ("mafia_kill","beast_kill","serial_kill","vigilante_shot"):
            queued_kills.append({"victim": target, "by": actor, "type": atype, "actor_role": actor_p.get("role")})
        elif atype == "cult_convert":
            queued_converts.append({"target": target, "by": actor})
        elif atype == "jail":
            queued_jails.append({"jailor": actor, "target": target, "execute": False})
        elif atype == "jail_execute":
            queued_jails.append({"jailor": actor, "target": target, "execute": True})
        elif atype == "detective_check":
            queued_investigations.append({"investigator": actor, "target": target})
        # additional actions not exhaustively listed here

    # converts first
    for c in queued_converts:
        t = next((p for p in room["players"] if p["name"]==c["target"] and p["alive"]), None)
        if t and t["role"] not in ("Godfather","Mafioso","Beastman","Soldier"):
            t["faction"] = "Cult"; t["role"] = "Acolyte"
            await broadcast(room_id, {"type":"system","text":f"✨ {t['name']} was converted to the Cult!"})

    # jails (protect from being targeted)
    jailed = set()
    for j in queued_jails:
        jailor = j["jailor"]; tgt = j["target"]; exec_flag = j["execute"]
        jailed.add(tgt)
        jailor_p = next((p for p in room["players"] if p["name"]==jailor), None)
        tgt_p = next((p for p in room["players"] if p["name"]==tgt and p["alive"]), None)
        if exec_flag and jailor_p and jailor_p.get("jailor_has_execute", True):
            if tgt_p:
                tgt_p["alive"] = False
                await broadcast(room_id, {"type":"system","text":f"⚖️ {tgt_p['name']} was executed by Jailor {jailor}."})
                await reveal_death(room_id, tgt_p)
            if tgt_p and tgt_p["faction"] == "Town":
                jailor_p["jailor_has_execute"] = False
                await broadcast(room_id, {"type":"system","text":f"⚠️ {jailor} executed a Town member and lost execution ability."})

    # apply kills (Cupid sacrifice and doctor protections)
    final_kills = []
    for k in queued_kills:
        v = k["victim"]
        if v in jailed:
            await broadcast(room_id, {"type":"system","text":f"🔒 {v} was jailed and could not be targeted."})
            continue
        lover = room["lovers"].get(v)
        if lover and any(p["name"]==lover and p["alive"] for p in room["players"]):
            final_kills.append({"name":lover, "actor_role":k.get("actor_role"), "type":k["type"]})
            await broadcast(room_id, {"type":"system","text":f"💔 {lover} sacrificed themselves for their lover {v}!"})
        else:
            final_kills.append({"name":v, "actor_role":k.get("actor_role"), "type":k["type"]})

    applied = set()
    for ent in final_kills:
        name = ent["name"]; actor_role = ent.get("actor_role",""); ktype = ent.get("type","")
        bypass = (actor_role == "Beastman") or (ktype in ("beast_kill","beastman_kill"))
        if (name in protected_by_doctor or name in protected_by_bodyguard) and not bypass:
            await broadcast(room_id, {"type":"system","text":f"🛡️ {name} was attacked but survived (protected)."})
            continue
        victim = next((p for p in room["players"] if p["name"]==name and p["alive"]), None)
        if victim:
            victim["alive"] = False
            if name not in janitor_cleans:
                await reveal_death(room_id, victim)
            else:
                victim["revealed"] = False
                await broadcast(room_id, {"type":"system","text":f"⚠️ A corpse was cleaned; role hidden."})
            applied.add(name)

    room["actions"] = []
    await broadcast(room_id, {"type":"room","room": room_summary(room)})

# ---------- reveal helper ----------
async def reveal_death(room_id: str, player):
    if not player.get("revealed", False):
        player["revealed"] = True
    await broadcast(room_id, {"type":"system","text":f"💀 {player['name']} was the {player['role']} ({player['faction']})!"})

# ---------- victory ----------
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
        msg = "🌼 Town wins!"
    elif winner_faction == "Mafia":
        msg = "💀 Mafia wins!"
    elif winner_faction == "Cult":
        msg = "🔮 Cult wins!"
    else:
        msg = "⚖️ Neutrals win!"
    await broadcast(room_id, {"type":"system","text": msg})
    await asyncio.sleep(1)
    if room.get("winners"):
        await broadcast(room_id, {"type":"system","text": "🏆 Individual winners: " + ", ".join(room["winners"])})
    recap = "\n".join([f"{p['name']}: {p['role']} ({p['faction']}) {'✅' if p['alive'] else '💀'}" for p in room["players"]])
    await broadcast(room_id, {"type":"system","text": "📜 Final Roles:\n" + recap})
    await broadcast(room_id, {"type":"room","room": room_summary(room)})

# ---------- WebSocket endpoint ----------
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    wsid = str(uuid4())
    room = rooms.get(room_id)
    if not room:
        await websocket.send_text(json.dumps({"type":"system","text":"Room not found"})); await websocket.close(); return
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
            elif mtype == "join_room":
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
                channel = msg.get("channel","public"); text = msg.get("text",""); sender = msg.get("from","")
                # broadcast respecting channels (same as previous)
                if channel == "public":
                    await broadcast(room_id, {"type":"chat","from":sender,"text":text,"channel":"public"})
                elif channel == "mafia":
                    for p in room["players"]:
                        if p["faction"]=="Mafia" and p.get("ws_id"):
                            ws = room_ws[room_id].sockets.get(p["ws_id"])
                            if ws:
                                try: await ws.send_text(json.dumps({"type":"chat","from":sender,"text":text,"channel":"mafia"}))
                                except: pass
                elif channel == "cult":
                    for p in room["players"]:
                        if p["faction"]=="Cult" and p.get("ws_id"):
                            ws = room_ws[room_id].sockets.get(p["ws_id"])
                            if ws:
                                try: await ws.send_text(json.dumps({"type":"chat","from":sender,"text":text,"channel":"cult"}))
                                except: pass
                elif channel == "lovers":
                    partner = room["lovers"].get(sender)
                    for nm in (sender, partner):
                        p = next((pp for pp in room["players"] if pp["name"]==nm and pp.get("ws_id")), None)
                        if p:
                            ws = room_ws[room_id].sockets.get(p["ws_id"])
                            if ws:
                                try: await ws.send_text(json.dumps({"type":"chat","from":sender,"text":text,"channel":"lovers"}))
                                except: pass
                elif channel == "medium":
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
            if p: p["is_bot"] = True; p["ws_id"] = None
        await broadcast(room_id, {"type":"system","text":"A player disconnected"})
    except Exception:
        traceback.print_exc()
        try: await websocket.send_text(json.dumps({"type":"system","text":"Server error"}))
        except: pass

# ---------- minimal missing references ----------
class ActionRequest(BaseModel):
    roomId: str
    slot: int
    action: dict

# (end)
