
# main.py - Town of Shadows (final backend v2)
# Run: uvicorn main:app --host 0.0.0.0 --port $PORT

import asyncio, json, random, time
from typing import Dict, Any, List, Optional
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

FRONTEND_ORIGINS = ["https://narutowjouanki669-hash.github.io","https://narutowjouanki669-hash.github.io/game-trial","http://localhost:5500"]
NIGHT_SECONDS = 40
DAY_DISCUSS = 60
DAY_VOTE = 20
DAY_DEFENCE = 10
DAY_FINAL = 10
TOTAL_PLAYERS = 20

TOWN_POOL = ["Doctor","Detective","Bodyguard","Vigilante","Jailor","Soldier","Cupid","Gossip","Lookout","Mayor","Investigator","Escort","Medium"]
MAFIA_POOL = ["Godfather","Mafioso","Janitor","Spy","Beastman","Blackmailer","Framer"]
CULT_POOL = ["Cult Leader","Fanatic","Infiltrator","Prophet","Acolyte"]
NEUTRAL_POOL = ["Jester","Executioner","Serial Killer","Arsonist","Survivor","Amnesiac","Witch","Guardian Angel"]

def role_to_faction(r: str) -> str:
    if r in TOWN_POOL: return "Town"
    if r in MAFIA_POOL: return "Mafia"
    if r in CULT_POOL: return "Cult"
    if r in NEUTRAL_POOL: return "Neutral"
    return "Unknown"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=FRONTEND_ORIGINS + ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

rooms: Dict[str, Dict[str, Any]] = {}
ws_managers: Dict[str, Dict[str, WebSocket]] = {}

class CreateRoomReq(BaseModel):
    host_name: Optional[str] = "Host"

class JoinReq(BaseModel):
    roomId: str
    name: Optional[str] = "Player"

class ActionReq(BaseModel):
    room_id: str
    actor: str
    target: str
    type: str

def sample_roles():
    roles=[]
    while len(roles)<8:
        roles.append(random.choice(TOWN_POOL))
    mafia=["Godfather","Mafioso"]
    remaining=[r for r in MAFIA_POOL if r not in mafia]
    while len(mafia)<4:
        mafia.append(random.choice(remaining+["Mafioso"]))
    roles.extend(mafia)
    roles.extend(["Cult Leader","Fanatic","Acolyte"])
    roles.extend(random.sample(NEUTRAL_POOL,3))
    while len(roles)<TOTAL_PLAYERS:
        roles.append(random.choice(TOWN_POOL))
    random.shuffle(roles)
    return roles

def create_room(host_name="Host"):
    rid=str(uuid4())[:6].upper()
    roles=sample_roles()
    players=[]
    for i in range(1,TOTAL_PLAYERS+1):
        r=roles[i-1]
        players.append({
            "slot":i,"name":f"Player {i}","is_bot":True,"alive":True,"role":r,"faction":role_to_faction(r),
            "ws_id":None,"revealed":False,"soldier_used":False,"contacted":False,"culted":False,"cleaned":False
        })
    room={"id":rid,"host":host_name,"players":players,"state":"waiting","phase":"waiting","day":0,
          "actions":[],"votes":{},"accused":None,"verdict_votes":{},"controller_task":None,"seen_rules":set()}
    rooms[rid]=room
    ws_managers[rid]={}
    return room

def room_summary(room):
    return {"id":room["id"],"host":room["host"],"state":room["state"],"phase":room["phase"],
            "day":room["day"],"players":[{"slot":p["slot"],"name":p["name"],"alive":p["alive"],
            "revealed":p["revealed"],"is_bot":p["is_bot"],"role":p["role"] if p["revealed"] else None,"faction":p["faction"]} for p in room["players"]],
            "accused":room.get("accused")}

@app.get("/test")
async def test(): return {"message":"Hello from Town of Shadows backend"}

