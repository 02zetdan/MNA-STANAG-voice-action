# ============================================================================
# Mock world model — Baltic scenario, ~58°N 15-16°E
# Three UUVs, two USVs. UUV Alpha tasked, USV Delta offline.
# ============================================================================

MOCK_PLATFORMS = {
    "UUV Alpha": {
        "call_sign": "UUV Alpha",
        "type": "UUV",
        "status": "tasked",
        "latitude": 58.2431,
        "longitude": 15.4892,
        "heading": 87.0,
        "speed": 4.2,
        "current_task": {
            "task_id": "tk_a91f",
            "task_type": "transit_to_waypoint",
            "latitude": 58.25,
            "longitude": 15.5,
            "dispatched_at": "2026-05-08T09:14:22Z",
        },
    },
    "UUV Bravo": {
        "call_sign": "UUV Bravo",
        "type": "UUV",
        "status": "ready",
        "latitude": 58.3102,
        "longitude": 15.4218,
        "heading": 90.0,
        "speed": 0.0,
        "current_task": None,
    },
    "UUV Charlie": {
        "call_sign": "UUV Charlie",
        "type": "UUV",
        "status": "ready",
        "latitude": 58.1876,
        "longitude": 15.6033,
        "heading": 180.0,
        "speed": 0.0,
        "current_task": None,
    },
    "USV Delta": {
        "call_sign": "USV Delta",
        "type": "USV",
        "status": "offline",
        "latitude": 58.2950,
        "longitude": 15.5511,
        "heading": 0.0,
        "speed": 0.0,
        "current_task": None,
    },
    "USV Echo": {
        "call_sign": "USV Echo",
        "type": "USV",
        "status": "ready",
        "latitude": 58.2204,
        "longitude": 15.3877,
        "heading": 270.0,
        "speed": 6.5,
        "current_task": None,
    },
}


# ============================================================================
# list_platforms() — returns list of summary dicts
# ============================================================================

LIST_PLATFORMS_RETURN = [
    {
        "call_sign": "UUV Alpha",
        "type": "UUV",
        "status": "tasked",
        "latitude": 58.2431,
        "longitude": 15.4892,
    },
    {
        "call_sign": "UUV Bravo",
        "type": "UUV",
        "status": "ready",
        "latitude": 58.3102,
        "longitude": 15.4218,
    },
    {
        "call_sign": "UUV Charlie",
        "type": "UUV",
        "status": "ready",
        "latitude": 58.1876,
        "longitude": 15.6033,
    },
    {
        "call_sign": "USV Delta",
        "type": "USV",
        "status": "offline",
        "latitude": 58.2950,
        "longitude": 15.5511,
    },
    {
        "call_sign": "USV Echo",
        "type": "USV",
        "status": "ready",
        "latitude": 58.2204,
        "longitude": 15.3877,
    },
]


# ============================================================================
# get_platform_state(call_sign) — full state for one platform
# ============================================================================

# Happy path: ready platform, no task
GET_STATE_BRAVO = {
    "call_sign": "UUV Bravo",
    "type": "UUV",
    "status": "ready",
    "latitude": 58.3102,
    "longitude": 15.4218,
    "heading": 90.0,
    "speed": 0.0,
    "current_task": None,
}

# Happy path: platform under way on a task
GET_STATE_ALPHA = {
    "call_sign": "UUV Alpha",
    "type": "UUV",
    "status": "tasked",
    "latitude": 58.2431,
    "longitude": 15.4892,
    "heading": 87.0,
    "speed": 4.2,
    "current_task": {
        "task_id": "tk_a91f",
        "task_type": "transit_to_waypoint",
        "latitude": 58.25,
        "longitude": 15.5,
        "dispatched_at": "2026-05-08T09:14:22Z",
    },
}

# Error case: not in fleet
# raises ToolError("UNKNOWN_CALLSIGN")
# triggered by e.g. call_sign="UUV Foxtrot"

# Error case: in fleet but offline
# raises ToolError("PLATFORM_UNREACHABLE")
# triggered by call_sign="USV Delta"


# ============================================================================
# task_waypoint(call_sign, latitude, longitude) — stages a task
# ============================================================================

# Happy path
TASK_WAYPOINT_RETURN = {
    "pending_task_id": "pt_4719",
    "readback": (
        "UUV Bravo, transit to fife-eight decimal two-fife north, "
        "one-fife decimal fife east. Confirm."
    ),
    "call_sign": "UUV Bravo",
    "latitude": 58.25,
    "longitude": 15.5,
}

# Second happy-path example, southern waypoint
TASK_WAYPOINT_RETURN_2 = {
    "pending_task_id": "pt_4720",
    "readback": (
        "UUV Charlie, transit to fife-eight decimal one-zero north, "
        "one-fife decimal seven-fife east. Confirm."
    ),
    "call_sign": "UUV Charlie",
    "latitude": 58.10,
    "longitude": 15.75,
}

# Error case: unknown call sign
# raises ToolError("UNKNOWN_CALLSIGN")

# Error case: out-of-range coordinates (e.g. lat=91.0)
# raises ToolError("INVALID_COORDINATE")

# Error case: offline or already tasked
# raises ToolError("PLATFORM_NOT_READY")
# triggered by call_sign="USV Delta" (offline) or "UUV Alpha" (tasked)


# ============================================================================
# get_pending_task(pending_task_id) — re-read a staged task
# ============================================================================

GET_PENDING_TASK_RETURN = {
    "pending_task_id": "pt_4719",
    "readback": (
        "UUV Bravo, transit to fife-eight decimal two-fife north, "
        "one-fife decimal fife east. Confirm."
    ),
    "call_sign": "UUV Bravo",
    "latitude": 58.25,
    "longitude": 15.5,
    "staged_at": "2026-05-08T09:42:11Z",
}

# Error case: ID not recognised (already dispatched, cancelled, or never existed)
# raises ToolError("UNKNOWN_TASK")
# triggered by pending_task_id="pt_9999"


# ============================================================================
# cancel_pending_task(pending_task_id) — cancel before dispatch
# ============================================================================

CANCEL_PENDING_TASK_RETURN = {
    "cancelled": True,
    "pending_task_id": "pt_4719",
}

# Error case: unknown
# raises ToolError("UNKNOWN_TASK")

# Error case: orchestrator already dispatched it to the bus
# raises ToolError("ALREADY_DISPATCHED")
# triggered by pending_task_id="pt_4719" if confirmation already happened