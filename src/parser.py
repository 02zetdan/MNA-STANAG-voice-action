import socket
import struct
import pynmea2
import json

def create_multicast_socket(group, port):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", port))
    membership = struct.pack("4sL", socket.inet_aton(group), socket.INADDR_ANY)
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

def process_live(group, port):
    sock = create_multicast_socket(group, port)
    print(f"[✓] Listening on multicast {group}:{port}")

    last_hdt    = None
    track_count = 0

    while True:
        raw_bytes, _ = sock.recvfrom(4096)
        # print(f"[DEBUG] Received {len(raw_bytes)} bytes: {raw_bytes[:50]}")

        try:
            raw = raw_bytes.decode("ascii").strip().rstrip("\x00")
        except UnicodeDecodeError:
            continue

        if not raw.startswith("$"):
            continue

        sentence_id = raw[3:6]
        if sentence_id not in ("HDT", "RMC"):
            continue

        msg = parse_sentence(raw)
        if msg is None:
            continue

        if isinstance(msg, pynmea2.HDT):
            last_hdt = msg
        elif isinstance(msg, pynmea2.RMC):
            track = build_track(msg, hdt=last_hdt)
            track_count += 1
            print(json.dumps(track, indent=2, default=str))

    # Run code
if __name__ == "__main__":      
    process_live("239.192.43.79", 4379)