@app.post("/create-room")
async def create_room_endpoint(req: CreateRoomReq):
    room=create_room(req.host_name)
    return {"roomId":room["id"], "room": room_summary(room)}

@app.post("/join-room")
async def join_room_endpoint(req: JoinReq):
    rid=req.roomId
    if rid not in rooms: raise HTTPException(status_code=404, detail="Room not found")
    room=rooms[rid]
    slot=next((p for p in room["players"] if p["is_bot"]), None)
    if not slot: raise HTTPException(status_code=400, detail="Room full")
    slot["is_bot"]=False
    slot["name"]=req.name or slot["name"]
    return {"slot":slot["slot"], "role":slot["role"], "faction":slot["faction"], "room": room_summary(room)}

@app.post("/queue-action")
async def queue_action(req: ActionReq):
    rid=req.room_id
    if rid not in rooms: raise HTTPException(status_code=404, detail="Room not found")
    room=rooms[rid]
    if not room["phase"].startswith("night"): raise HTTPException(status_code=400, detail="Actions only allowed at night")
    room.setdefault("actions",[]).append({"actor":req.actor,"target":req.target,"type":req.type,"ts":time.time(),"actor_role":None})
    return {"ok":True}

# WebSocket helpers
async def send_to_ws(room_id, wsid, message):
    mgr = ws_managers.get(room_id, {})
    ws = mgr.get(wsid)
    if not ws: return
    try:
        await ws.send_text(json.dumps(message))
    except:
        mgr.pop(wsid, None)

async def broadcast(room_id, message):
    mgr = ws_managers.get(room_id, {})
    dead=[]
    for wsid, ws in list(mgr.items()):
        try:
            await ws.send_text(json.dumps(message))
        except:
            dead.append(wsid)
    for d in dead: mgr.pop(d, None)

async def send_to_player(room_id, player_name, message):
    room=rooms.get(room_id)
    if not room: return
    p=next((x for x in room["players"] if x["name"]==player_name), None)
    if not p: return
    wsid=p.get("ws_id")
    if not wsid: return
    await send_to_ws(room_id, wsid, message)

async def send_to_faction(room_id, faction, message):
    room=rooms.get(room_id)
    if not room: return
    for p in room["players"]:
        if p["faction"]==faction and p.get("ws_id"):
            await send_to_player(room_id, p["name"], message)

def faction_list(room, viewer):
    items=[]
    for p in room["players"]:
        if p["faction"]!=viewer.get("faction"): continue
        if p["role"]=="Fanatic" and not p.get("contacted",False):
            if viewer["role"] not in ("Fanatic","Cult Leader"): continue
        if p["role"]=="Spy" and not p.get("contacted",False): continue
        items.append({"slot":p["slot"],"role":p["role"],"name":p["name"],"alive":p["alive"]})
    return items

async def send_faction_mates(room_id):
    room=rooms.get(room_id)
    if not room: return
    for p in room["players"]:
        if not p.get("ws_id"): continue
        if p.get("faction") in ("Mafia","Cult"):
            mates = faction_list(room,p)
            await send_to_player(room_id,p["name"],{"type":"faction_mates","mates":mates})

@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await websocket.accept()
    if room_id not in rooms:
        await websocket.send_text(json.dumps({"type":"system","text":"Room not found"}))
        await websocket.close()
        return
    wsid=str(uuid4())
    ws_managers[room_id][wsid]=websocket
    try:
        await websocket.send_text(json.dumps({"type":"system","text":f"Connected to {room_id}","ws_id":wsid}))
        while True:
            raw = await websocket.receive_text()
            try:
                msg = json.loads(raw)
            except:
                await websocket.send_text(json.dumps({"type":"system","text":"Invalid JSON"}))
                continue
            await handle_ws(room_id, wsid, msg)
    except WebSocketDisconnect:
        ws_managers[room_id].pop(wsid, None)
    except Exception:
        ws_managers[room_id].pop(wsid, None)

