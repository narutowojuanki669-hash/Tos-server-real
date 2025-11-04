from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, Optional
from uuid import uuid4
import asyncio, json, random, traceback

app = FastAPI(title="Town of Shadows – Final Server")

# ✅ Updated frontend domain
FRONTEND_ORIGIN = "https://690a8382d00a2311478c4251--celebrated-bonbon-1fd3cf.netlify.app"

TOTAL_PLAYERS = 20
FACTION_COUNTS = {"town": 8, "mafia": 5, "cult": 4, "neutrals": 3}

ROLE_POOL = {
    "town": ["Detective","Sheriff","Investigator","Lookout","Tracker","Bodyguard",
             "Doctor","Jailor","Cupid","Mayor","Vigilante","Escort","Medium","Soldier","Gossip"],
    "mafia": ["Godfather","Mafioso","Janitor","Spy","Beastman","Consort","Blackmailer","Framer","Disguiser","Forger"],
    "cult": ["Cult Leader","Fanatic","Infiltrator","Prophet","High Priest","Acolyte"],
    "neutrals": ["Jester","Executioner","Serial Killer","Arsonist","Survivor","Amnesiac","Witch","Guardian Angel"]
}

# ✅ CORS setup for new Netlify domain
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Utility helpers ---
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
    while len(roles)<FACTION_COUNTS["cult"]:
        roles.append(sample(pool))
    random.shuffle(roles); return roles

def build_town_roles(): return shuffle(ROLE_POOL["town"])[:FACTION_COUNTS["town"]]
def build_neutrals(): return shuffle(ROLE_POOL["neutrals"])[:FACTION_COUNTS["neutrals"]]

def build_full_roles():
    mafia, cult, town, neutrals = build_mafia_roles(), build_cult_roles(), build_town_roles(), build_neutrals()
    all_roles = town + mafia + cult + neutrals
    while len(all_roles) < TOTAL_PLAYERS: all_roles.append(sample(ROLE_POOL["town"]))
    random.shuffle(all_roles); return all_roles

def role_to_faction(role):
    if role in ROLE_POOL["town"]: return "Town"
    if role in ROLE_POOL["mafia"]: return "Mafia"
    if role in ROLE_POOL["cult"]: return "Cult"
    if role in ROLE_POOL["neutrals"]: return "Neutral"
    return "Unknown"

rooms: Dict[str, Any] = {}
room_ws: Dict[str, Any] = {}

class RoomWebsockets:
    def __init__(self): self.sockets={}
    async def send(self, msg):
        dead=[]
        for ws_id,ws in list(self.sockets.items()):
            try: await ws.send_text(json.dumps(msg))
            except: dead.append(ws_id)
        for d in dead: self.sockets.pop(d,None)

async def broadcast(room_id, msg):
    mgr=room_ws.get(room_id)
    if mgr: await mgr.send(msg)

def create_room(host="Host"):
    rid=str(uuid4())[:6].upper()
    roles=build_full_roles()
    players=[{
        "slot":i,"name":f"Player {i}","role":roles[i-1],
        "faction":role_to_faction(roles[i-1]),"is_bot":True,
        "alive":True,"revealed":False,"ws_id":None
    } for i in range(1,TOTAL_PLAYERS+1)]
    room={"id":rid,"host":host,"players":players,"state":"waiting",
          "phase":"day","day":1,"actions":[],"votes":{},"sockets":{}}
    rooms[rid]=room; room_ws[rid]=RoomWebsockets(); return room

def summary(room):
    return {
        "id":room["id"],"host":room["host"],"state":room["state"],
        "phase":room["phase"],"day":room["day"],
        "players":[{"slot":p["slot"],"name":p["name"],
                    "alive":p["alive"],"revealed":p["revealed"],
                    "symbol":p["role"] if p["revealed"] else ("?" if p["alive"] else None),
                    "is_bot":p["is_bot"]} for p in room["players"]]
    }

@app.get("/test")
def test(): return {"message":"Hello from Town of Shadows backend!"}

@app.post("/create-room")
def create_room_ep(name:str="Host"):
    r=create_room(name); return {"roomId":r["id"],"room":summary(r)}

class JoinReq(BaseModel):
    roomId:str; name:Optional[str]="Player"

