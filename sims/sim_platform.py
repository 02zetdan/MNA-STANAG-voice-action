"""
Minimal simulated UUV/USV process for the Speak-to-the-Fleet demo.

Subscribes to the world model's dispatch multicast for tasking commands
addressed to this platform's call sign; transits toward the staged waypoint
at a fixed cruise speed; publishes its own position to the world model's
ingest endpoint at a fixed rate so the map UI can render it moving.

Stdlib only (socket, urllib, threading) so it can run anywhere Python runs
without project dependencies.

Usage:
    python sim_platform.py --call-sign UUV-Alpha --lat 56.1350 --lon 15.5000 \\
        [--speed 6.0] [--ingest-url http://localhost:8000/api/v1/ingest] \\
        [--mcast-group 239.1.2.3] [--mcast-port 5000] [--tick 1.0]
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import socket
import struct
import sys
import threading
import time
import urllib.parse
import urllib.request
from typing import Optional, Tuple

logger = logging.getLogger("sim")

# Approx metres per degree of latitude (constant); per-degree of longitude is
# scaled by cos(lat). Good enough for ~10 km at 58° N.
_M_PER_DEG_LAT = 111_320.0


def _knots_to_mps(knots: float) -> float:
    return knots * 0.514_444


def _step(
    lat: float,
    lon: float,
    dest: Optional[Tuple[float, float]],
    speed_kts: float,
    heading_deg: float,
    dt: float,
) -> Tuple[float, float, Optional[Tuple[float, float]], float]:
    """Advance one tick. Returns (new_lat, new_lon, new_dest, new_heading).

    Pure function — no I/O, no clock — so it can be unit-tested directly.
    `new_dest` is `None` when the platform has arrived (within a one-tick step).
    """
    if dest is None or speed_kts <= 0.0:
        return lat, lon, dest, heading_deg

    dest_lat, dest_lon = dest
    # Local flat-earth east/north components in metres.
    dx = (dest_lon - lon) * _M_PER_DEG_LAT * math.cos(math.radians(lat))
    dy = (dest_lat - lat) * _M_PER_DEG_LAT
    dist_m = math.hypot(dx, dy)
    step_m = _knots_to_mps(speed_kts) * dt

    if dist_m <= step_m:
        return dest_lat, dest_lon, None, heading_deg

    step_x = dx / dist_m * step_m
    step_y = dy / dist_m * step_m
    new_lon = lon + step_x / (_M_PER_DEG_LAT * math.cos(math.radians(lat)))
    new_lat = lat + step_y / _M_PER_DEG_LAT
    new_heading = (math.degrees(math.atan2(step_x, step_y)) + 360.0) % 360.0
    return new_lat, new_lon, dest, new_heading


def _parse_dispatch(raw: bytes, my_call_sign: str) -> Optional[Tuple[float, float]]:
    """Parse a dispatch JSON; return (lat, lon) if it's for me, else None.

    Expected shape (matches b-service /api/v1/dispatch wire format):
        {
          "header": {...},
          "payload": {
            "target_track_id": "<call_sign>",
            "command": "transit_to_waypoint",
            "parameters": {"destination": {"lat": ..., "lon": ...}}
          }
        }
    """
    try:
        msg = json.loads(raw.decode("utf-8"))
        payload = msg.get("payload", {})
        if payload.get("target_track_id") != my_call_sign:
            return None
        dest = payload["parameters"]["destination"]
        return float(dest["lat"]), float(dest["lon"])
    except (ValueError, KeyError, TypeError):
        return None


def _open_multicast(group: str, port: int) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    mreq = struct.pack("4sl", socket.inet_aton(group), socket.INADDR_ANY)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)
    return sock


def _post_ingest(
    ingest_url: str,
    *,
    call_sign: str,
    lat: float,
    lon: float,
    speed_kts: float,
    heading_deg: float,
    is_controllable: bool,
    source: str = "SIM",
) -> None:
    params = urllib.parse.urlencode({
        "track_id": call_sign,
        "lat": f"{lat:.6f}",
        "lon": f"{lon:.6f}",
        "speed_knots": f"{speed_kts:.2f}",
        "heading_deg": f"{heading_deg:.1f}",
        "source": source,
        "is_controllable": "true" if is_controllable else "false",
    })
    req = urllib.request.Request(f"{ingest_url}?{params}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=2.0) as r:
            r.read()
    except OSError as e:
        logger.warning("ingest POST failed: %s", e)


def main() -> int:
    p = argparse.ArgumentParser(description="Simulated fleet platform.")
    p.add_argument("--call-sign", required=True)
    p.add_argument("--lat", type=float, required=True)
    p.add_argument("--lon", type=float, required=True)
    p.add_argument("--speed", type=float, default=6.0, help="Cruise speed in knots.")
    p.add_argument("--ingest-url", default="http://localhost:8000/api/v1/ingest")
    p.add_argument("--mcast-group", default="239.1.2.3")
    p.add_argument("--mcast-port", type=int, default=5000)
    p.add_argument("--tick", type=float, default=1.0, help="Tick interval in seconds.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format=f"[{args.call_sign}] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    state = {
        "lat": args.lat,
        "lon": args.lon,
        "dest": None,
        "heading": 0.0,
    }
    lock = threading.Lock()

    sock = _open_multicast(args.mcast_group, args.mcast_port)
    logger.info(
        "listening on %s:%d, ingest=%s, speed=%.1fkts",
        args.mcast_group, args.mcast_port, args.ingest_url, args.speed,
    )

    def listen_loop() -> None:
        while True:
            try:
                raw, _addr = sock.recvfrom(8192)
            except OSError:
                continue
            dest = _parse_dispatch(raw, args.call_sign)
            if dest is None:
                continue
            with lock:
                state["dest"] = dest
            logger.info("tasking received → dest=(%.4f, %.4f)", *dest)

    threading.Thread(target=listen_loop, daemon=True).start()

    while True:
        with lock:
            lat, lon, dest, heading = (
                state["lat"], state["lon"], state["dest"], state["heading"],
            )
        new_lat, new_lon, new_dest, new_heading = _step(
            lat, lon, dest, args.speed, heading, args.tick,
        )
        active_speed = args.speed if dest is not None else 0.0
        with lock:
            state["lat"], state["lon"], state["dest"], state["heading"] = (
                new_lat, new_lon, new_dest, new_heading,
            )
        _post_ingest(
            args.ingest_url,
            call_sign=args.call_sign,
            lat=new_lat, lon=new_lon,
            speed_kts=active_speed, heading_deg=new_heading,
            is_controllable=True,
        )
        if dest is not None and new_dest is None:
            logger.info("arrived at (%.4f, %.4f)", new_lat, new_lon)
        time.sleep(args.tick)


if __name__ == "__main__":
    sys.exit(main() or 0)