async def handle_ws(room_id, wsid, msg):
    mtype = msg.get("type")
    room = rooms.get(room_id)
    if not room: return

    if mtype=="identify":
        slot = msg.get("slot")
        p = next((x for x in room["players"] if x["slot"]==slot), None)
        if p:
            p["ws_id"]=wsid
            p["is_bot"]=False
            await send_to_player(room_id,p["name"],{"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"]})
            await broadcast(room_id,{"type":"room","room":room_summary(room)})
            await send_faction_mates(room_id)
        else:
            await send_to_ws(room_id, wsid, {"type":"system","text":"Slot not found"})
        return

    if mtype=="chat":
        ch = msg.get("channel","public")
        text = msg.get("text","")
        sender = msg.get("from","Anon")
        if room["phase"]=="day_vote" and text.strip().isdigit():
            target_slot = int(text.strip())
            target_p = next((x for x in room["players"] if x["slot"]==target_slot), None)
            if target_p:
                room.setdefault("votes",{})[sender]=target_p["name"]
                await send_to_ws(room_id, wsid, {"type":"system","text":f"You voted for Player {target_slot}"})
                await broadcast(room_id, {"type":"system","text":f"{sender} cast a vote (anonymous)."})
                return
        if ch=="mafia": await send_to_faction(room_id,"Mafia",{"type":"chat","from":sender,"text":text,"channel":"mafia"}); return
        if ch=="cult": await send_to_faction(room_id,"Cult",{"type":"chat","from":sender,"text":text,"channel":"cult"}); return
        if ch=="dead":
            for p in room["players"]:
                if not p["alive"] and p.get("ws_id"):
                    await send_to_player(room_id,p["name"],{"type":"chat","from":sender,"text":text,"channel":"dead"})
            return
        await broadcast(room_id,{"type":"chat","from":sender,"text":text,"channel":"public"})
        return

    if mtype=="player_action":
        action = msg.get("action")
        if action:
            if not room["phase"].startswith("night"):
                await send_to_ws(room_id, wsid, {"type":"system","text":"Actions only allowed at night"})
                return
            room.setdefault("actions",[]).append({
                "actor": action.get("actor"),
                "target": action.get("target"),
                "type": action.get("type"),
                "ts": time.time(),
                "actor_role": action.get("actor_role")
            })
            await send_to_ws(room_id, wsid, {"type":"system","text":"Action queued."})
        return

    if mtype=="start_game":
        try:
            await start_game(room_id)
        except Exception as e:
            await send_to_ws(room_id, wsid, {"type":"system","text":str(e)})
        return

    if mtype=="vote":
        if room["phase"]!="day_vote":
            await send_to_ws(room_id, wsid, {"type":"system","text":"Voting only during vote phase"})
            return
        voter = msg.get("from")
        target = msg.get("target")
        if isinstance(target,str) and target.isdigit():
            tgt = next((x for x in room["players"] if x["slot"]==int(target)), None)
            if tgt:
                room.setdefault("votes",{})[voter]=tgt["name"]
                await send_to_ws(room_id, wsid, {"type":"system","text":f"You voted for Player {tgt['slot']}"})
                await broadcast(room_id, {"type":"system","text":f"{voter} cast a vote (anonymous)."})
                return
        if target in ("skip","SKIP"):
            room.setdefault("votes",{})[voter]="SKIP"
            await broadcast(room_id, {"type":"system","text":f"{voter} skipped voting."})
            return
        room.setdefault("votes",{})[voter]=target
        await broadcast(room_id, {"type":"system","text":f"{voter} voted for {target}"})
        return

    await send_to_ws(room_id, wsid, {"type":"system","text":"Unknown message type"})

# Start controller and supporting functions are included in the packaged file.
@app.on_event("startup")
async def startup_event():
    if not rooms:
        r=create_room("Host")
        print("Sample room created:", r["id"])
