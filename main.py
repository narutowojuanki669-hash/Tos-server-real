
# main.py - Town of Shadows (tos_file final)
# Run with: uvicorn main:app --host 0.0.0.0 --port $PORT

import asyncio, json, random, time
from typing import Dict, Any, List, Optional
from uuid import uuid4

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi import Request
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
    if rid not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    room=rooms[rid]
    slot=next((p for p in room["players"] if p["is_bot"]), None)
    if not slot:
        raise HTTPException(status_code=400, detail="Room full")
    slot["is_bot"]=False
    slot["name"]=req.name or slot["name"]
    return {"slot":slot["slot"], "role":slot["role"], "faction":slot["faction"], "room": room_summary(room)}

# HTTP start endpoint to support clients that call HTTP start
@app.post("/start-game/{room_id}")
async def start_game_http(room_id: str, req: Request):
    if room_id not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    await start_game(room_id)
    return {"ok": True}

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
    # send to each connected faction member immediately
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

# start_game - ensures faction visibility is sent after the game becomes active
async def start_game(room_id):
    if room_id not in rooms:
        raise HTTPException(status_code=404, detail="Room not found")
    room = rooms[room_id]
    if room["state"]=="active":
        return {"ok":True}
    room["state"]="active"
    room["day"]=0
    room["phase"]="night"
    # notify players their private roles
    for p in room["players"]:
        if p.get("ws_id"):
            await send_to_player(room_id,p["name"],{"type":"private_role","slot":p["slot"],"role":p["role"],"faction":p["faction"]})
    # send a clear game_started signal (double-send to ensure delivery)
    await broadcast(room_id, {"type":"game_started","text":"Game has started. Night 1 begins."})
    await asyncio.sleep(0.6)
    await broadcast(room_id, {"type":"game_started","text":"Game has started. Night 1 begins. (confirm)"})
    # now send faction mates so client can render roles in-grid after they know game started
    await send_faction_mates(room_id)
    await broadcast(room_id, {"type":"system","text":"Game started. Night 1 begins."})
    # start controller
    if room.get("controller_task") is None or room.get("controller_task").done():
        room["controller_task"]=asyncio.create_task(phase_controller(room_id))
    return {"ok":True}

async def broadcast_phase(room_id, phase_name, seconds):
    room = rooms.get(room_id)
    payload={"type":"phase","phase":phase_name,"seconds":seconds}
    if phase_name=="day_vote":
        payload["players"]=[{"slot":p["slot"],"name":p["name"],"alive":p["alive"]} for p in room["players"]]
    await broadcast(room_id, payload)
    await broadcast(room_id, {"type":"room","room":room_summary(room)})

async def phase_controller(room_id):
    room = rooms.get(room_id)
    if not room: return
    while room["state"]=="active":
        try:
            # Night
            room["phase"]="night"
            await send_faction_mates(room_id)
            await broadcast_phase(room_id,"night",NIGHT_SECONDS)
            asyncio.create_task(simulate_bot_night_actions(room_id))
            await asyncio.sleep(NIGHT_SECONDS)
            await apply_player_actions(room_id)
            await check_victory(room_id)
            if room["state"]!="active": break

            # Day discuss
            room["day"]+=1
            room["phase"]="day_discuss"
            await broadcast_phase(room_id,"day_discuss",DAY_DISCUSS)
            asyncio.create_task(simulate_bot_day_chat(room_id))
            await asyncio.sleep(DAY_DISCUSS)

            # Vote
            room["phase"]="day_vote"
            room["votes"]={}
            await broadcast_phase(room_id,"day_vote",DAY_VOTE)
            asyncio.create_task(simulate_bot_day_votes_and_accusations(room_id))
            await asyncio.sleep(DAY_VOTE)

            await determine_accused(room_id)

            # Defence
            room["phase"]="day_defence"
            await broadcast_phase(room_id,"day_defence",DAY_DEFENCE)
            await asyncio.sleep(DAY_DEFENCE)

            # Final
            if room.get("accused"):
                room["phase"]="day_final"
                room["verdict_votes"]={}
                await broadcast(room_id, {"type":"verdict_phase","accused":room["accused"],"seconds":DAY_FINAL})
                await broadcast_phase(room_id,"day_final",DAY_FINAL)
                asyncio.create_task(simulate_bot_verdict_votes(room_id))
                await asyncio.sleep(DAY_FINAL)
                await resolve_verdict(room_id)
            else:
                await broadcast(room_id, {"type":"system","text":"No accused this day."})
                await asyncio.sleep(DAY_FINAL)
        except Exception as e:
            await broadcast(room_id, {"type":"system","text":f"Phase controller error: {str(e)}"})
            await asyncio.sleep(2)