@app.post("/join-room")
def join_room(req:JoinReq):
    r=rooms.get(req.roomId)
    if not r: raise HTTPException(404,"Room not found")
    slot=next((p for p in r["players"] if p["is_bot"]),None)
    if not slot: raise HTTPException(400,"Room full")
    slot["is_bot"]=False; slot["name"]=req.name
    return {"slot":slot["slot"],"role":slot["role"],"faction":slot["faction"],"room":summary(r)}

@app.get("/room/{rid}")
def get_room(rid:str):
    r=rooms.get(rid)
    if not r: raise HTTPException(404,"Room not found")
    return summary(r)

@app.websocket("/ws/{rid}")
async def ws_room(ws:WebSocket,rid:str):
    await ws.accept(); wsid=str(uuid4())
    r=rooms.get(rid)
    if not r: await ws.send_text(json.dumps({"type":"system","text":"Room not found"})); return
    r["sockets"][wsid]=None; room_ws[rid].sockets[wsid]=ws
    await ws.send_text(json.dumps({"type":"system","text":f"Connected to {rid}","ws_id":wsid}))
    try:
        while True:
            msg=json.loads(await ws.receive_text())
            t=msg.get("type")
            if t=="join":
                slot=next((p for p in r["players"] if p["is_bot"]),None)
                if not slot: await ws.send_text(json.dumps({"type":"system","text":"Room full"})); continue
                slot["is_bot"]=False; slot["name"]=msg.get("name","Player"); slot["ws_id"]=wsid; r["sockets"][wsid]=slot["slot"]
                await broadcast(rid,{"type":"system","text":f"{slot['name']} joined"})
                await broadcast(rid,{"type":"room","room":summary(r)})
            elif t=="chat":
                await broadcast(rid,{"type":"chat","from":msg.get("from"),"text":msg.get("text")})
    except WebSocketDisconnect:
        s=r["sockets"].pop(wsid,None); room_ws[rid].sockets.pop(wsid,None)
        if s: p=next((x for x in r["players"] if x["slot"]==s),None)
        if p: p["is_bot"]=True; p["ws_id"]=None
        await broadcast(rid,{"type":"system","text":"A player disconnected"})
    except Exception as e:
        traceback.print_exc()
        try: await ws.send_text(json.dumps({"type":"system","text":f"Server error {e}"}))
        except: pass
# --- Game Flow Extensions ---

@app.post("/start-game/{rid}")
async def start_game(rid: str):
    room = rooms.get(rid)
    if not room:
        raise HTTPException(404, "Room not found")
    if room["state"] == "active":
        return {"ok": True, "message": "Game already running"}
    room["state"] = "active"
    room["phase"] = "day"
    room["day"] = 1
    await broadcast(rid, {"type": "system", "text": "🌞 The game has started!"})
    await broadcast(rid, {"type": "room", "room": summary(room)})
    asyncio.create_task(perform_bot_day_actions(rid))
    return {"ok": True, "message": "Game started"}

@app.post("/next-phase/{rid}")
def next_phase(rid: str):
    room = rooms.get(rid)
    if not room:
        raise HTTPException(404, "Room not found")
    if room["state"] != "active":
        raise HTTPException(400, "Game not active")
    room["phase"] = "night" if room["phase"] == "day" else "day"
    if room["phase"] == "day":
        room["day"] += 1
    asyncio.create_task(broadcast(rid, {
        "type": "system",
        "text": f"🔄 Phase changed to {room['phase']} (Day {room['day']})"
    }))
    asyncio.create_task(broadcast(rid, {"type": "room", "room": summary(room)}))
    return {"ok": True, "phase": room["phase"], "day": room["day"]}
# --- Voting and Night Actions ---

@app.post("/vote")
async def vote(room_id: str, voter: str, target: str):
    """Registers a daytime vote."""
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "Room not found")
    if room["phase"] != "day":
        raise HTTPException(400, "Voting only during the day")

    room["votes"][voter] = target
    await broadcast(room_id, {"type": "system", "text": f"{voter} voted for {target}"})

    # Check if all living players have voted
    alive = [p for p in room["players"] if p["alive"]]
    if len(room["votes"]) >= len(alive):
        # Count votes
        tally = {}
        for t in room["votes"].values():
            tally[t] = tally.get(t, 0) + 1
        top = max(tally, key=tally.get)
        # Eliminate top voted player
        target_player = next((p for p in alive if p["name"] == top), None)
        if target_player:
            target_player["alive"] = False
            target_player["revealed"] = True
            await broadcast(room_id, {"type": "system", "text": f"⚖️ {top} was voted out!"})
            await broadcast(room_id, {"type": "room", "room": summary(room)})

        room["votes"] = {}
        room["phase"] = "night"
        await broadcast(room_id, {"type": "system", "text": "🌙 Night begins..."})

    return {"ok": True, "votes": room["votes"]}


