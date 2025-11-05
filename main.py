# main.py - Baseline stable server (phase loop + websocket + join/start)
# Deploy: uvicorn main:app --host 0.0.0.0 --port 10000

import asyncio
import json
import random
import time
from typing import Dict, Any, Optional
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Town of Shadows - Baseline Server")

# Update these to your frontend/backend URLs if they differ
FRONTEND_URL = "https://690a8382d00a2311478c4251--celebrated-bonbon-1fd3cf.netlify.app"
BACKEND_BASE = "https://town-of-shadows-server.onrender.com"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Data models ---
class CreateRoomReq(BaseModel):
    host_name: Optional[str] = "Host"

class JoinReq(BaseModel):
    roomId: str
    name: Optional[str] = "Player"

# --- In-memory state ---
rooms: Dict[str, Any] = {}        # room_id -> room object
ws_managers: Dict[str, Dict[str, WebSocket]] = {}  # room_id -> {wsid: websocket}

# default timers for baseline
NIGHT_SECONDS = 40
DAY_SECONDS = 60 + 20 + 10 + 10  # discussion + voting + defence + final = 100

# helper: create a room object
def create_room_obj(host_name="Host", total_slots: int = 20):
    room_id = str(uuid4())[:6].upper()
    players = []
    for i in range(1, total_slots + 1):
        p = {
            "slot": i,
            "name": f"Player {i}",
            "is_bot": True,
            "alive": True,
            "role": random.choice(["Detective","Doctor","Villager","Mafioso","Jester","Executioner"]),
            "revealed": False,
            "ws_id": None
        }
        players.append(p)
    room = {
        "id": room_id,
        "host": host_name,
        "players": players,
        "state": "waiting",
        "phase": "waiting",
        "day": 0,
        "controller_task": None
    }
    rooms[room_id] = room
    ws_managers[room_id] = {}
    return room

def room_summary(room):
    return {
        "id": room["id"],
        "host": room["host"],
        "state": room["state"],
        "phase": room["phase"],
        "day": room["day"],
        "players": [
            {"slot": p["slot"], "name": p["name"], "alive": p["alive"], "revealed": p["revealed"], "is_bot": p["is_bot"]}
            for p in room["players"]
        ]
    }

# --- REST endpoints ---
@app.post("/create-room")
def create_room(req: CreateRoomReq):
    room = create_room_obj(req.host_name)
    return {"roomId": room["id"], "room": room_summary(room)}

@app.post("/join-room")
def join_room(req: JoinReq):
    room_id = req.roomId
    if room_id not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    room = rooms[room_id]
    # find first bot slot
    slot = next((p for p in room["players"] if p["is_bot"]), None)
    if not slot:
        raise HTTPException(status_code=400, detail="Room full")
    slot["is_bot"] = False
    slot["name"] = req.name or slot["name"]
    return {"slot": slot["slot"], "role": slot["role"], "room": room_summary(room)}

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
    # send initial phase broadcast immediately
    await broadcast(room_id, {"type":"system","text":"Game starting: Night 1 begins."})
    await broadcast_phase(room_id, "Night 1", NIGHT_SECONDS)
    # start controller loop background task
    if room.get("controller_task") is None:
        room["controller_task"] = asyncio.create_task(phase_controller(room_id))
    return {"ok": True, "room": room_summary(room)}

@app.get("/room/{room_id}")
def get_room(room_id: str):
    if room_id not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    return room_summary(rooms[room_id])

# --- Websocket endpoint ---
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
            except:
                await websocket.send_text(json.dumps({"type":"system","text":"Invalid JSON"})); continue
            mtype = msg.get("type")
            if mtype == "identify":
                # client binds to a slot
                slot = msg.get("slot")
                room = rooms[room_id]
                p = next((x for x in room["players"] if x["slot"] == slot), None)
                if not p:
                    await websocket.send_text(json.dumps({"type":"system","text":"Slot not found"}))
                    continue
                p["ws_id"] = wsid
                p["is_bot"] = False
                # send private role immediately
                await websocket.send_text(json.dumps({"type":"private_role","slot":p["slot"],"role":p["role"], "explain": f"{p['role']} - baseline role info"}))
                # broadcast updated room summary
                await broadcast(room_id, {"type":"room","room": room_summary(room)})
            elif mtype == "chat":
                # public chat broadcast
                text = msg.get("text","")
                sender = msg.get("from","")
                await broadcast(room_id, {"type":"chat","from":sender,"text":text})
            else:
                await websocket.send_text(json.dumps({"type":"system","text":"Unknown ws message type"}))
    except WebSocketDisconnect:
        # cleanup
        ws_managers[room_id].pop(wsid, None)
    except Exception:
        ws_managers[room_id].pop(wsid, None)

# --- Broadcast helpers ---
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
    # also send room summary occasionally
    await broadcast(room_id, {"type":"room","room": room_summary(rooms[room_id])})

# --- Phase controller loop (alternates night/day) ---
async def phase_controller(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    try:
        # already started with Night 1 by start_game
        cycle = 1
        while room["state"] == "active":
            # NIGHT (already broadcast at start) - wait NIGHT_SECONDS
            await asyncio.sleep(NIGHT_SECONDS)
            # after night, start day
            room["phase"] = "day"
            room["day"] += 1
            await broadcast_phase(room_id, f"Day {room['day']} (Discussion)", DAY_SECONDS)
            # day lasts DAY_SECONDS
            await asyncio.sleep(DAY_SECONDS)
            # after day, start next night
            cycle += 1
            room["phase"] = "night"
            await broadcast_phase(room_id, f"Night {cycle}", NIGHT_SECONDS)
            # loop continues
    except Exception:
        pass

# --- Run-time sanity: create an example room on startup for convenience ---
@app.on_event("startup")
async def startup_event():
    # only create if none exist
    if not rooms:
        r = create_room_obj("Host")
        print("Created sample room:", r["id"])
