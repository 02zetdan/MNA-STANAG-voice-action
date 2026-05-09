"""
Mock world model for the Speak-to-the-Fleet voice agent.

Drop-in replacement for the real HTTP API. Implements the same tool surface
(list_platforms, get_platform_state, task_waypoint, get_pending_task,
cancel_pending_task) against an in-memory dict, so the LiveKit agent can be
exercised end-to-end before the real world model and multicast bus are up.

Usage with LiveKit:

    from livekit.agents import llm
    from mock_world_model import MockWorldModel, build_tools

    world = MockWorldModel()
    tools = build_tools(world)
    agent = llm.LLMAgent(tools=tools, instructions=SYSTEM_PROMPT, ...)

The tool functions below have the exact signatures and docstrings the LLM
sees. Edit those carefully — LiveKit exposes the docstring verbatim to the
model.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

# ToolError is guarded so this module can also be imported and tested
# without the LiveKit SDK installed.
try:
    from livekit.agents import ToolError  # type: ignore
except ImportError:  # pragma: no cover
    class ToolError(Exception):
        """Stand-in for livekit.agents.ToolError."""
        def __init__(self, code: str):
            super().__init__(code)
            self.code = code


# ---------------------------------------------------------------------------
# NATO phonetic numeral rendering (used to pre-format readback strings)
# ---------------------------------------------------------------------------

_PHONETIC_DIGITS = {
    "0": "zero", "1": "one", "2": "two", "3": "tree", "4": "fower",
    "5": "fife", "6": "six", "7": "seven", "8": "eight", "9": "niner",
}


def _phoneticise(number: float) -> str:
    """Render a number as hyphen-separated NATO phonetic digits with 'decimal'."""
    s = f"{number:.4f}".rstrip("0").rstrip(".")
    out = []
    for ch in s:
        if ch == ".":
            out.append("decimal")
        elif ch == "-":
            out.append("minus")
        else:
            out.append(_PHONETIC_DIGITS[ch])
    return "-".join(out)


def _format_readback(call_sign: str, lat: float, lon: float) -> str:
    """Build a NATO-style transit readback. Speak verbatim."""
    lat_hem = "north" if lat >= 0 else "south"
    lon_hem = "east" if lon >= 0 else "west"
    return (
        f"{call_sign}, transit to "
        f"{_phoneticise(abs(lat))} {lat_hem}, "
        f"{_phoneticise(abs(lon))} {lon_hem}. "
        f"Confirm."
    )


def _format_recall_readback(call_sign: str) -> str:
    """Build a NATO-style recall-to-base readback. Speak verbatim.

    Coords are intentionally omitted: 'base' is a fixed, known location;
    the readback is for verifying the resolved call sign, not the destination.
    """
    return f"{call_sign}, recall to base. Confirm."


# ---------------------------------------------------------------------------
# Home base — destination for recall_to_base().
# Karlskrona harbor centre; matches the b-service operator default and the
# captured PCAP source location to within ~100 m. Distinct from demo tasking
# waypoints (e.g. 56.15, 15.58) so 'recall' and 'task to ...' produce
# different readbacks.
# ---------------------------------------------------------------------------

BASE_LATITUDE = 56.16
BASE_LONGITUDE = 15.59


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Platform:
    call_sign: str
    type: str          # "UUV" | "USV" | "MV" | "FV"
    status: str        # "ready" | "tasked" | "offline" | "active" (ambient)
    latitude: float
    longitude: float
    heading: float
    speed: float
    current_task: Optional[dict] = None
    # True for fleet platforms the operator can task. False for ambient
    # contacts (real-world traffic). The agent's task path refuses False
    # with UNKNOWN_CALLSIGN — ambient contacts must look like they don't
    # exist for tasking, even though they appear in queries and on the map.
    is_controllable: bool = True


@dataclass
class PendingTask:
    pending_task_id: str
    call_sign: str
    latitude: float
    longitude: float
    readback: str
    staged_at: str
    dispatched: bool = False
    cancelled: bool = False


# ---------------------------------------------------------------------------
# World model
# ---------------------------------------------------------------------------

class MockWorldModel:
    """
    In-memory stand-in for the real world model HTTP API.

    The orchestrator (or a test harness) calls mark_dispatched(pending_task_id)
    to simulate the operator confirming a staged task and the multicast bus
    accepting it. After dispatch, cancel_pending_task raises ALREADY_DISPATCHED.
    """

    def __init__(self) -> None:
        self._platforms: dict[str, Platform] = {p.call_sign: p for p in _seed_platforms()}
        self._pending: dict[str, PendingTask] = {}

    # ----- Lookups -------------------------------------------------------

    def _resolve(self, call_sign: str) -> Platform:
        # STT emits "UUV Alpha" (spaces) but canonical names use hyphens
        # ("UUV-Alpha"). Normalise both sides so either form matches.
        target = call_sign.strip().lower().replace("-", " ")
        for cs, p in self._platforms.items():
            if cs.lower().replace("-", " ") == target:
                return p
        raise ToolError("UNKNOWN_CALLSIGN")

    # ----- Tool implementations -----------------------------------------

    def list_platforms(self) -> list[dict]:
        return [
            {
                "call_sign": p.call_sign,
                "type": p.type,
                "status": p.status,
                "latitude": p.latitude,
                "longitude": p.longitude,
                "is_controllable": p.is_controllable,
            }
            for p in self._platforms.values()
        ]

    def get_platform_state(self, call_sign: str) -> dict:
        p = self._resolve(call_sign)
        if p.status == "offline":
            raise ToolError("PLATFORM_UNREACHABLE")
        return {
            "call_sign": p.call_sign,
            "type": p.type,
            "status": p.status,
            "latitude": p.latitude,
            "longitude": p.longitude,
            "heading": p.heading,
            "speed": p.speed,
            "current_task": p.current_task,
            "is_controllable": p.is_controllable,
        }

    def task_waypoint(self, call_sign: str, latitude: float, longitude: float) -> dict:
        if not (-90.0 <= latitude <= 90.0) or not (-180.0 <= longitude <= 180.0):
            raise ToolError("INVALID_COORDINATE")

        p = self._resolve(call_sign)
        if not p.is_controllable:
            # Ambient contacts must not be tasked. Refuse with UNKNOWN_CALLSIGN
            # rather than reveal the contact exists but isn't ours.
            raise ToolError("UNKNOWN_CALLSIGN")
        if p.status != "ready":
            raise ToolError("PLATFORM_NOT_READY")

        pending_task_id = f"pt_{uuid.uuid4().hex[:4]}"
        readback = _format_readback(p.call_sign, latitude, longitude)
        self._pending[pending_task_id] = PendingTask(
            pending_task_id=pending_task_id,
            call_sign=p.call_sign,
            latitude=latitude,
            longitude=longitude,
            readback=readback,
            staged_at=_utcnow_iso(),
        )
        return {
            "pending_task_id": pending_task_id,
            "readback": readback,
            "call_sign": p.call_sign,
            "latitude": latitude,
            "longitude": longitude,
        }

    def recall_to_base(self, call_sign: str) -> dict:
        p = self._resolve(call_sign)
        if not p.is_controllable:
            # Ambient contacts must not be tasked. Refuse with UNKNOWN_CALLSIGN
            # rather than reveal the contact exists but isn't ours.
            raise ToolError("UNKNOWN_CALLSIGN")
        if p.status != "ready":
            raise ToolError("PLATFORM_NOT_READY")

        pending_task_id = f"pt_{uuid.uuid4().hex[:4]}"
        readback = _format_recall_readback(p.call_sign)
        self._pending[pending_task_id] = PendingTask(
            pending_task_id=pending_task_id,
            call_sign=p.call_sign,
            latitude=BASE_LATITUDE,
            longitude=BASE_LONGITUDE,
            readback=readback,
            staged_at=_utcnow_iso(),
        )
        return {
            "pending_task_id": pending_task_id,
            "readback": readback,
            "call_sign": p.call_sign,
            "latitude": BASE_LATITUDE,
            "longitude": BASE_LONGITUDE,
        }

    def get_pending_task(self, pending_task_id: str) -> dict:
        task = self._pending.get(pending_task_id)
        if task is None or task.cancelled:
            raise ToolError("UNKNOWN_TASK")
        return {
            "pending_task_id": task.pending_task_id,
            "readback": task.readback,
            "call_sign": task.call_sign,
            "latitude": task.latitude,
            "longitude": task.longitude,
            "staged_at": task.staged_at,
        }

    def cancel_pending_task(self, pending_task_id: str) -> dict:
        task = self._pending.get(pending_task_id)
        if task is None or task.cancelled:
            raise ToolError("UNKNOWN_TASK")
        if task.dispatched:
            raise ToolError("ALREADY_DISPATCHED")
        task.cancelled = True
        return {"cancelled": True, "pending_task_id": pending_task_id}

    # ----- Orchestrator hooks (not exposed to LLM) -----------------------

    def mark_dispatched(self, pending_task_id: str) -> None:
        """
        Simulate the orchestrator confirming a task and putting it on the bus.
        Updates the platform's current_task and status. Call this from the
        confirmation state machine, not from the LLM.
        """
        task = self._pending.get(pending_task_id)
        if task is None or task.cancelled or task.dispatched:
            return
        task.dispatched = True
        p = self._platforms[task.call_sign]
        p.status = "tasked"
        p.current_task = {
            "task_id": f"tk_{uuid.uuid4().hex[:4]}",
            "task_type": "transit_to_waypoint",
            "latitude": task.latitude,
            "longitude": task.longitude,
            "dispatched_at": _utcnow_iso(),
        }


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Seed scenario — Baltic, Karlskrona ~56.16°N 15.59°E
# Operating area centred on Karlskrona harbour; matches b-service operator
# default and the captured PCAP signal (MV Stumholmen ambient).
# ---------------------------------------------------------------------------

def _seed_platforms() -> list[Platform]:
    return [
        # ----- Fleet (controllable) ----------------------------------------
        Platform(
            call_sign="UUV-Alpha", type="UUV", status="tasked",
            latitude=56.1350, longitude=15.5000, heading=87.0, speed=4.2,
            current_task={
                "task_id": "tk_a91f",
                "task_type": "transit_to_waypoint",
                "latitude": 56.15,
                "longitude": 15.56,
                "dispatched_at": "2026-05-08T09:14:22Z",
            },
        ),
        Platform(
            call_sign="UUV-Bravo", type="UUV", status="ready",
            latitude=56.1700, longitude=15.6500, heading=90.0, speed=0.0,
        ),
        Platform(
            call_sign="UUV-Charlie", type="UUV", status="ready",
            latitude=56.1000, longitude=15.4500, heading=180.0, speed=0.0,
        ),
        Platform(
            call_sign="USV-Delta", type="USV", status="offline",
            latitude=56.2000, longitude=15.6000, heading=0.0, speed=0.0,
        ),
        Platform(
            call_sign="USV-Echo", type="USV", status="ready",
            latitude=56.1200, longitude=15.7000, heading=270.0, speed=6.5,
        ),
        # ----- Ambient contacts (NMEA-replayed; not tasking targets) -------
        Platform(
            call_sign="MV Northern Star", type="MV", status="active",
            latitude=56.2100, longitude=15.6200, heading=205.0, speed=11.8,
            is_controllable=False,
        ),
        Platform(
            call_sign="FV Karlsvik", type="FV", status="active",
            latitude=56.0500, longitude=15.5800, heading=15.0, speed=4.7,
            is_controllable=False,
        ),
        # Stationary moored vessel — position from the captured PCAP
        # (src/kraken_data/multicast_AdvNavData-2026-05-08_07-40-14_capture.pcap).
        Platform(
            call_sign="MV Stumholmen", type="MV", status="active",
            latitude=56.16080495, longitude=15.56721734, heading=0.0, speed=0.0,
            is_controllable=False,
        ),
    ]


# ---------------------------------------------------------------------------
# Tool wrappers (plain async callables, no decorator)
# ---------------------------------------------------------------------------
#
# build_tools(world) returns plain async callables bound to the given
# MockWorldModel. The docstrings below are what the LLM sees, so edit them
# with care. Decorate them with @function_tool at the call site if needed.

def build_tools(world: MockWorldModel):

    async def list_platforms() -> list[dict]:
        """
        List all platforms currently registered in the fleet world model.

        Returns:
            List of dicts, each with:
                - call_sign (str): e.g. "UUV Alpha"
                - type (str): e.g. "UUV", "USV"
                - status (str): "ready" | "tasked" | "offline"
                - latitude (float): decimal degrees, positive north
                - longitude (float): decimal degrees, positive east

        Call when the operator asks for fleet status, available platforms,
        or a roster ("list the fleet", "what's out there", "available
        platforms"). Do not call unprompted.

        Example: operator says "List the fleet."
        """
        return world.list_platforms()

    async def get_platform_state(call_sign: str) -> dict:
        """
        Return full current state for one named platform.

        Args:
            call_sign: Platform call sign as spoken by operator,
                e.g. "UUV Alpha". Case-insensitive.

        Returns:
            Dict with:
                - call_sign (str)
                - type (str)
                - status (str)
                - latitude (float): decimal degrees
                - longitude (float): decimal degrees
                - heading (float): degrees true
                - speed (float): knots
                - current_task (dict | None)

        Raises:
            ToolError("UNKNOWN_CALLSIGN"): call sign not in fleet.
            ToolError("PLATFORM_UNREACHABLE"): platform cannot be polled.

        Call when the operator asks about a specific named platform.

        Example: operator says "Status on UUV Bravo."
        """
        return world.get_platform_state(call_sign)

    async def task_waypoint(call_sign: str, latitude: float, longitude: float) -> dict:
        """
        Stage a transit-to-waypoint task for a platform. STAGES ONLY — does
        not dispatch. Confirmation and dispatch are handled by the agent
        orchestrator outside this LLM. After calling this tool, speak the
        returned readback verbatim and stop.

        Args:
            call_sign: Platform call sign as spoken, e.g. "UUV Alpha".
            latitude: Decimal degrees, positive north. Range -90 to 90.
            longitude: Decimal degrees, positive east. Range -180 to 180.

        Returns:
            Dict with:
                - pending_task_id (str)
                - readback (str): pre-formatted NATO-style readback to
                  speak verbatim
                - call_sign (str)
                - latitude (float)
                - longitude (float)

        Raises:
            ToolError("UNKNOWN_CALLSIGN")
            ToolError("INVALID_COORDINATE"): coordinates out of range.
            ToolError("PLATFORM_NOT_READY"): platform offline or already tasked.

        Only call when the operator has stated BOTH an explicit call sign
        AND explicit decimal-degree coordinates. If either is implied,
        relative, or missing, do not call — issue the underspecified
        refusal instead.

        Example: operator says "Task UUV Alpha to fifty-six point one
        five north, fifteen point five eight east."
        """
        return world.task_waypoint(call_sign, latitude, longitude)

    async def get_pending_task(pending_task_id: str) -> dict:
        """
        Retrieve a staged task that has not yet been confirmed or cancelled.

        Args:
            pending_task_id: ID previously returned by task_waypoint.

        Returns:
            Dict matching task_waypoint return shape, plus:
                - staged_at (str): ISO 8601 timestamp.

        Raises:
            ToolError("UNKNOWN_TASK"): ID not recognised.

        Call when the operator asks to re-read or hear again a pending task
        ("say again the pending task", "read back the staged order").
        """
        return world.get_pending_task(pending_task_id)

    async def cancel_pending_task(pending_task_id: str) -> dict:
        """
        Cancel a staged task before the orchestrator dispatches it.

        Args:
            pending_task_id: ID previously returned by task_waypoint.

        Returns:
            Dict with:
                - cancelled (bool)
                - pending_task_id (str)

        Raises:
            ToolError("UNKNOWN_TASK"): ID not recognised.
            ToolError("ALREADY_DISPATCHED"): task confirmed and on the bus.

        Call when the operator says "cancel", "belay that", "scrub the
        order", or similar before they have spoken a confirmation keyword.
        """
        return world.cancel_pending_task(pending_task_id)

    return [
        list_platforms,
        get_platform_state,
        task_waypoint,
        get_pending_task,
        cancel_pending_task,
    ]


# ---------------------------------------------------------------------------
# Smoke test — `python mock_world_model.py` exercises every path
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import asyncio

    async def main():
        world = MockWorldModel()
        (list_platforms, get_platform_state, task_waypoint,
         get_pending_task, cancel_pending_task) = build_tools(world)

        print("=== list_platforms (fleet + ambient) ===")
        for p in await list_platforms():
            kind = "FLEET" if p["is_controllable"] else "AMBIENT"
            print(f"  [{kind:7s}] {p['call_sign']:18s} {p['type']:3s} {p['status']:8s} "
                  f"({p['latitude']:.4f}, {p['longitude']:.4f})")

        print("\n=== get_platform_state('UUV-Bravo') ===")
        print(" ", await get_platform_state("UUV-Bravo"))

        print("\n=== get_platform_state('USV-Delta')  [expect PLATFORM_UNREACHABLE] ===")
        try: await get_platform_state("USV-Delta")
        except ToolError as e: print(f"  ToolError: {e}")

        print("\n=== get_platform_state('UUV-Foxtrot') [expect UNKNOWN_CALLSIGN] ===")
        try: await get_platform_state("UUV-Foxtrot")
        except ToolError as e: print(f"  ToolError: {e}")

        print("\n=== task_waypoint('UUV-Bravo', 56.15, 15.58) — STT-style spaced form ===")
        staged = await task_waypoint("UUV Bravo", 56.15, 15.58)  # hyphen-tolerant
        print(" ", staged)
        ptid = staged["pending_task_id"]

        print(f"\n=== get_pending_task({ptid!r}) ===")
        print(" ", await get_pending_task(ptid))

        print("\n=== task_waypoint('UUV-Alpha', ...)  [expect PLATFORM_NOT_READY] ===")
        try: await task_waypoint("UUV-Alpha", 56.0, 15.0)
        except ToolError as e: print(f"  ToolError: {e}")

        print("\n=== task_waypoint('UUV-Bravo', 91.0, 15.0)  [expect INVALID_COORDINATE] ===")
        try: await task_waypoint("UUV-Bravo", 91.0, 15.0)
        except ToolError as e: print(f"  ToolError: {e}")

        print("\n=== task_waypoint('MV Northern Star', ...) [expect UNKNOWN_CALLSIGN — ambient refusal] ===")
        try: await task_waypoint("MV Northern Star", 56.20, 15.62)
        except ToolError as e: print(f"  ToolError: {e}")

        print(f"\n=== cancel_pending_task({ptid!r}) ===")
        print(" ", await cancel_pending_task(ptid))

        print(f"\n=== cancel_pending_task({ptid!r}) again  [expect UNKNOWN_TASK] ===")
        try: await cancel_pending_task(ptid)
        except ToolError as e: print(f"  ToolError: {e}")

        print("\n=== dispatch flow ===")
        staged2 = await task_waypoint("UUV-Charlie", 56.10, 15.75)
        print("  staged:    ", staged2["readback"])
        world.mark_dispatched(staged2["pending_task_id"])
        print("  charlie:   ", await get_platform_state("UUV-Charlie"))
        try: await cancel_pending_task(staged2["pending_task_id"])
        except ToolError as e:
            print(f"  cancel after dispatch: ToolError({e})  [expect ALREADY_DISPATCHED]")

    asyncio.run(main())