# Bot and action functions (same as previous stable implementation)
async def simulate_bot_day_chat(room_id):
    room = rooms.get(room_id)
    if not room: return
    alive = [p for p in room["players"] if p["alive"]]
    bots = [p for p in alive if p["is_bot"]]
    if not bots: return
    count = min(len(bots), random.randint(2,4))
    speakers = random.sample(bots, count)
    for i, bot in enumerate(speakers):
        delay = random.randint(6,15) + i*2
        if delay >= DAY_DISCUSS - 2:
            delay = max(1, DAY_DISCUSS - 3 - i)
        asyncio.create_task(bot_say_after(room_id, bot["name"], delay))
    return

async def bot_say_after(room_id, bot_name, delay):
    await asyncio.sleep(delay)
    room = rooms.get(room_id)
    if not room or room["state"]!="active": return
    bot = next((p for p in room["players"] if p["name"]==bot_name), None)
    if not bot or not bot["alive"]: return
    alive = [p for p in room["players"] if p["alive"] and p["name"]!=bot_name]
    if not alive: return
    target = random.choice(alive)
    templates = [
        f"I feel like {target['name']} is acting strange.",
        f"{target['name']} was pretty quiet.",
        f"Why is {target['name']} so defensive?",
        f"Maybe we should skip this time.",
        f"I don't trust {target['name']}.",
        f"{target['name']} seems suspicious."
    ]
    text = random.choice(templates)
    await broadcast(room_id, {"type":"chat","from":bot_name,"text":text,"channel":"public"})

