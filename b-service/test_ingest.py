import requests
import json
import time

url = "http://127.0.0.1:8000/api/v1/ingest"

# The exact JSON structure Person A's `build_track` produces
payload = {
  "trackId": "TRK-20260508-1",
  "sentenceType": "RMC",
  "type": "SURFACE",
  "source": "NMEA_GNSS",
  "timestamp": "2026-05-08T07:40:14Z",
  "position": {
    "lat": 56.1608,
    "lon": 15.5672
  },
  "velocity": {
    "speedKnots": 12.5,
    "courseOverGround": 45.2,
    "trueHeading": 45.0
  },
  "pntConfidence": 0.85,
  "gnssStatus": "A"
}

print(f"Skickar test-JSON till {url}...")
try:
    response = requests.post(url, json=payload, timeout=2.0)
    print(f"Statuskod: {response.status_code}")
    print(f"Svar: {response.text}")
except Exception as e:
    print(f"Krasch/Nätverksfel: {e}")
