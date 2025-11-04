# main.py
# Town of Shadows - FastAPI WebSocket server (Enhanced version)
# Instructions: install dependencies from requirements.txt and run:
#    uvicorn main:app --host 0.0.0.0 --port $PORT
# This server implements rooms, AI fillers, private role messages, and simple night processing.

from fastapi import FastAPI

app = FastAPI()

@app.get("/test")
def test():
    return {"message": "Server is running!"}

@app.get("/")
def home():
    return {"message": "Welcome to Town of Shadows server!"}

import asyncio, json, random, os
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from typing import Dict, List, Any
from uuid import uuid4

app = FastAPI(title="Town of Shadows - FastAPI Server (Enhanced)")

# Allow Netlify frontend origin and common local hosts
origins = [
    "https://69095967f1d5a6e43a33739b--bright-sunshine-4f6996.netlify.app",
    "http://localhost:5173",
    "http://localhost:3000",
    "https://localhost"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Configuration ---
TOTAL_PLAYERS = 20
FACTION_COUNTS = {"town":8, "mafia":5, "cult":4, "neutrals":3}

ROLE_POOL = {
    "town":["Detective","Sheriff","Investigator","Lookout","Tracker","Bodyguard","Doctor","Jailor","Cupid","Mayor","Vigilante","Escort","Medium","Soldier","Gossip"],
    "mafia":["Godfather","Mafioso","Janitor","Spy","Beastman","Consort","Blackmailer","Framer","Disguiser","Forger"],
    "cult":["Cult Leader","Fanatic","Infiltrator","Prophet","High Priest","Acolyte"],
    "neutrals":["Jester","Executioner","Serial Killer","Arsonist","Survivor","Amnesiac","Witch","Guardian Angel"]
}

# --- Utilities ---
def shuffle(a):
    b = a[:]
    random.shuffle(b)
    return b
def sample(a): return random.choice(a)

def build_mafia_roles():
    roles = ["Godfather","Mafioso"]
    others = [r for r in ROLE_POOL["mafia"] if r not in roles]
    roles.append(sample(others))
    while len(roles) < FACTION_COUNTS["mafia"]:
        roles.append("Mafioso" if random.random() < 0.5 else sample(others))
    random.shuffle(roles)
    return roles

def build_cult_roles():
    roles = ["Cult Leader","Fanatic"]
    pool = [r for r in ROLE_POOL["cult"] if r not in roles]
    while len(roles) < FACTION_COUNTS["cult"]:
        roles.append(sample(pool))
    random.shuffle(roles)
    return roles

def build_town_roles():
    return shuffle(ROLE_POOL["town"])[:FACTION_COUNTS["town"]]

def build_neutrals():
    return shuffle(ROLE_POOL["neutrals"])[:FACTION_COUNTS["neutrals"]]

def build_full_roles():
    mafia = build_mafia_roles()
    cult = build_cult_roles()
    town = build_town_roles()
    neutrals = build_neutrals()
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

# --- In-memory rooms ---
rooms: Dict[str, Any] = {}

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, WebSocket] = {}

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        ws_id = str(uuid4())
        self.active_connections[ws_id] = websocket
        return ws_id

    def disconnect(self, ws_id: str):
        if ws_id in self.active_connections:
            del self.active_connections[ws_id]

    async def send_personal(self, ws_id: str, message: dict):
        ws = self.active_connections.get(ws_id)
        if ws:
            try:
                await ws.send_text(json.dumps(message))
            except:
                pass

    async def broadcast(self, room_id: str, message: dict):
        room = rooms.get(room_id)
        if not room: return
        for slot in room["players"]:
            if not slot["is_bot"] and slot.get("ws_id"):
                ws = self.active_connections.get(slot["ws_id"])
                if ws:
                    try:
                        await ws.send_text(json.dumps(message))
                    except:
                        pass

manager = ConnectionManager()

def create_room(host_name="Host"):
    room_id = str(uuid4())[:6].upper()
    roles = build_full_roles()
    players = []
    for i in range(1, TOTAL_PLAYERS+1):
        players.append({
            "slot": i,
            "name": f"Player {i}",
            "role": roles[i-1],
            "faction": role_to_faction(roles[i-1]),
            "is_bot": True,
            "alive": True,
            "revealed": False,
            "doused": False,
            "ws_id": None
        })
    rooms[room_id] = {
        "id": room_id,
        "host": host_name,
        "players": players,
        "state": "waiting",
        "phase": "day",
        "day": 1,
        "actions": [],
        "votes": {},
    }
    return rooms[room_id]

