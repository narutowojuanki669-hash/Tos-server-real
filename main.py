from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
import asyncio
import random
import json

app = FastAPI()

# ✅ Allow your frontend & backend to talk
origins = [
    "https://690a8382d00a2311478c4251--celebrated-bonbon-1fd3cf.netlify.app",
    "https://town-of-shadows-server.onrender.com"
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# ------------------ MODELS ------------------ #
class JoinRequest(BaseModel):
    roomId: str
    name: Optional[str] = "Player"

class ActionRequest(BaseModel):
    roomId: str
    slot: int
    action: dict

# ------------------ STORAGE ------------------ #
ROOMS = {}

def create_room(rid):
    return {
        "id": rid,
        "players": [],
        "phase": "night",
        "day": 0,
        "timer": None,
        "chat": [],
        "started": False
    }

# ------------------ BASIC ROUTES ------------------ #
@app.get("/test")
def test():
    return {"message": "Hello from Town of Shadows Backend"}

@app.post("/join-room")
def join_room(req: JoinRequest):
    rid = req.roomId
    name = req.name
    if rid not in ROOMS:
        ROOMS[rid] = create_room(rid)
    room = ROOMS[rid]
    player_slot = len(room["players"])
    player = {
        "name": name,
        "slot": player_slot,
        "alive": True,
        "faction": "town",
        "role": None,
        "cooldown": 0,
        "connection": None
    }
    room["players"].append(player)
    return {"slot": player_slot, "room": rid}

@app.post("/player-action")
def player_action(req: ActionRequest):
    rid = req.roomId
    slot = req.slot
    action = req.action
    return {"status": "success", "message": f"Player {slot} used {action}"}

@app.post("/start-game/{rid}")
async def start_game(rid: str):
    """Start a room game"""
    if rid not in ROOMS:
        return {"error": "Room not found"}
    room = ROOMS[rid]
    if room["started"]:
        return {"error": "Game already started"}
    room["started"] = True
    room["phase"] = "night"
    room["day"] = 0
    asyncio.create_task(game_loop(rid))
    return {"status": "started", "room": rid}

# ------------------ WEBSOCKET ------------------ #
@app.websocket("/ws/{rid}/{slot}")
async def websocket_endpoint(websocket: WebSocket, rid: str, slot: int):
    await websocket.accept()
    if rid not in ROOMS:
        ROOMS[rid] = create_room(rid)
    room = ROOMS[rid]
    if slot >= len(room["players"]):
        await websocket.close()
        return
    room["players"][slot]["connection"] = websocket
    await websocket.send_json({"type": "private_role", "role": "Detective"})
    try:
        while True:
            data = await websocket.receive_text()
            msg = json.loads(data)
            if msg.get("type") == "chat":
                room["chat"].append(msg)
                await broadcast(room, msg)
    except WebSocketDisconnect:
        room["players"][slot]["connection"] = None

async def broadcast(room, message):
    for p in room["players"]:
        ws = p.get("connection")
        if ws:
            try:
                await ws.send_json(message)
            except:
                pass

# ------------------ GAME LOOP ------------------ #
async def game_loop(rid):
    """Core timer-driven phase control"""
    room = ROOMS[rid]
    while True:
        if not room["started"]:
            break
        if room["phase"] == "night":
            await start_night_phase(room)
            room["phase"] = "day"
        else:
            await start_day_phase(room)
            room["phase"] = "night"

async def start_day_phase(room):
    room["day"] += 1
    await broadcast(room, {"type": "phase", "phase": f"Day {room['day']}", "time": 60})
    await run_phase_timer(room, "discussion", 60)
    await broadcast(room, {"type": "phase", "phase": "Voting", "time": 20})
    await run_phase_timer(room, "voting", 20)
    await broadcast(room, {"type": "phase", "phase": "Defence", "time": 10})
    await run_phase_timer(room, "defence", 10)
    await broadcast(room, {"type": "phase", "phase": "Verdict", "time": 10})
    await run_phase_timer(room, "verdict", 10)
    await broadcast(room, {"type": "phase", "phase": "Night incoming", "time": 5})
    await asyncio.sleep(5)

async def start_night_phase(room):
    await broadcast(room, {"type": "phase", "phase": "Night", "time": 40})
    await run_phase_timer(room, "night", 40)
    await broadcast(room, {"type": "phase", "phase": "Daybreak", "time": 3})
    await asyncio.sleep(3)

async def run_phase_timer(room, label, seconds):
    """Counts down and informs players"""
    remaining = seconds
    while remaining > 0:
        if remaining % 10 == 0 or remaining <= 5:
            await broadcast(room, {
                "type": "timer",
                "phase": label,
                "remaining": remaining
            })
        await asyncio.sleep(1)
        remaining -= 1

# ------------------ ROLE ACTIONS ------------------ #
def resolve_doctor(target, attacks):
    """Doctor's heal logic with beastman exception"""
    if target in attacks and "beastman" not in attacks[target]:
        del attacks[target]
    return attacks

def resolve_vigilante(vig_target, target_faction):
    """Vigilante can only kill Mafia"""
    if target_faction == "mafia":
        return True
    return False
