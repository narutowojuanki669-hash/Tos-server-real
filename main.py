from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Dict, Any, Optional
from uuid import uuid4
import asyncio, json, random, traceback

app = FastAPI(title="Town of Shadows – Final Server")

# ✅ Updated frontend domain
FRONTEND_ORIGIN = "https://690a64824cd1d9834c081fcd--resonant-cobbler-2fa697.netlify.app"

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
def start_game(rid: str):
    room = rooms.get(rid)
    if not room:
        raise HTTPException(404, "Room not found")
    if room["state"] != "waiting":
        raise HTTPException(400, "Game already started")
    room["state"] = "active"
    room["phase"] = "day"
    room["day"] = 1
    asyncio.create_task(broadcast(rid, {"type": "system", "text": "🌞 The game has started!"}))
    asyncio.create_task(broadcast(rid, {"type": "room", "room": summary(room)}))
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
