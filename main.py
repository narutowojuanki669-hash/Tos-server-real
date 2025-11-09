# main.py – Town of Shadows backend (stable baseline + Step 1 roles)
# Deploy with: uvicorn main:app --host 0.0.0.0 --port 10000

import asyncio
import json
import random
from typing import Dict, Any, Optional
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

app = FastAPI(title="Town of Shadows")

FRONTEND_URL = "https://690a8382d00a2311478c4251--celebrated-bonbon-1fd3cf.netlify.app"
BACKEND_BASE = "https://town-of-shadows-server.onrender.com"

app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL, "http://localhost:3000", "http://localhost:5173", "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ----------------------------------------------------------------
# Models
class CreateRoomReq(BaseModel):
    host_name: Optional[str] = "Host"

class JoinReq(BaseModel):
    roomId: str
    name: Optional[str] = "Player"

# ----------------------------------------------------------------
# Globals
rooms: Dict[str, Any] = {}
ws_managers: Dict[str, Dict[str, WebSocket]] = {}

NIGHT_SECONDS = 40
DAY_SECONDS = 60 + 20 + 10 + 10  # total 100s

# ----------------------------------------------------------------
# Helpers
def create_room_obj(host_name="Host", total_slots: int = 20):
    rid = str(uuid4())[:6].upper()
    players = []
    for i in range(1, total_slots + 1):
        players.append({
            "slot": i,
            "name": f"Player {i}",
            "is_bot": True,
            "alive": True,
            "role": "Villager",
            "faction": "town",
            "ws_id": None
        })
    room = {
        "id": rid,
        "host": host_name,
        "players": players,
        "state": "waiting",
        "phase": "waiting",
        "day": 0,
        "controller_task": None
    }
    rooms[rid] = room
    ws_managers[rid] = {}
    return room

def room_summary(room):
    return {
        "id": room["id"],
        "host": room["host"],
        "state": room["state"],
        "phase": room["phase"],
        "day": room["day"],
        "players": [
            {"slot": p["slot"], "name": p["name"], "alive": p["alive"],
             "faction": p.get("faction","?"), "is_bot": p["is_bot"]}
            for p in room["players"]
        ]
    }

# ----------------------------------------------------------------
# Role assignment
def assign_roles(room):
    town_roles = [
        "Doctor", "Detective", "Bodyguard", "Vigilante", "Jailor",
        "Soldier", "Cupid", "Gossip"
    ]
    mafia_roles = ["Godfather", "Mafioso", "Beastman", "Janitor", "Spy"]
    cult_roles = ["Cult Leader", "Fanatic", "Infiltrator"]
    neutral_roles = ["Jester", "Executioner", "Serial Killer", "Arsonist"]
    all_roles = town_roles + mafia_roles + cult_roles + neutral_roles
    random.shuffle(all_roles)

    for i, p in enumerate(room["players"]):
        r = all_roles[i % len(all_roles)]
        p["role"] = r
        if r in town_roles:
            p["faction"] = "town"
        elif r in mafia_roles:
            p["faction"] = "mafia"
        elif r in cult_roles:
            p["faction"] = "cult"
        else:
            p["faction"] = "neutral"

# ----------------------------------------------------------------
# REST endpoints
@app.post("/create-room")
def create_room(req: CreateRoomReq):
    room = create_room_obj(req.host_name)
    return {"roomId": room["id"], "room": room_summary(room)}

@app.post("/join-room")
def join_room(req: JoinReq):
    rid = req.roomId
    if rid not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    room = rooms[rid]
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

    # assign roles
    assign_roles(room)

    # send private roles to humans
    mgr = ws_managers.get(room_id, {})
    for wsid, ws in mgr.items():
        p = next((x for x in room["players"] if x.get("ws_id") == wsid), None)
        if p:
            try:
                await ws.send_text(json.dumps({
                    "type": "private_role",
                    "slot": p["slot"],
                    "role": p["role"],
                    "explain": f"Faction: {p['faction']}"
                }))
            except Exception:
                pass

    await broadcast(room_id, {"type": "system", "text": "Roles assigned. Night 1 begins."})
    await broadcast_phase(room_id, "Night 1", NIGHT_SECONDS)

    if room.get("controller_task") is None:
        room["controller_task"] = asyncio.create_task(phase_controller(room_id))
    return {"ok": True, "room": room_summary(room)}

@app.get("/room/{room_id}")
def get_room(room_id: str):
    if room_id not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    return room_summary(rooms[room_id])

# ----------------------------------------------------------------
# WebSocket
@app.websocket("/ws/{room_id}")
async def websocket_endpoint(ws: WebSocket, room_id: str):
    await ws.accept()
    if room_id not in rooms:
        await ws.send_text(json.dumps({"type": "system", "text": "Room not found"}))
        await ws.close()
        return
    wsid = str(uuid4())
    ws_managers[room_id][wsid] = ws
    try:
        await ws.send_text(json.dumps({"type": "system", "text": f"Connected to {room_id}"}))
        while True:
            data = await ws.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "identify":
                slot = msg.get("slot")
                p = next((x for x in rooms[room_id]["players"] if x["slot"] == slot), None)
                if p:
                    p["ws_id"] = wsid
                    p["is_bot"] = False
                    await ws.send_text(json.dumps({
                        "type": "private_role",
                        "slot": p["slot"],
                        "role": p["role"],
                        "explain": f"Faction: {p['faction']}"
                    }))
                    await broadcast(room_id, {"type": "room", "room": room_summary(rooms[room_id])})
            elif msg.get("type") == "chat":
                await broadcast(room_id, {"type": "chat", "from": msg.get("from","Anon"), "text": msg.get("text","")})
    except WebSocketDisconnect:
        ws_managers[room_id].pop(wsid, None)
    except Exception:
        ws_managers[room_id].pop(wsid, None)

# ----------------------------------------------------------------
# Broadcast helpers
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
    await broadcast(room_id, {"type": "phase", "phase": phase_name, "seconds": seconds})
    await broadcast(room_id, {"type": "room", "room": room_summary(rooms[room_id])})

# ----------------------------------------------------------------
# Phase controller
async def phase_controller(room_id: str):
    room = rooms.get(room_id)
    if not room:
        return
    cycle = 1
    while room["state"] == "active":
        await asyncio.sleep(NIGHT_SECONDS)
        room["phase"] = "day"
        room["day"] += 1
        await broadcast_phase(room_id, f"Day {room['day']}", DAY_SECONDS)
        await asyncio.sleep(DAY_SECONDS)
        cycle += 1
        room["phase"] = "night"
        await broadcast_phase(room_id, f"Night {cycle}", NIGHT_SECONDS)

# ----------------------------------------------------------------
@app.on_event("startup")
async def startup_event():
    if not rooms:
        r = create_room_obj("Host")
        print("Sample room created:", r["id"])
