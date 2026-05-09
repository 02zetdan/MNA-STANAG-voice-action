"""
Synthetic ambient-traffic replay for the Speak-to-the-Fleet demo.

Continuously POSTs a handful of ambient surface contacts (commercial / fishing
vessels) to b-service /api/v1/ingest with `is_controllable=false`, so the
operator's map shows real-world traffic alongside the controllable fleet.

The replay is synthetic — generated from constants below — rather than
parsed from PCAP. The captured PCAP under src/kraken_data is a single
stationary GPS source recorded at high frequency, which doesn't make for
useful demo motion. The contact list and starting positions match the
ambient seed in src/command-agent/src/mock_world_model.py so the agent's
view (mock) and the UI's view (b-service) stay consistent on names.

Stdlib only. Usage:

    python ambient_replay.py [--ingest-url http://localhost:8000/api/v1/ingest]
                             [--tick 2.0]
"""

from __future__ import annotations

import argparse
import logging
import math
import sys
import time
from dataclasses import dataclass

from sim_platform import _M_PER_DEG_LAT, _knots_to_mps, _post_ingest

logger = logging.getLogger("ambient")


@dataclass
class Ambient:
    call_sign: str
    type: str
    lat: float
    lon: float
    heading_deg: float  # course over ground
    speed_kts: float
    # Provenance tag forwarded to b-service /api/v1/ingest. Synthetic ambient
    # uses "SIM_AMBIENT"; the moored MV Stumholmen entry uses "PCAP_REPLAY"
    # to mark that its position came from the captured NMEA log.
    source: str = "SIM_AMBIENT"


# Mirrors the ambient seed in src/command-agent/src/mock_world_model.py
# (MV Northern Star, FV Karlsvik, MV Stumholmen) plus one extra moving
# contact for visual density. Operating area: Karlskrona, ~56.16°N 15.59°E.
DEFAULT_AMBIENT: list[Ambient] = [
    Ambient("MV Northern Star", "MV", 56.2100, 15.6200, heading_deg=205.0, speed_kts=11.8),
    Ambient("FV Karlsvik",      "FV", 56.0500, 15.5800, heading_deg=15.0,  speed_kts=4.7),
    Ambient("MV Östersjön",     "MV", 56.1600, 15.8500, heading_deg=270.0, speed_kts=13.2),
    # Moored vessel — coordinates lifted byte-for-byte from the captured PCAP
    # at src/kraken_data/multicast_AdvNavData-2026-05-08_07-40-14_capture.pcap
    # (284 GPS fixes, sub-2-meter variance, 56.16080°N 15.56722°E).
    Ambient("MV Stumholmen",    "MV", 56.16080495, 15.56721734,
            heading_deg=0.0, speed_kts=0.0, source="PCAP_REPLAY"),
]


def advance(a: Ambient, dt: float) -> None:
    """Move `a` forward on its current heading by `speed_kts * dt`. In-place.

    Pure flat-earth approximation; good enough for ~10 km of demo motion.
    """
    step_m = _knots_to_mps(a.speed_kts) * dt
    bearing_rad = math.radians(a.heading_deg)
    dy = step_m * math.cos(bearing_rad)
    dx = step_m * math.sin(bearing_rad)
    a.lat += dy / _M_PER_DEG_LAT
    a.lon += dx / (_M_PER_DEG_LAT * math.cos(math.radians(a.lat)))


def main() -> int:
    p = argparse.ArgumentParser(description="Ambient-traffic replay.")
    p.add_argument("--ingest-url", default="http://localhost:8000/api/v1/ingest")
    p.add_argument("--tick", type=float, default=2.0,
                   help="Tick interval in seconds (default 2.0).")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[ambient] %(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    ambient = list(DEFAULT_AMBIENT)
    logger.info(
        "publishing %d ambient contact(s) to %s every %.1fs",
        len(ambient), args.ingest_url, args.tick,
    )

    while True:
        for a in ambient:
            advance(a, args.tick)
            _post_ingest(
                args.ingest_url,
                call_sign=a.call_sign,
                lat=a.lat, lon=a.lon,
                speed_kts=a.speed_kts, heading_deg=a.heading_deg,
                is_controllable=False,
                source=a.source,
            )
        time.sleep(args.tick)


if __name__ == "__main__":
    sys.exit(main() or 0)