def ai_choose_action(room, bot_player):
    if room["phase"] != "night": return None
    alive = [p for p in room["players"] if p["alive"]]
    if bot_player["faction"] == "Mafia":
        target = sample([p for p in alive if p["faction"] != "Mafia" and p["slot"] != bot_player["slot"]])
        return {"type":"mafia_kill","by":bot_player["slot"], "target": target["slot"]}
    if bot_player["faction"] == "Cult":
        if random.random() < 0.2:
            target = sample([p for p in alive if p["faction"] != "Cult" and p["slot"] != bot_player["slot"]])
            return {"type":"cult_convert","by":bot_player["slot"], "target": target["slot"]}
        return None
    if bot_player["role"] == "Arsonist":
        if random.random() < 0.6:
            target = sample([p for p in alive if p["slot"] != bot_player["slot"]])
            return {"type":"arson_douse","by":bot_player["slot"], "target":target["slot"]}
    return None

def resolve_night(room):
    actions = room.get("actions", [])
    log = []
    arson_douses = [a["target"] for a in actions if a["type"]=="arson_douse"]
    for t in arson_douses:
        p = next((x for x in room["players"] if x["slot"]==t and x["alive"]), None)
        if p:
            p["doused"] = True
            log.append(f"Player {t} was doused by an Arsonist.")

    mafia_targets = [a["target"] for a in actions if a["type"]=="mafia_kill"]
    mafia_target = random.choice(mafia_targets) if mafia_targets else None
    heals = [a["target"] for a in actions if a["type"]=="heal"]

    if mafia_target:
        victim = next((x for x in room["players"] if x["slot"]==mafia_target and x["alive"]), None)
        if victim:
            if mafia_target in heals:
                log.append(f"Player {mafia_target} was attacked by Mafia but saved by Doctor.")
            else:
                victim["alive"] = False; victim["revealed"] = True
                log.append(f"Player {mafia_target} was killed by Mafia. Role: {victim['role']}")

    for a in [ac for ac in actions if ac["type"]=="cult_convert"]:
        v = next((x for x in room["players"] if x["slot"]==a["target"] and x["alive"]), None)
        if v and v["role"] not in ["Godfather","Mafioso","Janitor","Beastman","Soldier"]:
            v["faction"] = "Cult"; v["role"] = "Acolyte"
            log.append(f"Player {v['slot']} was converted to Cult.")

    ignite = any(a["type"]=="arson_ignite" for a in actions)
    if ignite:
        burned = [p for p in room["players"] if p["alive"] and p.get("doused")]
        for b in burned:
            b["alive"] = False; b["revealed"] = True
            log.append(f"Player {b['slot']} burned in Arsonist ignition. Role: {b['role']}")

    room["actions"] = []
    return log

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    ws_id = await manager.connect(websocket)
    try:
        await manager.send_personal(ws_id, {"type":"system_msg", "text":"Connected to Town of Shadows FastAPI server."})
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except:
                await manager.send_personal(ws_id, {"type":"system_msg", "text":"Invalid JSON."})
                continue

            mtype = msg.get("type")
            if mtype == "create_room":
                name = msg.get("name","Host")
                room = create_room(name)
                rooms[room["id"]] = room
                await manager.send_personal(ws_id, {"type":"room_info", "room": room_summary(room)})
                await manager.send_personal(ws_id, {"type":"system_msg", "text":f"Room {room['id']} created. Share this code to friends."})
            elif mtype == "join_room":
                rid = msg.get("roomId")
                name = msg.get("name","Player")
                room = rooms.get(rid)
                if not room:
                    await manager.send_personal(ws_id, {"type":"system_msg","text":"Room not found."}); continue
                slot = next((p for p in room["players"] if p["is_bot"]), None)
                if not slot:
                    await manager.send_personal(ws_id, {"type":"system_msg","text":"Room full."}); continue
                slot["is_bot"] = False; slot["name"]=name; slot["ws_id"]=ws_id
                await manager.send_personal(ws_id, {"type":"private_role", "slot":slot["slot"], "role":slot["role"], "faction":slot["faction"], "explain": role_explain(slot["role"], slot["faction"]) })
                await manager.send_personal(ws_id, {"type":"system_msg", "text":f"You joined as slot {slot['slot']}."})
                await manager.broadcast(room["id"], {"type":"room_info", "room": room_summary(room)})
            elif mtype == "start_game":
                rid = msg.get("roomId"); room = rooms.get(rid)
                if not room: await manager.send_personal(ws_id, {"type":"system_msg","text":"Room not found."}); continue
                room["state"] = "running"; room["phase"]="night"; room["day"]=1; room["actions"]=[]
                for p in room["players"]:
                    if not p["is_bot"] and p.get("ws_id"):
                        await manager.send_personal(p["ws_id"], {"type":"private_role","slot":p["slot"], "role":p["role"], "faction":p["faction"], "explain": role_explain(p["role"], p["faction"]) })
                await manager.broadcast(room["id"], {"type":"system_msg","text":"Game started. Night 1 begins."})
                await manager.broadcast(room["id"], {"type":"room_info","room": room_summary(room)})
            elif mtype == "player_action":
                rid = msg.get("roomId"); room = rooms.get(rid)
                if not room: await manager.send_personal(ws_id, {"type":"system_msg","text":"Room not found."}); continue
                if room["phase"] != "night":
                    await manager.send_personal(ws_id, {"type":"system_msg","text":"Actions accepted only at night."}); continue
                action = msg.get("action")
                action["by"] = msg.get("slot")
                room.setdefault("actions", []).append(action)
                await manager.send_personal(ws_id, {"type":"system_msg","text":f"Action queued: {action.get('type')} by slot {action.get('by')}"})
            elif mtype == "advance_phase":
                rid = msg.get("roomId"); room = rooms.get(rid)
                if not room: await manager.send_personal(ws_id, {"type":"system_msg","text":"Room not found."}); continue
                if room["phase"] == "day":
                    room["phase"] = "night"; await manager.broadcast(room["id"], {"type":"system_msg","text":f"Night {room['day']} falls."})
                    for p in room["players"]:
                        if p["is_bot"] and p["alive"]:
                            act = ai_choose_action(room, p)
                            if act: room.setdefault("actions", []).append(act)
                    await asyncio.sleep(1.0)
                    logs = resolve_night(room)
                    for l in logs:
                        await manager.broadcast(room["id"], {"type":"system_msg","text":l})
                    room["phase"] = "day"; room["day"] += 1
                    await manager.broadcast(room["id"], {"type":"room_info","room": room_summary(room)})
                    await manager.broadcast(room["id"], {"type":"system_msg","text":f"Day {room['day']} dawns."})
                else:
                    await manager.send_personal(ws_id, {"type":"system_msg","text":"Server: cannot advance to night manually."})
            elif mtype == "vote":
                rid = msg.get("roomId"); room = rooms.get(rid)
                if not room: await manager.send_personal(ws_id, {"type":"system_msg","text":"Room not found."}); continue
                slot = msg.get("slot"); target = msg.get("target")
                room.setdefault("votes", {})[str(slot)] = target
                await manager.broadcast(room["id"], {"type":"system_msg","text":f"Player {slot} voted for {target}."})
                alive_count = sum(1 for p in room["players"] if p["alive"])
                tally = {}
                for v in room.get("votes", {}).values():
                    tally[v] = tally.get(v, 0) + 1
                for t,count in tally.items():
                    if count > alive_count / 2:
                        victim = next((x for x in room["players"] if x["slot"]==int(t) and x["alive"]), None)
                        if victim:
                            victim["alive"] = False; victim["revealed"]=True
                            await manager.broadcast(room["id"], {"type":"system_msg","text":f"Player {victim['slot']} was lynched. Role: {victim['role']}"})
                            room["votes"] = {}
                            await manager.broadcast(room["id"], {"type":"room_info","room": room_summary(room)})
            elif mtype == "chat":
                rid = msg.get("roomId"); room = rooms.get(rid)
                if room:
                    await manager.broadcast(room["id"], {"type":"chat","from": msg.get("slot"), "text": msg.get("text")})
            else:
                await manager.send_personal(ws_id, {"type":"system_msg","text":"Unknown message type."})

    except WebSocketDisconnect:
        manager.disconnect(ws_id)
    except Exception as e:
        try:
            await manager.send_personal(ws_id, {"type":"system_msg","text":f"Server error: {str(e)}"})
        except: pass