@app.post("/night-action")
async def night_action(room_id: str, actor: str, target: str):
    """Handles simple night actions like mafia kills or doctor saves."""
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "Room not found")
    if room["phase"] != "night":
        raise HTTPException(400, "Night actions only allowed at night")

    actor_player = next((p for p in room["players"] if p["name"] == actor), None)
    target_player = next((p for p in room["players"] if p["name"] == target), None)
    if not actor_player or not target_player:
        raise HTTPException(400, "Invalid actor or target")

    # Basic rule example:
    if actor_player["faction"] == "Mafia" and actor_player["alive"]:
        target_player["alive"] = False
        target_player["revealed"] = True
        await broadcast(room_id, {"type": "system", "text": f"💀 {target} was killed overnight!"})
        await broadcast(room_id, {"type": "room", "room": summary(room)})

    # For now, other roles just log their action
    await broadcast(room_id, {"type": "system", "text": f"{actor} performed an action on {target}"})
    return {"ok": True}
    import random

async def perform_bot_day_actions(room_id: str):
    """Bots automatically vote during the day."""
    room = rooms.get(room_id)
    if not room or room["phase"] != "day":
        return

    alive_players = [p for p in room["players"] if p["alive"]]
    for bot in [p for p in alive_players if p["is_bot"]]:
        targets = [p["name"] for p in alive_players if p["name"] != bot["name"]]
        if not targets:
            continue
        target = random.choice(targets)
        room["votes"][bot["name"]] = target
        await broadcast(room_id, {"type": "system", "text": f"🤖 {bot['name']} voted for {target}"})

    # Check if all living players have voted
    alive = [p for p in room["players"] if p["alive"]]
    if len(room["votes"]) >= len(alive):
        await resolve_votes(room_id)


async def resolve_votes(room_id: str):
    """Resolves daytime votes."""
    room = rooms.get(room_id)
    if not room:
        return

    tally = {}
    for t in room["votes"].values():
        tally[t] = tally.get(t, 0) + 1

    if not tally:
        return

    top = max(tally, key=tally.get)
    target_player = next((p for p in room["players"] if p["name"] == top and p["alive"]), None)
    if target_player:
        target_player["alive"] = False
        target_player["revealed"] = True
        await broadcast(room_id, {"type": "system", "text": f"⚖️ {top} was voted out!"})
        await broadcast(room_id, {"type": "room", "room": summary(room)})

    room["votes"].clear()
    room["phase"] = "night"
    await broadcast(room_id, {"type": "system", "text": "🌙 Night begins..."})
    await asyncio.sleep(3)
    await perform_bot_night_actions(room_id)

@app.post("/queue-action")
async def queue_action(room_id: str, actor: str, target: str):
    """Queue an action for later resolution."""
    room = rooms.get(room_id)
    if not room:
        raise HTTPException(404, "Room not found")
    if room["phase"] != "night":
        raise HTTPException(400, "Actions only at night")

    if "actions" not in room:
        room["actions"] = []

    room["actions"].append({"actor": actor, "target": target})
    await broadcast(room_id, {"type": "system", "text": f"{actor} has selected a target..."})
    return {"ok": True}

async def perform_bot_night_actions(room_id: str):
    """Bots automatically perform night actions."""
    room = rooms.get(room_id)
    if not room or room["phase"] != "night":
        return

    alive_players = [p for p in room["players"] if p["alive"]]
    mafia = [p for p in alive_players if p["faction"] == "Mafia"]
    cult = [p for p in alive_players if p["faction"] == "Cult"]

    # Mafia kill
    if mafia:
        victim = random.choice([p for p in alive_players if p["faction"] != "Mafia"])
        victim["alive"] = False
        victim["revealed"] = True
        await broadcast(room_id, {"type": "system", "text": f"💀 {victim['name']} was killed overnight!"})

    # Cult recruit
    if cult:
        candidates = [p for p in alive_players if p["faction"] not in ["Cult", "Mafia"]]
        if candidates:
            recruit = random.choice(candidates)
            recruit["faction"] = "Cult"
            await broadcast(room_id, {"type": "system", "text": f"✨ {recruit['name']} has been converted to the Cult!"})

    await broadcast(room_id, {"type": "room", "room": summary(room)})

    await apply_player_actions(room_id)
    await asyncio.sleep(3)
    await check_victory(room_id)

