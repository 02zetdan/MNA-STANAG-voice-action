import argparse
import json
import os
import socket
import struct

import pynmea2
import requests

def create_multicast_socket(group, port, iface="0.0.0.0"):
    """Open a multicast receive socket bound to (port) joining (group).

    `iface` selects the local interface on which to join — defaults to
    INADDR_ANY (let the kernel pick, typically the default route).
    Pass "127.0.0.1" for single-host setups where the producer is also
    pinned to loopback.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    membership = socket.inet_aton(group) + socket.inet_aton(iface)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, membership)
    return sock

def parse_sentence(raw):
    try:
        return pynmea2.parse(raw)
    except pynmea2.ParseError as e:
        print(f"[WARN] Could not parse: {raw!r} — {e}\n")
        return None

def assess_quality(rmc):
    if rmc.status == "V":
        return 0.0
    score = 1.0
    hdop = getattr(rmc, "horizontal_dil", None)
    if hdop:
        hdop = float(hdop)
        if hdop > 2.0:
            score -= 0.3
        elif hdop > 1.0:
            score -= 0.1
    return round(score, 2)

def build_track(rmc, hdt=None):
    confidence = assess_quality(rmc)
    return {
        "trackId":       f"TRK-{rmc.timestamp}",
        "sentenceType":  "RMC+HDT" if hdt else "RMC",
        "type":          "SURFACE",
        "source":        "NMEA_GNSS",
        "timestamp":     rmc.datetime.isoformat() if rmc.datetime else None,
        "position": {
            "lat": rmc.latitude  if rmc.lat != None else None,
            "lon": rmc.longitude if rmc.lon != None else None,
        },
        "velocity": {
            "speedKnots":       float(rmc.spd_over_grnd) if rmc.spd_over_grnd != None else None,
            "courseOverGround": float(rmc.true_course)   if rmc.true_course != None else None,
            "trueHeading":      float(hdt.heading)       if hdt and hdt.heading != None else None,
        },
        "pntConfidence": confidence,
        "gnssStatus":    rmc.status,
    }

def send_to_b_service(rmc, heading, gga, *, b_service_url, track_id, source,
                      is_controllable=True):
    """POST a parsed NMEA fix to b-service /api/v1/ingest.

    Field names match the IngestPayload Pydantic model on b-service.
    Sends JSON body (the legacy query-param shape was replaced when ingest
    moved to a typed payload).
    """
    url = f"{b_service_url}/api/v1/ingest"

    speed = float(rmc.spd_over_grnd) if getattr(rmc, 'spd_over_grnd', None) else 0.0
    hdg = float(heading.heading) if heading and getattr(heading, 'heading', None) else 0.0

    payload = {
        "track_id": track_id,
        "speed_knots": speed,
        "heading_deg": hdg,
        "source": source,
        "is_controllable": is_controllable,
    }

    # GNSS Denied logik
    if rmc.status == 'A' and rmc.latitude and rmc.longitude:
        payload["lat"] = rmc.latitude
        payload["lon"] = rmc.longitude

    # SIGNALBEHANDLING: Extrahera MSDF-parametrar från GGA
    if gga:
        if getattr(gga, 'horizontal_dil', None): payload["hdop"] = float(gga.horizontal_dil)
        if getattr(gga, 'gps_qual', None): payload["gga_fix"] = int(gga.gps_qual)
        if getattr(gga, 'num_sats', None): payload["gga_sats"] = int(gga.num_sats)

    try:
        requests.post(url, json=payload, timeout=1.0)
    except Exception:
        pass  # Tyst fail så vi inte spammar terminalen om B-tjänsten startar om

def process_live(group, port, *, b_service_url, track_id, source,
                 is_controllable=True, verbose=False, iface="0.0.0.0"):
    sock = create_multicast_socket(group, port, iface=iface)
    print(f"[✓] Listening on multicast {group}:{port} via {iface}, "
          f"ingesting to {b_service_url}")
    print(f"    track_id={track_id} source={source} is_controllable={is_controllable}")

    last_hdt    = None
    last_gga    = None
    track_count = 0

    while True:
        raw_bytes, _ = sock.recvfrom(4096)
        try:
            raw = raw_bytes.decode("ascii").strip().rstrip("\x00")
        except UnicodeDecodeError:
            continue

        if not raw.startswith("$"): continue

        sentence_id = raw[3:6]
        if sentence_id not in ("HDT", "RMC", "GGA"):
            continue

        msg = parse_sentence(raw)
        if msg is None: continue

        if isinstance(msg, pynmea2.HDT):
            last_hdt = msg
        elif isinstance(msg, pynmea2.GGA):
            last_gga = msg
        elif isinstance(msg, pynmea2.RMC):
            track = build_track(msg, hdt=last_hdt)
            track_count += 1
            if verbose:
                print(json.dumps(track, indent=2, default=str))
            else:
                print(f"[{track_count}] {track_id} {track['position']['lat']},"
                      f"{track['position']['lon']} conf={track['pntConfidence']}")
            send_to_b_service(
                msg, last_hdt, last_gga,
                b_service_url=b_service_url,
                track_id=track_id,
                source=source,
                is_controllable=is_controllable,
            )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="NMEA multicast → b-service ingest.")
    p.add_argument("--multicast-group", default="239.192.43.79")
    p.add_argument("--multicast-port", type=int, default=4379)
    p.add_argument("--b-service-url",
                   default=os.environ.get("WORLD_MODEL_URL", "http://localhost:8000"))
    p.add_argument("--track-id", default="MV-KRAKEN-01",
                   help="Call sign emitted to b-service (default: ambient ship MV-KRAKEN-01).")
    p.add_argument("--source", default="NMEA_PCAP")
    p.add_argument("--controllable", action="store_true",
                   help="Mark the track as a controllable fleet platform "
                        "(default: ambient).")
    p.add_argument("--iface", default="127.0.0.1",
                   help="Local interface on which to join the multicast "
                        "group. Default: 127.0.0.1 for single-host demos. "
                        "Use 0.0.0.0 to let the kernel pick.")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    process_live(
        args.multicast_group, args.multicast_port,
        b_service_url=args.b_service_url,
        track_id=args.track_id,
        source=args.source,
        is_controllable=args.controllable,
        verbose=args.verbose,
        iface=args.iface,
    )