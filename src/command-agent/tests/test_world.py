"""Tests for the b-service-backed WorldModel HTTP client.

Uses httpx.MockTransport to stub the /api/v1/tracks and /api/v1/dispatch
responses so the tests don't need a running b-service.
"""

import json

import httpx
import pytest
from livekit.agents import ToolError

from world import WorldModel


def _track_envelope(track_id: str, lat: float, lon: float, *,
                    is_controllable: bool = True, source: str = "SIM",
                    speed: float = 0.0, heading: float = 0.0) -> dict:
    """Mirror b-service envelope shape (mapper + envelope.wrap)."""
    return {
        "version": 1,
        "type": "MESSAGE_TYPE_OBSERVATION_REPORT",
        "payload": {
            "_rejected": False,
            "type": "CATL.TrackUpdate",
            "trackId": track_id,
            "trackType": "UUV" if is_controllable else "MV",
            "isControllable": is_controllable,
            "position": {"lat": lat, "lon": lon},
            "speedKts": speed,
            "headingDeg": heading,
            "certainty": "CONFIRMED",
            "rawSource": source,
        },
    }


def _make_world(*tracks: dict, dispatch_status: int = 200) -> WorldModel:
    """Construct a WorldModel whose HTTP client returns the given tracks."""
    dispatched: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/tracks":
            return httpx.Response(200, json=list(tracks))
        if request.url.path == "/api/v1/dispatch":
            dispatched.append(json.loads(request.content))
            if dispatch_status >= 400:
                return httpx.Response(dispatch_status, text="refused")
            return httpx.Response(200, json={"status": "dispatched"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    w = WorldModel("http://test")
    w._client.close()
    w._client = httpx.Client(transport=transport, base_url="http://test")
    w._dispatched_messages = dispatched  # test-only handle
    return w


# ----------------------------------------------------------------------
# Query surface
# ----------------------------------------------------------------------

def test_list_platforms_returns_payloads_with_is_controllable() -> None:
    w = _make_world(
        _track_envelope("Falcon", 56.13, 15.50, is_controllable=True),
        _track_envelope("MV Ambient", 56.20, 15.60, is_controllable=False, source="PCAP_REPLAY"),
    )
    rows = w.list_platforms()
    assert len(rows) == 2
    fleet = next(r for r in rows if r["call_sign"] == "Falcon")
    assert fleet["is_controllable"] is True
    ambient = next(r for r in rows if r["call_sign"] == "MV Ambient")
    assert ambient["is_controllable"] is False


def test_get_platform_state_resolves_canonical_name() -> None:
    w = _make_world(_track_envelope("Falcon", 56.135, 15.50))
    s = w.get_platform_state("falcon")  # case-insensitive
    assert s["call_sign"] == "Falcon"
    assert s["latitude"] == 56.135


def test_resolver_unknown_callsign_raises() -> None:
    w = _make_world(_track_envelope("Falcon", 56.13, 15.50))
    with pytest.raises(ToolError) as exc:
        w.get_platform_state("Phantom")
    assert str(exc.value) == "UNKNOWN_CALLSIGN"


# ----------------------------------------------------------------------
# Staging surface
# ----------------------------------------------------------------------

def test_task_waypoint_stages_locally() -> None:
    w = _make_world(_track_envelope("Raven", 56.17, 15.65))
    result = w.task_waypoint("Raven", 56.15, 15.58)
    assert result["call_sign"] == "Raven"
    assert result["pending_task_id"] in w._pending
    assert "Raven" in result["readback"]


def test_task_waypoint_refuses_ambient_with_unknown_callsign() -> None:
    w = _make_world(_track_envelope("MV Ambient", 56.20, 15.60, is_controllable=False))
    with pytest.raises(ToolError) as exc:
        w.task_waypoint("MV Ambient", 56.15, 15.58)
    assert str(exc.value) == "UNKNOWN_CALLSIGN"


def test_task_waypoint_refuses_outside_ops_area() -> None:
    w = _make_world(_track_envelope("Raven", 56.17, 15.65))
    with pytest.raises(ToolError) as exc:
        w.task_waypoint("Raven", 56.121, 6.701)  # north sea
    assert str(exc.value) == "INVALID_COORDINATE"


def test_recall_to_base_stages_to_known_base() -> None:
    from mock_world_model import BASE_LATITUDE, BASE_LONGITUDE
    w = _make_world(_track_envelope("Raven", 56.17, 15.65))
    r = w.recall_to_base("Raven")
    assert r["latitude"] == BASE_LATITUDE
    assert r["longitude"] == BASE_LONGITUDE
    assert "recall to base" in r["readback"].lower()


def test_intercept_snapshots_target_position() -> None:
    w = _make_world(
        _track_envelope("Raven", 56.17, 15.65),
        _track_envelope("Falcon", 56.135, 15.50),
    )
    r = w.intercept_platform("Raven", "Falcon")
    assert r["call_sign"] == "Raven"
    assert r["target_call_sign"] == "Falcon"
    assert r["latitude"] == 56.135
    assert r["longitude"] == 15.50


def test_intercept_refuses_self_target() -> None:
    w = _make_world(_track_envelope("Raven", 56.17, 15.65))
    with pytest.raises(ToolError) as exc:
        w.intercept_platform("Raven", "Raven")
    assert str(exc.value) == "INVALID_TARGET"


# ----------------------------------------------------------------------
# Dispatch
# ----------------------------------------------------------------------

def test_mark_dispatched_posts_to_b_service() -> None:
    w = _make_world(_track_envelope("Raven", 56.17, 15.65))
    staged = w.task_waypoint("Raven", 56.15, 15.58)
    ptid = staged["pending_task_id"]

    w.mark_dispatched(ptid)

    assert len(w._dispatched_messages) == 1
    msg = w._dispatched_messages[0]
    assert msg["target_id"] == "Raven"
    assert msg["latitude"] == 56.15
    assert msg["longitude"] == 15.58
    # Pending dropped after successful dispatch
    assert ptid not in w._pending


def test_mark_dispatched_failure_re_stashes_pending() -> None:
    w = _make_world(_track_envelope("Raven", 56.17, 15.65), dispatch_status=503)
    staged = w.task_waypoint("Raven", 56.15, 15.58)
    ptid = staged["pending_task_id"]

    with pytest.raises(ToolError) as exc:
        w.mark_dispatched(ptid)
    assert str(exc.value) == "DISPATCH_FAILED"
    # On failure the staged task is restored so the operator can retry.
    assert ptid in w._pending


def test_mark_dispatched_idempotent_for_unknown_id() -> None:
    """Calling mark_dispatched with an unknown ptid is a silent no-op
    (matches MockWorldModel behavior — supports double-tap safety)."""
    w = _make_world()
    w.mark_dispatched("pt_nope")  # should not raise
    assert w._dispatched_messages == []


def test_cancel_pending_drops_local_state() -> None:
    w = _make_world(_track_envelope("Raven", 56.17, 15.65))
    staged = w.task_waypoint("Raven", 56.15, 15.58)
    w.cancel_pending_task(staged["pending_task_id"])
    assert staged["pending_task_id"] not in w._pending


def test_cancel_unknown_pending_raises() -> None:
    w = _make_world()
    with pytest.raises(ToolError) as exc:
        w.cancel_pending_task("pt_nope")
    assert str(exc.value) == "UNKNOWN_TASK"
