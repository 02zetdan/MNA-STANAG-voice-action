import json

from sim_platform import _knots_to_mps, _parse_dispatch, _step

# ---- unit conversions ----

def test_knots_to_mps_one_knot() -> None:
    assert abs(_knots_to_mps(1.0) - 0.514_444) < 1e-5


# ---- _step movement ----

def test_step_with_no_destination_holds_position() -> None:
    new_lat, new_lon, new_dest, new_heading = _step(
        58.0, 15.0, dest=None, speed_kts=6.0, heading_deg=90.0, dt=1.0,
    )
    assert (new_lat, new_lon) == (58.0, 15.0)
    assert new_dest is None
    assert new_heading == 90.0


def test_step_arrives_at_nearby_destination() -> None:
    """Destination ~11m away; one tick at 10kts (~5m/s) over 10s = 51m, way enough."""
    dest = (58.0001, 15.0)  # ~11m north
    new_lat, new_lon, new_dest, _ = _step(
        58.0, 15.0, dest=dest, speed_kts=10.0, heading_deg=0.0, dt=10.0,
    )
    assert new_dest is None  # arrived
    assert (new_lat, new_lon) == dest


def test_step_moves_toward_distant_destination() -> None:
    dest = (58.5, 15.5)
    initial_lat_gap = 58.5 - 58.0
    new_lat, new_lon, new_dest, _ = _step(
        58.0, 15.0, dest=dest, speed_kts=10.0, heading_deg=0.0, dt=1.0,
    )
    assert new_dest == dest  # still en route
    assert (58.5 - new_lat) < initial_lat_gap
    assert new_lon > 15.0


def test_step_updates_heading_toward_destination() -> None:
    """North-east destination should yield heading near 045°."""
    _, _, _, new_heading = _step(
        58.0, 15.0, dest=(58.1, 15.1), speed_kts=10.0, heading_deg=0.0, dt=1.0,
    )
    # NE bearing at 58°N is roughly 062° because longitude degrees are
    # narrower at high latitudes. Just sanity-bound it: between due-east
    # (090°) and due-north (000°/360°).
    assert 0.0 < new_heading < 90.0


def test_step_with_zero_speed_does_not_move() -> None:
    new_lat, new_lon, new_dest, _ = _step(
        58.0, 15.0, dest=(58.5, 15.5), speed_kts=0.0, heading_deg=0.0, dt=10.0,
    )
    assert (new_lat, new_lon) == (58.0, 15.0)
    assert new_dest == (58.5, 15.5)


# ---- _parse_dispatch ----

def _dispatch_msg(target: str, lat: float, lon: float) -> bytes:
    return json.dumps({
        "header": {"message_type": "MESSAGE_TYPE_COMMAND"},
        "payload": {
            "target_track_id": target,
            "command": "transit_to_waypoint",
            "parameters": {"destination": {"lat": lat, "lon": lon}},
        },
    }).encode("utf-8")


def test_parse_dispatch_for_my_callsign_returns_destination() -> None:
    raw = _dispatch_msg("Falcon", 58.25, 15.5)
    assert _parse_dispatch(raw, "Falcon") == (58.25, 15.5)


def test_parse_dispatch_for_other_callsign_returns_none() -> None:
    raw = _dispatch_msg("Raven", 58.25, 15.5)
    assert _parse_dispatch(raw, "Falcon") is None


def test_parse_dispatch_malformed_returns_none() -> None:
    assert _parse_dispatch(b"not json", "Falcon") is None
    assert _parse_dispatch(b"{}", "Falcon") is None
    assert _parse_dispatch(b'{"payload": {}}', "Falcon") is None
    # Has my callsign but no destination structure
    bad = json.dumps({"payload": {"target_track_id": "Falcon"}}).encode()
    assert _parse_dispatch(bad, "Falcon") is None


# ---- ambient_replay.advance ----

def test_ambient_advance_due_north() -> None:
    from ambient_replay import Ambient, advance

    a = Ambient("MV Test", "MV", 58.0, 15.0, heading_deg=0.0, speed_kts=10.0)
    advance(a, dt=60.0)  # one minute at 10 kt
    assert a.lat > 58.0
    assert abs(a.lon - 15.0) < 1e-9


def test_ambient_advance_due_east() -> None:
    from ambient_replay import Ambient, advance

    a = Ambient("MV Test", "MV", 58.0, 15.0, heading_deg=90.0, speed_kts=10.0)
    advance(a, dt=60.0)
    assert abs(a.lat - 58.0) < 1e-9
    assert a.lon > 15.0


def test_ambient_advance_zero_dt_does_not_move() -> None:
    from ambient_replay import Ambient, advance

    a = Ambient("MV Test", "MV", 58.0, 15.0, heading_deg=45.0, speed_kts=10.0)
    advance(a, dt=0.0)
    assert (a.lat, a.lon) == (58.0, 15.0)


def test_ambient_advance_distance_matches_speed() -> None:
    """1 knot * 3600s = 1 nautical mile ≈ 1/60° latitude. Verify magnitude."""
    from ambient_replay import Ambient, advance

    a = Ambient("MV Test", "MV", 58.0, 15.0, heading_deg=0.0, speed_kts=1.0)
    advance(a, dt=3600.0)
    assert abs((a.lat - 58.0) - (1.0 / 60.0)) < 5e-4
