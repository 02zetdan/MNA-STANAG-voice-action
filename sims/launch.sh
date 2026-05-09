#!/usr/bin/env bash
# launch.sh — start the demo fleet of sim platforms in the background.
#
# The platforms publish their initial position to b-service on first tick,
# so the world model populates within ~1 second. Tasking commands arriving
# on the dispatch multicast (239.1.2.3:5000 by default) drive movement.
#
# Override INGEST_URL via env if b-service is not on localhost:8000.
# Ctrl-C kills all child sims.

set -e
cd "$(dirname "$0")"

INGEST_URL="${INGEST_URL:-http://localhost:8000/api/v1/ingest}"

trap 'kill 0' INT TERM

python sim_platform.py --call-sign UUV-Alpha --lat 56.1350 --lon 15.5000 --speed 4.0 --ingest-url "$INGEST_URL" &
python sim_platform.py --call-sign UUV-Bravo --lat 56.1700 --lon 15.6500 --speed 5.0 --ingest-url "$INGEST_URL" &
python sim_platform.py --call-sign USV-Echo  --lat 56.1200 --lon 15.7000 --speed 8.0 --ingest-url "$INGEST_URL" &

# Ambient surface traffic (MV / FV vessels) — visible on the map but never
# tasking targets. Each POST carries is_controllable=false.
python ambient_replay.py --ingest-url "$INGEST_URL" &

wait