async def simulate_bot_day_votes_and_accusations(room_id):
    room = rooms.get(room_id)
    if not room or room["phase"]!="day_vote": return
    await asyncio.sleep(max(1, DAY_VOTE//3))
    alive = [p for p in room["players"] if p["alive"]]
    bots = [p for p in alive if p["is_bot"]]
    for bot in bots:
        if random.random() < 0.55:
            candidates = [c for c in alive if c["name"]!=bot["name"]]
            if not candidates: continue
            weights = []
            for c in candidates:
                w = 1.0
                if c["faction"] in ("Mafia", "Cult"):
                    w = 2.5
                weights.append((c, w))
            total = sum(w for _,w in weights)
            r = random.random() * total
            upto = 0
            pick = weights[-1][0]
            for c, w in weights:
                upto += w
                if r <= upto:
                    pick = c
                    break
            room.setdefault("votes", {})[bot["name"]] = pick["name"]
            await broadcast(room_id, {"type":"system","text":f"🤖 {bot['name']} voted for {pick['name']}"})

async def simulate_bot_night_actions(room_id):
    room = rooms.get(room_id)
    if not room or room["state"]!="active": return
    await asyncio.sleep(2)
    alive = [p for p in room["players"] if p["alive"]]
    mafia = [p for p in alive if p["faction"]=="Mafia"]
    if mafia:
        candidates = [p for p in alive if p["faction"]!="Mafia"]
        if candidates:
            victim = random.choice(candidates)
            attacker = random.choice(mafia)
            room.setdefault("actions", []).append({"actor":attacker["name"],"target":victim["name"],"type":"mafia_kill","actor_role":attacker["role"]})
            # send only to mafia
            await send_to_faction(room_id, "Mafia", {"type":"system","text":"Mafia selected a target (private)."})
    cults = [p for p in alive if p["faction"]=="Cult"]
    if cults and random.random() < 0.45:
        candidates = [p for p in alive if p["faction"] not in ("Cult","Mafia")]
        if candidates:
            t = random.choice(candidates)
            room.setdefault("actions", []).append({"actor":random.choice(cults)["name"],"target":t["name"],"type":"cult_convert"})
            await send_to_faction(room_id, "Cult", {"type":"system","text":f"Cult attempted to convert {t['name']} (private)."})
    for d in [p for p in alive if p["role"]=="Doctor"]:
        if random.random() < 0.6:
            tgt = random.choice(alive)["name"]
            room.setdefault("actions", []).append({"actor":d["name"],"target":tgt,"type":"doctor_heal"})
            await send_to_player(room_id, d["name"], {"type":"system","text":f"You healed {tgt} tonight."})

# Actions resolution (same logic as previous)
async def apply_player_actions(room_id):
    room = rooms.get(room_id)
    if not room: return
    actions = room.get("actions", [])[:]
    protected = set()
    bodyguard = {}
    kills = []
    converts = []
    contacts = []
    for a in actions:
        t = a.get("type")
        if t == "doctor_heal": protected.add(a.get("target"))
        elif t == "bodyguard_protect": bodyguard[a.get("target")] = a.get("actor")
        elif t in ("mafia_kill", "vigilante_shot", "beast_kill", "serial_kill"):
            kills.append({"victim": a.get("target"), "by": a.get("actor"), "actor_role": a.get("actor_role")})
        elif t == "cult_convert": converts.append({"target": a.get("target"), "by": a.get("actor")})
        elif t == "contact": contacts.append({"actor": a.get("actor"), "target": a.get("target"), "actor_role": a.get("actor_role")})
    for c in contacts:
        actor = next((p for p in room["players"] if p["name"]==c["actor"]), None)
        target = next((p for p in room["players"] if p["name"]==c["target"]), None)
        if not actor or not target: continue
        if c["actor_role"] == "Fanatic":
            if target["faction"] == "Cult":
                target["contacted"] = True
                actor["contacted"] = True
                await send_to_player(room_id, actor["name"], {"type":"system","text":f"You contacted {target['name']}."})
                await send_faction_mates(room_id)
        if c["actor_role"] == "Spy":
            if target["faction"] == "Mafia":
                actor["contacted"] = True
                await send_to_player(room_id, actor["name"], {"type":"system","text":f"You contacted {target['name']} (mafia)."})
                await send_faction_mates(room_id)
            else:
                await send_to_player(room_id, actor["name"], {"type":"system","text":f"You investigated {target['name']}: {target['role']} ({target['faction']})"})
        if c["actor_role"] == "Beastman":
            actor["contacted"] = True
            await send_to_player(room_id, actor["name"], {"type":"system","text":f"You contacted {target['name']}. Beast kill unlocked."})
    for cv in converts:
        tp = next((p for p in room["players"] if p["name"]==cv["target"] and p["alive"]), None)
        if tp and tp["role"] not in ("Godfather","Mafioso","Beastman","Soldier"):
            tp["faction"] = "Cult"
            tp["role"] = "Acolyte"
            tp["culted"] = True
            await send_to_player(room_id, tp["name"], {"type":"system","text":"You were converted to Cult (Acolyte)."})
            await send_faction_mates(room_id)
    for k in kills:
        victim = next((p for p in room["players"] if p["name"]==k["victim"] and p["alive"]), None)
        if not victim: continue
        bypass = (k.get("actor_role") == "Beastman")
        if victim["name"] in protected and not bypass:
            await send_to_player(room_id, victim["name"], {"type":"system","text":"You were attacked but healed."})
            continue
        if victim["name"] in bodyguard and not bypass:
            bg = next((p for p in room["players"] if p["name"]==bodyguard[victim["name"]] and p["alive"]), None)
            if bg:
                bg["alive"] = False
                bg["revealed"] = True
                await broadcast(room_id, {"type":"system","text":f"{bg['name']} died protecting {victim['name']}."})
                continue
        if victim.get("role")=="Soldier" and not victim.get("soldier_used",False):
            if k.get("actor_role")=="Beastman":
                victim["alive"] = False
                victim["revealed"] = True
                await broadcast(room_id, {"type":"system","text":f"{victim['name']} was killed by Beastman."})
            else:
                victim["soldier_used"] = True
                await send_to_player(room_id, victim["name"], {"type":"system","text":"Your Soldier protection activated and you survived."})
            continue
        if k.get("actor_role") == "Vigilante":
            if victim["faction"] != "Mafia":
                await send_to_player(room_id, k["by"], {"type":"system","text":"Your Vigilante shot failed (target not Mafia)."})
                continue
        victim["alive"] = False
        victim["revealed"] = True
        await broadcast(room_id, {"type":"system","text":f"{victim['name']} was killed — {victim['role']} ({victim['faction']})"})
    room["actions"] = []
    await broadcast(room_id, {"type":"room","room":room_summary(room)})
    await send_faction_mates(room_id)
    await check_victory(room_id)

async def determine_accused(room_id):
    room = rooms.get(room_id)
    if not room: return
    votes = room.get("votes",{}) or {}
    if not votes:
        room["accused"] = None
        await broadcast(room_id, {"type":"system","text":"No accusations were made."})
        await broadcast(room_id, {"type":"accused_update","accused":None})
        return
    tally = {}
    for v in votes.values():
        tally[v] = tally.get(v,0) + 1
    sorted_counts = sorted(tally.items(), key=lambda x: x[1], reverse=True)
    if len(sorted_counts) > 1 and sorted_counts[0][1] == sorted_counts[1][1]:
        room["accused"] = None
        await broadcast(room_id, {"type":"system","text":"Tie in accusations — no accused."})
        await broadcast(room_id, {"type":"accused_update","accused":None})
        return
    top = sorted_counts[0][0]
    if top == "SKIP":
        room["accused"] = None
        await broadcast(room_id, {"type":"system","text":"Voting resulted in Skip — no accused."})
        await broadcast(room_id, {"type":"accused_update","accused":None})
        return
    room["accused"] = top
    await broadcast(room_id, {"type":"system","text":f"{top} has been accused and will defend themselves."})
    await broadcast(room_id, {"type":"accused_update","accused":top})

async def resolve_verdict(room_id):
    room = rooms.get(room_id)
    if not room: return
    accused = room.get("accused")
    if not accused: return
    votes = room.get("verdict_votes",{}) or {}
    if not votes:
        await broadcast(room_id, {"type":"system","text":"No verdict votes — no lynch."})
        room["accused"] = None
        await broadcast(room_id, {"type":"accused_update","accused":None})
        return
    tally = {"guilty":0,"innocent":0}
    for v in votes.values():
        tally[v] = tally.get(v,0) + 1
    if tally["guilty"] > tally["innocent"]:
        victim = next((p for p in room["players"] if p["name"]==accused and p["alive"]), None)
        if victim:
            victim["alive"] = False
            victim["revealed"] = True
            await broadcast(room_id, {"type":"system","text":f"{accused} was found GUILTY — {victim['role']} ({victim['faction']})"})
            room["accused"] = None
            room["verdict_votes"] = {}
            await broadcast(room_id, {"type":"room","room":room_summary(room)})
            await check_victory(room_id)
            return
    else:
        await broadcast(room_id, {"type":"system","text":f"{accused} was found INNOCENT."})
    room["accused"] = None
    room["verdict_votes"] = {}
    await broadcast(room_id, {"type":"room","room":room_summary(room)})

async def check_victory(room_id):
    room = rooms.get(room_id)
    if not room: return
    alive = [p for p in room["players"] if p["alive"]]
    mafia = [p for p in alive if p["faction"]=="Mafia"]
    cult = [p for p in alive if p["faction"]=="Cult"]
    town = [p for p in alive if p["faction"]=="Town"]
    neutral = [p for p in alive if p["faction"]=="Neutral"]
    if not mafia and town:
        await end_game(room_id, "Town")
        return
    if not town and len(mafia) >= len(cult):
        await end_game(room_id, "Mafia")
        return
    if len(cult) >= (len(mafia) + len(town) + len(neutral)):
        await end_game(room_id, "Cult")
        return
    if neutral and not mafia and not town and not cult:
        await end_game(room_id, "Neutral")
        return

async def end_game(room_id, winner):
    room = rooms.get(room_id)
    if not room: return
    room["state"] = "ended"
    await broadcast(room_id, {"type":"system","text":f"{winner} win!"})
    recap = []
    for p in room["players"]:
        recap.append(f"{p['name']}: {p['role']} ({p['faction']}) {'Alive' if p['alive'] else 'Dead'}")
    await broadcast(room_id, {"type":"system","text":"Final Roles:\\n" + "\\n".join(recap)})
    await broadcast(room_id, {"type":"room","room":room_summary(room)})

# Startup sample room
@app.on_event("startup")
async def startup_event():
    if not rooms:
        r=create_room("Host")
        print("Sample room created:", r["id"])