def room_summary(room):
    return {
        "id": room["id"],
        "host": room["host"],
        "state": room["state"],
        "phase": room["phase"],
        "day": room["day"],
        "players": [{
            "slot": p["slot"], "name": p["name"], "alive": p["alive"], "revealed": p["revealed"],
            "symbol": p["role"] if p["revealed"] else ("?" if p["alive"] else None), "is_bot": p["is_bot"]
        } for p in room["players"]]
    }

def role_explain(role, faction):
    explanations = {
        "Arsonist": "Neutral Killer. Douse players at night; ignite later to burn all doused.",
        "Jailor": "Town. Jail one player at night to interrogate or execute (risk losing power if wrong).",
        "Doctor": "Town. Can heal players at night; can self-heal twice. Cannot prevent culting.",
        "Detective": "Town. Investigative role; check alignment of players.",
        "Godfather": "Mafia leader. Controls the Mafia; appears innocent to some investigations.",
        "Mafioso": "Mafia killer. Works with Godfather to kill at night.",
        "Cult Leader": "Cult. Tries to convert players; wins when Cult >= sum of others.",
        "Fanatic": "Cult. Oblivious until contacted; can become second leader if contacted."
    }
    return explanations.get(role, f"{role} — {faction} role. Detailed rules in the guide.")