async def apply_player_actions(room_id: str):
    """Processes queued night actions (from humans or bots)."""
    room = rooms.get(room_id)
    if not room:
        return

    actions = room.get("actions", [])
    protected = set()
    killed = []
    converted = []

    for action in actions:
        actor = next((p for p in room["players"] if p["name"] == action["actor"]), None)
        target = next((p for p in room["players"] if p["name"] == action["target"]), None)
        if not actor or not target or not actor["alive"]:
            continue

        role = actor.get("role")
        # --- Town Roles ---
        if role == "Doctor":
            protected.add(target["name"])
        elif role == "Detective":
            info = "Mafia" if target["faction"] == "Mafia" else "Not Mafia"
            await broadcast(room_id, {"type": "private", "to": actor["name"], "text": f"🕵️ Your target {target['name']} is {info}."})
        elif role == "Bodyguard":
            if target["name"] in killed:
                killed.remove(target["name"])
                actor["alive"] = False
                actor["revealed"] = True
                await broadcast(room_id, {"type": "system", "text": f"🛡️ {actor['name']} died protecting {target['name']}!"})

        # --- Mafia Roles ---
        elif role == "Mafioso" and target["name"] not in protected:
            killed.append(target["name"])
        elif role == "Beastman" and target["name"] not in protected:
            killed.append(target["name"])

        # --- Cult Roles ---
        elif role == "Cult Leader" and target["faction"] not in ["Mafia", "Cult"]:
            target["faction"] = "Cult"
            converted.append(target["name"])

        # --- Neutral Roles (example Serial Killer) ---
        elif role == "Serial Killer" and target["name"] not in protected:
            killed.append(target["name"])

    # Apply results
    for k in set(killed):
        victim = next((p for p in room["players"] if p["name"] == k), None)
        if victim and victim["alive"]:
            victim["alive"] = False
            victim["revealed"] = True
            await broadcast(room_id, {"type": "system", "text": f"💀 {k} was found dead!"})

    for c in converted:
        await broadcast(room_id, {"type": "system", "text": f"✨ {c} has joined the Cult!"})

    room["actions"] = []
    await broadcast(room_id, {"type": "room", "room": summary(room)})
    
    async def reveal_death(room_id: str, player):
    """Reveals a player's death with role and faction info."""
    if not player["revealed"]:
        player["revealed"] = True
        msg = f"💀 {player['name']} was the {player['role']} ({player['faction']})!"
        await broadcast(room_id, {"type": "system", "text": msg})


async def check_victory(room_id: str):
    """Checks win conditions and moves to next day."""
    room = rooms.get(room_id)
    if not room:
        return

    alive = [p for p in room["players"] if p["alive"]]
    mafia_alive = [p for p in alive if p["faction"] == "Mafia"]
    cult_alive = [p for p in alive if p["faction"] == "Cult"]
    town_alive = [p for p in alive if p["faction"] == "Town"]

    if not mafia_alive and not cult_alive:
        await broadcast(room_id, {"type": "system", "text": "🌼 Town has won the game!"})
        room["state"] = "ended"
        return
    if not town_alive and len(mafia_alive) > len(cult_alive):
        await broadcast(room_id, {"type": "system", "text": "💀 Mafia has taken over!"})
        room["state"] = "ended"
        return
    if len(cult_alive) >= len(mafia_alive) + len(town_alive):
        await broadcast(room_id, {"type": "system", "text": "🔮 The Cult dominates all!"})
        room["state"] = "ended"
        return

    # No winner yet → continue next day
    room["phase"] = "day"
    room["day"] += 1
    await broadcast(room_id, {"type": "system", "text": f"🌞 Day {room['day']} begins!"})
    await broadcast(room_id, {"type": "room", "room": summary(room)})
    await perform_bot_day_actions(room_id)
