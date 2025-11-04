# main.py
# Town of Shadows - FastAPI server (Game Backend Ready)
# Includes: /test, REST endpoints for room management, and a WebSocket /ws endpoint.
import asyncio, json, random
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from uuid import uuid4

app = FastAPI(title="Town of Shadows - Game Backend (Ready)")

FRONTEND_ORIGIN = "https://690a4beb4e174c424fe59d8c--townofshadows.netlify.app"
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_ORIGIN, "http://localhost:5173", "http://localhost:3000"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

TOTAL_PLAYERS = 20
FACTION_COUNTS = {"town":8, "mafia":5, "cult":4, "neutrals":3}

ROLE_POOL = {
    "town":["Detective","Sheriff","Investigator","Lookout","Tracker","Bodyguard","Doctor","Jailor","Cupid","Mayor","Vigilante","Escort","Medium","Soldier","Gossip"],
    "mafia":["Godfather","Mafioso","Janitor","Spy","Beastman","Consort","Blackmailer","Framer","Disguiser","Forger"],
    "cult":["Cult Leader","Fanatic","Infiltrator","Prophet","High Priest","Acolyte"],
    "neutrals":["Jester","Executioner","Serial Killer","Arsonist","Survivor","Amnesiac","Witch","Guardian Angel"]
}

def sample(a): import random; return random.choice(a)
def shuffle(a): import random; b=a[:]; random.shuffle(b); return b

def build_mafia_roles():
    roles = ["Godfather","Mafioso"]
    others = [r for r in ROLE_POOL["mafia"] if r not in roles]
    roles.append(sample(others))
    while len(roles) < FACTION_COUNTS["mafia"]:
        roles.append("Mafioso" if random.random() < 0.5 else sample(others))
    random.shuffle(roles); return roles

def build_cult_roles():
    roles = ["Cult Leader","Fanatic"]
    pool = [r for r in ROLE_POOL["cult"] if r not in roles]
    while len(roles) < FACTION_COUNTS["cult"]:
        roles.append(sample(pool))
    random.shuffle(roles); return roles

def build_town_roles():
    return shuffle(ROLE_POOL["town"])[:FACTION_COUNTS["town"]]

def build_neutrals():
    return shuffle(ROLE_POOL["neutrals"])[:FACTION_COUNTS["neutrals"]]

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

rooms = {}
ws_connections = {}

class JoinRequest(BaseModel):
    roomId: str
    name: str = "Player"

@app.get("/test")
def test():
    return {"message":"Hello from Town of Shadows backend!"}

@app.post("/create-room")
def create_room(name: str = "Host"):
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
    room = {"id":room_id,"host":name,"players":players,"state":"waiting","phase":"day","day":1,"actions":[],"votes":{}}
    rooms[room_id]=room
    return {"roomId":room_id,"room":room_summary(room)}

@app.post("/join-room")
def join_room(req: JoinRequest):
    room = rooms.get(req.roomId)
    if not room:
        raise HTTPException(status_code=404, detail="Room not found")
    slot = next((p for p in room["players"] if p["is_bot"]), None)
    if not slot:
        raise HTTPException(status_code=400, detail="Room full")
    slot["is_bot"]=False; slot["name"]=req.name
    return {"slot":slot["slot"],"role":slot["role"],"faction":slot["faction"],"explain":role_explain(slot["role"],slot["faction"]),"room":room_summary(room)}

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
    act = req.action; act["by"]=req.slot
    room.setdefault("actions",[]).append(act)
    return {"status":"queued","action":act}

