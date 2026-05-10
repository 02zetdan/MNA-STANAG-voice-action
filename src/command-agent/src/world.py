"""
HTTP client for the b-service world model.

Mirrors the method surface of `MockWorldModel` so the `Assistant` can swap
between the in-process mock (unit tests, offline dev) and the real b-service
(end-to-end demo) by changing one constructor argument.

Key design points:

- **b-service is stateless about pending tasks.** Staging is purely an
  agent-side concept; only the dispatch endpoint ever leaves the service.
  This client therefore keeps an in-process `_pending` dict identical to
  the mock's. The only network behavior difference vs. the mock is that
  `mark_dispatched` POSTs to `/api/v1/dispatch`.

- **Resolution uses `/api/v1/tracks`** to enumerate platforms — the same
  payload shape the operator UI consumes. The same hyphen/space/NATO
  resolver tiers from the mock are reused here so STT garbles match the
  same way against either backend.

- **The seeded fleet may differ.** The agent's mock seeds platforms that
  may not exist in b-service if no sim is running for them. That's a
  feature: in production you only see what the sims actually publish.
  Voice tasks against unknown call signs raise UNKNOWN_CALLSIGN.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx

try:
    from livekit.agents import ToolError  # type: ignore
except ImportError:  # pragma: no cover
    class ToolError(Exception):
        def __init__(self, code: str):
            super().__init__(code)
            self.code = code

# Reuse readback formatters and base coords from the mock so wire wording is
# identical regardless of backend.
from mock_world_model import (
    _OPS_AREA_LAT_MAX,
    _OPS_AREA_LAT_MIN,
    _OPS_AREA_LON_MAX,
    _OPS_AREA_LON_MIN,
    BASE_LATITUDE,
    BASE_LONGITUDE,
    _format_intercept_readback,
    _format_readback,
    _format_recall_readback,
    _normalise_callsign,
    _strip_noise_tokens,
)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


class WorldModel:
    """HTTP-backed mirror of MockWorldModel for end-to-end demos."""

    def __init__(self, base_url: str, *, timeout: float = 5.0) -> None:
        self.base_url = base_url.rstrip("/")
        # Sync client matches MockWorldModel's interface so the Assistant
        # tool wrappers don't need to know which backend they're talking to.
        # b-service is local so blocking I/O latency is negligible.
        self._client = httpx.Client(base_url=self.base_url, timeout=timeout)
        self._pending: dict[str, dict] = {}  # ptid → staged task dict

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _list_envelopes(self) -> list[dict]:
        r = self._client.get("/api/v1/tracks")
        r.raise_for_status()
        envelopes = r.json()
        return [
            e["payload"] for e in envelopes
            if isinstance(e, dict) and "payload" in e and not e["payload"].get("_rejected")
        ]

    def _resolve_payload(self, call_sign: str) -> dict:
        """Return the raw track payload matching `call_sign` using the same
        three-tier resolver as MockWorldModel._resolve."""
        target = _normalise_callsign(call_sign)
        if not target:
            raise ToolError("UNKNOWN_CALLSIGN")
        payloads = self._list_envelopes()

        # Tier 1: exact normalised match
        for p in payloads:
            if _normalise_callsign(p["trackId"]) == target:
                return p

        # Tier 2: NATO-prefix stripping
        target_stripped = _strip_noise_tokens(target)
        if target_stripped and target_stripped != target:
            for p in payloads:
                if _strip_noise_tokens(_normalise_callsign(p["trackId"])) == target_stripped:
                    return p

        # Tier 3: unique suffix match
        target_tokens = target.split()
        if target_tokens:
            suffix = target_tokens[-1]
            candidates = [
                p for p in payloads
                if _normalise_callsign(p["trackId"]).split()[-1] == suffix
            ]
            if len(candidates) == 1:
                return candidates[0]

        raise ToolError("UNKNOWN_CALLSIGN")

    # ------------------------------------------------------------------
    # Query surface
    # ------------------------------------------------------------------

    def list_platforms(self) -> list[dict]:
        payloads = self._list_envelopes()
        return [
            {
                "call_sign": p["trackId"],
                "type": p.get("trackType", "UNKNOWN"),
                "status": "active",  # b-service doesn't track ready/tasked/offline
                "latitude": p["position"]["lat"],
                "longitude": p["position"]["lon"],
                "is_controllable": p.get("isControllable", False),
            }
            for p in payloads
        ]

    def get_platform_state(self, call_sign: str) -> dict:
        p = self._resolve_payload(call_sign)
        return {
            "call_sign": p["trackId"],
            "type": p.get("trackType", "UNKNOWN"),
            "status": "active",
            "latitude": p["position"]["lat"],
            "longitude": p["position"]["lon"],
            "heading": p.get("headingDeg", 0.0),
            "speed": p.get("speedKts", 0.0),
            "current_task": None,
            "is_controllable": p.get("isControllable", False),
        }

    # ------------------------------------------------------------------
    # Staging surface — agent-local pending state, no b-service round-trip
    # ------------------------------------------------------------------

    def _validate_ops_area(self, lat: float, lon: float) -> None:
        if not (-90.0 <= lat <= 90.0) or not (-180.0 <= lon <= 180.0):
            raise ToolError("INVALID_COORDINATE")
        if not (_OPS_AREA_LAT_MIN <= lat <= _OPS_AREA_LAT_MAX) or \
           not (_OPS_AREA_LON_MIN <= lon <= _OPS_AREA_LON_MAX):
            raise ToolError("INVALID_COORDINATE")

    def _stage(self, call_sign: str, latitude: float, longitude: float, readback: str) -> dict:
        ptid = f"pt_{uuid.uuid4().hex[:4]}"
        result = {
            "pending_task_id": ptid,
            "readback": readback,
            "call_sign": call_sign,
            "latitude": latitude,
            "longitude": longitude,
            "_staged_at": _utcnow_iso(),
        }
        self._pending[ptid] = result
        return result

    def task_waypoint(self, call_sign: str, latitude: float, longitude: float) -> dict:
        self._validate_ops_area(latitude, longitude)
        p = self._resolve_payload(call_sign)
        if not p.get("isControllable"):
            raise ToolError("UNKNOWN_CALLSIGN")
        readback = _format_readback(p["trackId"], latitude, longitude)
        out = self._stage(p["trackId"], latitude, longitude, readback)
        return {k: v for k, v in out.items() if k != "_staged_at"}

    def recall_to_base(self, call_sign: str) -> dict:
        p = self._resolve_payload(call_sign)
        if not p.get("isControllable"):
            raise ToolError("UNKNOWN_CALLSIGN")
        readback = _format_recall_readback(p["trackId"])
        out = self._stage(p["trackId"], BASE_LATITUDE, BASE_LONGITUDE, readback)
        return {k: v for k, v in out.items() if k != "_staged_at"}

    def intercept_platform(self, call_sign: str, target_call_sign: str) -> dict:
        actor = self._resolve_payload(call_sign)
        if not actor.get("isControllable"):
            raise ToolError("UNKNOWN_CALLSIGN")
        target = self._resolve_payload(target_call_sign)
        if target["trackId"] == actor["trackId"]:
            raise ToolError("INVALID_TARGET")
        lat = target["position"]["lat"]
        lon = target["position"]["lon"]
        self._validate_ops_area(lat, lon)
        readback = _format_intercept_readback(actor["trackId"], target["trackId"])
        out = self._stage(actor["trackId"], lat, lon, readback)
        return {
            "pending_task_id": out["pending_task_id"],
            "readback": readback,
            "call_sign": actor["trackId"],
            "target_call_sign": target["trackId"],
            "latitude": lat,
            "longitude": lon,
        }

    def get_pending_task(self, pending_task_id: str) -> dict:
        task = self._pending.get(pending_task_id)
        if task is None:
            raise ToolError("UNKNOWN_TASK")
        return {
            "pending_task_id": task["pending_task_id"],
            "readback": task["readback"],
            "call_sign": task["call_sign"],
            "latitude": task["latitude"],
            "longitude": task["longitude"],
            "staged_at": task.get("_staged_at", ""),
        }

    def cancel_pending_task(self, pending_task_id: str) -> dict:
        task = self._pending.pop(pending_task_id, None)
        if task is None:
            raise ToolError("UNKNOWN_TASK")
        return {"cancelled": True, "pending_task_id": pending_task_id}

    # ------------------------------------------------------------------
    # Dispatch — the only path that actually crosses the network on confirm
    # ------------------------------------------------------------------

    def mark_dispatched(self, pending_task_id: str) -> None:
        task = self._pending.pop(pending_task_id, None)
        if task is None:
            return  # idempotent: already dispatched or cancelled
        r = self._client.post("/api/v1/dispatch", json={
            "target_id": task["call_sign"],
            "task_type": "transit_to_waypoint",
            "latitude": task["latitude"],
            "longitude": task["longitude"],
        })
        if r.status_code >= 400:
            # Re-stash so the operator can retry / cancel.
            self._pending[pending_task_id] = task
            raise ToolError("DISPATCH_FAILED")
