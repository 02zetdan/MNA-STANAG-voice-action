from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import List, Dict, Optional
from datetime import datetime, timezone
import math
import asyncio
import socket
import json

# ==========================================
# 1. LOKALA IMPORTER FRÅN REPOT
# ==========================================
from models import CimTrack, Position, QualityVector
import mapper
import envelope as env

# ==========================================
# 2. GROUNDING API — Modeller för Person C
# ==========================================
class ResolveRequest(BaseModel):
    targetText: str
    operator_lat: float = 56.1608  # Default: Karlskrona
    operator_lon: float = 15.5872
    # When True, only fleet platforms (is_controllable=True) are eligible
    # candidates. Set this for tasking flows; leave False for general queries.
    controllable_only: bool = False

class ResolveResponse(BaseModel):
    resolved_id: str
    resolved_text: str
    confidence: float
    lat: float
    lon: float
    speed_knots: float
    distance_nm: float

class DispatchRequest(BaseModel):
    target_id: str
    task_type: str
    latitude: float
    longitude: float

# ==========================================
# 3. HJÄLPFUNKTIONER (Voice Resolve)
# ==========================================
def _haversine(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Returnerar nautiska mil (nm)"""
    R = 3440.065 
    p1, p2   = math.radians(lat1), math.radians(lat2)
    dp       = math.radians(lat2 - lat1)
    dl       = math.radians(lon2 - lon1)
    a        = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))

def _resolve(text: str, world: Dict[str, CimTrack], op_lat: float, op_lon: float, controllable_only: bool = False) -> Optional[CimTrack]:
    if not world: return None
    tracks = [t for t in world.values() if (not controllable_only or t.is_controllable)]
    if not tracks:
        return None
    t = text.lower().strip()
    words = set(t.split())

    # (1) Exact track ID — filtered set so controllable_only tasking
    # never resolves to an ambient contact even if the call sign collides.
    for track in tracks:
        if t == track.track_id.lower():
            return track

    # (2) Spatial / Metric Intent
    if any(w in words for w in ["north", "norr", "nordlig"]): return max(tracks, key=lambda d: d.position.lat)
    if any(w in words for w in ["south", "söder", "sydlig"]): return min(tracks, key=lambda d: d.position.lat)
    if any(w in words for w in ["east", "öster", "östlig"]): return max(tracks, key=lambda d: d.position.lon)
    if any(w in words for w in ["west", "väster", "västlig"]): return min(tracks, key=lambda d: d.position.lon)
    if any(w in words for w in ["closest", "nearest", "närmast"]): return min(tracks, key=lambda d: _haversine(op_lat, op_lon, d.position.lat, d.position.lon))

    # (3) Tokenized matching (Fixar Substring-felet!)
    for track in tracks:
        track_tokens = set(track.track_id.lower().replace('-', ' ').split())
        if track_tokens.intersection(words):
            return track

    if len(tracks) == 1: return tracks[0]
    return None

# ==========================================
# 3.5. WEBSOCKET MANAGER (Live updates)
# ==========================================
class ConnectionManager:
    def __init__(self):
        self.active_connections: List[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)

    async def broadcast_json(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception:
                pass # Ignorera döda/stängda kopplingar

manager = ConnectionManager()

# ==========================================
# 4. STATE ROUTER (Hjärtat i B-tjänsten)
# ==========================================
class WorldStateEngine:
    def __init__(self):
        self.tracks: Dict[str, CimTrack] = {}

    async def ingest_data(
        self,
        track_id: str,
        lat: Optional[float],
        lon: Optional[float],
        speed_kts: float,
        heading_deg: float,
        source: str,
        incoming_confidence: float = 0.8,
        is_controllable: bool = False,
    ):
        completeness = 1.0
        now = datetime.now(timezone.utc)

        if lat is None or lon is None:
            completeness = 0.5
            incoming_confidence = 0.4
            if track_id in self.tracks:
                lat = self.tracks[track_id].position.lat
                lon = self.tracks[track_id].position.lon
            else:
                return

        if track_id in self.tracks:
            existing_track = self.tracks[track_id]
            age_of_existing = (now - existing_track.ingest_ts).total_seconds()
            if age_of_existing < 5.0 and existing_track.quality.confidence > incoming_confidence:
                return

        track = CimTrack(
            track_id=track_id,
            position=Position(lat=lat, lon=lon),
            speed_kts=speed_kts,
            heading_deg=heading_deg,
            track_type="surface",
            raw_source=source,
            quality=QualityVector(
                completeness=completeness,
                confidence=incoming_confidence,
                staleness_s=0.0,
                source_id=source
            ),
            ingest_ts=now,
            is_controllable=is_controllable,
        )
        self.tracks[track_id] = track

        # --- LIVE WEBSOCKET BROADCAST ---
        # Omedelbar push till Frontend
        mapped = mapper.map_track(track)
        if mapped and not mapped.get("_rejected"):
            enveloped = env.wrap(mapped, "MESSAGE_TYPE_OBSERVATION_REPORT")
            await manager.broadcast_json(enveloped)

    def get_envelopes(self) -> List[dict]:
        now = datetime.now(timezone.utc)
        envelopes = []

        for track in self.tracks.values():
            age_s = (now - track.ingest_ts).total_seconds()
            
            temp_quality = track.quality.model_copy(update={"staleness_s": age_s})
            temp_track = track.model_copy(update={"quality": temp_quality})

            mapped = mapper.map_track(temp_track)
            
            if mapped and not mapped.get("_rejected"):
                enveloped = env.wrap(mapped, "MESSAGE_TYPE_OBSERVATION_REPORT")
                envelopes.append(enveloped)
                
        return envelopes

# ==========================================
# 5. FASTAPI ROUTING
# ==========================================
app = FastAPI(title="Speak to the Fleet - B-Service")
engine = WorldStateEngine()

@app.post("/api/v1/ingest")
async def ingest_endpoint(
    track_id: str,
    lat: Optional[float] = None,
    lon: Optional[float] = None,
    speed_knots: float = 0.0,
    heading_deg: float = 0.0,
    source: str = "NMEA_PCAP",
    confidence: float = 0.8,
    is_controllable: bool = False,
):
    await engine.ingest_data(
        track_id,
        lat,
        lon,
        speed_knots,
        heading_deg,
        source,
        incoming_confidence=confidence,
        is_controllable=is_controllable,
    )
    return {"status": "ingested"}

@app.get("/api/v1/tracks")
def get_tracks_endpoint():
    return engine.get_envelopes()

@app.websocket("/api/v1/ws")
async def websocket_endpoint(websocket: WebSocket):
    """
    Live-feed för Frontend (Person C).
    När en klient ansluter får den först en ögonblicksbild (get_envelopes),
    sedan får den streamade uppdateringar via manager.broadcast_json().
    """
    await manager.connect(websocket)
    try:
        # 1. Skicka initial state så klienten slipper vänta på ny data
        initial_envelopes = engine.get_envelopes()
        for env_msg in initial_envelopes:
            await websocket.send_json(env_msg)
            
        # 2. Håll connection vid liv
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(websocket)

@app.post("/api/v1/resolve", response_model=ResolveResponse)
def resolve_endpoint(req: ResolveRequest):
    if not engine.tracks:
        raise HTTPException(503, "Världsmodellen är tom. Inväntar data...")

    track = _resolve(req.targetText, engine.tracks, req.operator_lat, req.operator_lon, req.controllable_only)
    if not track:
        raise HTTPException(404, f"Kunde inte hitta: {req.targetText}")

    dist_nm = _haversine(req.operator_lat, req.operator_lon, track.position.lat, track.position.lon)
    eff_conf = mapper._effective_confidence(track.quality)

    return ResolveResponse(
        resolved_id=track.track_id,
        resolved_text=f"{track.track_id} (Avstånd: {dist_nm:.1f} nautiska mil)",
        confidence=eff_conf,
        lat=track.position.lat,
        lon=track.position.lon,
        speed_knots=track.speed_kts or 0.0,
        distance_nm=dist_nm
    )

# ==========================================
# 6. DISPATCH (Operator confirmed commands)
# ==========================================
UDP_MULTICAST_IP = "239.1.2.3"
UDP_MULTICAST_PORT = 5000

@app.post("/api/v1/dispatch")
def dispatch_endpoint(req: DispatchRequest):
    target = engine.tracks.get(req.target_id)
    if target is None:
        raise HTTPException(404, f"Unknown target: {req.target_id}")
    if not target.is_controllable:
        # Don't reveal that the contact exists but isn't ours — same UX as unknown.
        raise HTTPException(404, f"Unknown target: {req.target_id}")

    dispatch_msg = {
        "header": {
            "message_type": "MESSAGE_TYPE_COMMAND",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "source": "B_SERVICE_DISPATCH"
        },
        "payload": {
            "target_track_id": req.target_id,
            "command": req.task_type,
            "parameters": {
                "destination": {
                    "lat": req.latitude,
                    "lon": req.longitude
                }
            }
        }
    }
    
    audit_str = f"AUDIT [DISPATCH]: Broadcasting task '{req.task_type}' to '{req.target_id}' at {req.latitude},{req.longitude}"
    print(audit_str)
    
    msg_bytes = json.dumps(dispatch_msg).encode("utf-8")
    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sock.sendto(msg_bytes, (UDP_MULTICAST_IP, UDP_MULTICAST_PORT))
        sock.close()
    except Exception as e:
        print(f"Varning: UDP Broadcast misslyckades ({e})")
        
    return {
        "status": "dispatched",
        "audit": audit_str,
        "message": dispatch_msg
    }

# ==========================================
# 7. PCAP-REPLAY & STATUS
# ==========================================
@app.on_event("startup")
async def start_replay():
    try:
        from pcap_replay import replay_pcap
        asyncio.create_task(replay_pcap(engine))
        print("PCAP Replay auto-startad.")
    except ImportError:
        print(" Varning: Hittade inte pcap_replay.py. Startar utan auto-replay.")

@app.get("/api/v1/replay/status")
def replay_status():
    return {
        "active_tracks": len(engine.tracks),
        "track_ids": list(engine.tracks.keys()),
    }