@app.post("/start-game")
def start_game(roomId: str):
    room = rooms.get(roomId)
    if not room: raise HTTPException(status_code=404, detail="Room not found")
    room["state"]="running"; room["phase"]="night"; room["day"]=1; room["actions"]=[]
    asyncio.create_task(broadcast_room(roomId,{"type":"system_msg","text":"Game started. Night 1 begins."}))
    return {"status":"started","room":room_summary(room)}

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await websocket.accept()
    ws_id = str(uuid4()); ws_connections[ws_id]=websocket
    try:
        await websocket.send_text(json.dumps({"type":"system_msg","text":"Connected to Town of Shadows WS"}))
        while True:
            data = await websocket.receive_text()
            try:
                msg = json.loads(data)
            except:
                await websocket.send_text(json.dumps({"type":"system_msg","text":"Invalid JSON"})); continue
            mtype = msg.get("type")
            if mtype == "create_room":
                resp = create_room(msg.get("name","Host"))
                await websocket.send_text(json.dumps({"type":"room_info","room":resp["room"],"roomId":resp["roomId"]}))
            elif mtype == "join_room":
                try:
                    jresp = join_room(JoinRequest(roomId=msg.get("roomId"),name=msg.get("name","Player")))
                    await websocket.send_text(json.dumps({"type":"private_role","slot":jresp["slot"],"role":jresp["role"],"faction":jresp["faction"],"explain":jresp["explain"]}))
                    await broadcast_room(msg.get("roomId"),{"type":"room_info","room":room_summary(rooms[msg.get("roomId")])})
                except Exception as e:
                    await websocket.send_text(json.dumps({"type":"system_msg","text":str(e)}))
            elif mtype == "start_game":
                try:
                    start_game(msg.get("roomId"))
                except Exception as e:
                    await websocket.send_text(json.dumps({"type":"system_msg","text":str(e)}))
            elif mtype == "player_action":
                try:
                    player_action(ActionRequest(roomId=msg.get("roomId"),slot=msg.get("slot"),action=msg.get("action")))
                    await websocket.send_text(json.dumps({"type":"system_msg","text":"Action queued"}))
                except Exception as e:
                    await websocket.send_text(json.dumps({"type":"system_msg","text":str(e)}))
            elif mtype == "chat":
                await broadcast_room(msg.get("roomId"),{"type":"chat","from":msg.get("slot"),"text":msg.get("text")})
            else:
                await websocket.send_text(json.dumps({"type":"system_msg","text":"Unknown message type"}))
    except WebSocketDisconnect:
        ws_connections.pop(ws_id,None)

async def broadcast_room(room_id: str, message: dict):
    room = rooms.get(room_id); 
    if not room: return
    for p in room["players"]:
        if not p["is_bot"] and p.get("ws_id"):
            ws = ws_connections.get(p["ws_id"])
            if ws:
                try: await ws.send_text(json.dumps(message))
                except: pass
    for ws in list(ws_connections.values()):
        try: await ws.send_text(json.dumps(message))
        except: pass

def room_summary(room: dict):
    return {"id":room["id"],"host":room["host"],"state":room["state"],"phase":room["phase"],"day":room["day"],
            "players":[{"slot":p["slot"],"name":p["name"],"alive":p["alive"],"revealed":p["revealed"],
                        "symbol":p["role"] if p["revealed"] else ("?" if p["alive"] else None),
                        "is_bot":p["is_bot"]} for p in room["players"]]}

def role_explain(role,faction):
    explanations = {
        "Arsonist":"Neutral Killer. Douse players at night; ignite later to burn all doused.",
        "Jailor":"Town. Jail one player at night to interrogate or execute (risk losing power if wrong).",
        "Doctor":"Town. Can heal players at night; can self-heal twice. Cannot prevent culting.",
        "Detective":"Town. Investigative role; check alignment of players.",
        "Godfather":"Mafia leader. Controls the Mafia; appears innocent to some investigations.",
        "Mafioso":"Mafia killer. Works with Godfather to kill at night.",
        "Cult Leader":"Cult. Tries to convert players; wins when Cult >= sum of others.",
        "Fanatic":"Cult. Oblivious until contacted; can become second leader if contacted."
    }
    return explanations.get(role,f"{role} — {faction} role.")