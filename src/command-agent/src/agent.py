import asyncio
import logging
import re
import textwrap
import time
from typing import Optional

from dotenv import load_dotenv
from livekit.agents import (
    Agent,
    AgentServer,
    AgentSession,
    JobContext,
    JobProcess,
    StopResponse,
    ToolError,
    cli,
    function_tool,
    llm,
    room_io,
)
from livekit.plugins import ai_coustics, anthropic, cartesia, silero, speechmatics
from livekit.plugins.turn_detector.multilingual import MultilingualModel

from mock_world_model import MockWorldModel

logger = logging.getLogger("agent")

load_dotenv(".env.local")


# ============================================================================
# Confirmation state machine — out-of-LLM intercept
# ============================================================================

_CONFIRM_PHRASES = {"confirm", "authorize", "go ahead"}
_CANCEL_PHRASES = {"cancel", "abort", "stop"}
_PUNCT_RE = re.compile(r"^[\s\W_]+|[\s\W_]+$")
_WS_RE = re.compile(r"\s+")
_MIN_PENDING_AGE_S = 1.5


def _normalise(text: str) -> str:
    t = text.strip().lower()
    t = _PUNCT_RE.sub("", t)
    return _WS_RE.sub(" ", t)


def _is_confirmation(text: str) -> bool:
    return _normalise(text) in _CONFIRM_PHRASES


def _is_cancellation(text: str) -> bool:
    return _normalise(text) in _CANCEL_PHRASES

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
class Assistant(Agent):
    def __init__(self, session: Optional[AgentSession] = None) -> None:
        super().__init__(
            # A Large Language Model (LLM) is your agent's brain, processing user input and generating a response
            # See all available models at https://docs.livekit.io/agents/models/llm/
            # To use a realtime model instead of a voice pipeline, replace the LLM
            # with a RealtimeModel and remove the STT/TTS from the AgentSession
            # (Note: This is for the OpenAI Realtime API. For other providers, see https://docs.livekit.io/agents/models/realtime/)
            # 1. Install livekit-agents[openai]
            # 2. Set OPENAI_API_KEY in .env.local
            # 3. Add `from livekit.plugins import openai` to the top of this file
            # 4. Replace the llm argument with:
            #     llm=openai.realtime.RealtimeModel(voice="marin")
            instructions=textwrap.dedent(
                """
              <role>
You are the voice interface for an unmanned maritime fleet command station. You
resolve operator voice commands into tool calls against a world model, read back
tasking orders in NATO-standard phrasing, and refuse anything outside that scope.
You do not dispatch orders — staging is your final action; confirmation is handled
by the agent orchestrator outside your control.
</role>

<context>
You operate inside a LiveKit voice agent (Speechmatics STT, Cartesia TTS) wired to
a world model HTTP API. Operators address the fleet by voice during NATO maritime
experiments (REPMUS, Dynamic Messenger, Task Force X Baltic). Every utterance you
process may result in a real platform moving in real water, so phrasing precision
and refusal discipline are safety-critical, not stylistic.

The orchestrator handles session greeting, confirmation keyword detection, and
dispatch to the multicast bus. You do not. Your responsibility ends when you
either (a) speak a readback of a staged task, (b) answer a query, or (c) issue a
fixed refusal.
</context>

<instructions>
1. CLASSIFY THE UTTERANCE into exactly one of:
   - Query fleet → call list_platforms
   - Query a specific platform → call get_platform_state
   - Stage a transit-to-waypoint task → call task_waypoint
   - Stage a recall-to-base task → call propose_recall
   - Re-read or cancel a staged task → call get_pending_task or cancel_pending_task
   - Anything else → fixed refusal (see below)

2. FOR TASKING (task_waypoint), require both an explicit call sign and explicit
   decimal-degree coordinates in the utterance. If either is missing, ambiguous,
   relative ("near that contact", "the closest one", "where it was last"), or
   implied rather than stated, do not call the tool. Issue the underspecified
   refusal and stop.

   FOR RECALLS (propose_recall), only an explicit call sign is required — base
   is a fixed system constant, not a destination the operator states. If the
   call sign is missing or ambiguous, issue the underspecified refusal.

3. FOR TASK READBACK, speak the readback string returned by task_waypoint
   verbatim. Do not paraphrase, embellish, prepend, or append anything. After
   speaking the readback, stop. Do not solicit confirmation — the orchestrator
   listens for it.

4. FOR QUERIES, report tool results in plain military-formal phrasing using the
   formatting rules below. State only what was returned. Do not speculate about
   intent, recommend actions, or volunteer information not asked for.

5. FOR TOOL ERRORS, speak the escalation phrase corresponding to the error
   class and stop. Do not retry, do not propose alternatives.

6. FOR OUT-OF-SCOPE REQUESTS (weather, opinions, chat, identity questions,
   anything not in step 1), speak the fixed refusal verbatim and stop.

7. NEVER fabricate platform names, coordinates, statuses, or task IDs. If a
   value is not in a tool result or the operator's utterance, it does not
   exist.

8. CONFIRMATION AND CANCELLATION ARE INTERCEPTED BY THE ORCHESTRATOR. If the
   operator's utterance is "confirm", "authorize", "go ahead", "cancel",
   "abort", or "stop", the orchestrator filters it before it reaches you.
   When you do receive a turn while a task is pending, treat it as a normal
   utterance — propose, query, or refuse per the rules above.
</instructions>

<constraints>
FORMATTING RULES FOR ALL SPOKEN OUTPUT:

- Digits are spoken individually with NATO phonetic numerals: zero, one, two,
  tree, fower, fife, six, seven, eight, niner.
- Decimal points are spoken as "decimal".
- Coordinates are spoken in decimal degrees with hemisphere:
  "fife-eight decimal two-fife north, one-fife decimal fife east".
- Call signs are spoken as written: "UUV Alpha", not "Uniform-Uniform-Victor
  Alpha", not "Alpha".
- Headings are spoken as three digits with "degrees": "two-seven-zero degrees".
- Speeds are spoken as digits with "knots": "one-two knots".
- Phrasing is third-person and imperative. Do not use "I". Do not use "please",
  "thanks", or conversational softeners.

FIXED PHRASES (speak verbatim):

- Underspecified call sign:
  "Negative. Specify call sign."
- Underspecified coordinates:
  "Negative. Specify coordinates in decimal degrees."
- Underspecified both:
  "Negative. Specify call sign and coordinates."
- Out of scope:
  "Negative. Request outside operational scope."
- UNKNOWN_CALLSIGN error:
  "Unable to raise [call sign as spoken by operator]. Advise."
- PLATFORM_UNREACHABLE / PLATFORM_NOT_READY error:
  "[Call sign] unreachable. Advise."
- INVALID_COORDINATE error:
  "Negative. Coordinates out of range. Advise."
- UNKNOWN_TASK error:
  "Unable to locate pending task. Advise."
- ALREADY_DISPATCHED error:
  "Task already dispatched. Unable to cancel. Advise."
- PENDING_TASK_EXISTS error:
  "Pending task exists. Confirm or cancel first."
- Any other tool failure:
  "Tool failure. Advise."
</constraints>

<output_format>
All output is spoken aloud through TTS. Output is one of:

- The verbatim readback string returned by task_waypoint, and nothing else.
- A plain query response constructed from tool results using the formatting
  rules above.
- One of the fixed phrases above, verbatim.

No markdown. No lists. No filler. No greetings. No sign-offs.
</output_format>

<examples>

<example>
Operator: "Task UUV Alpha to fifty-six point one five north, fifteen point
five eight east."
Action: call task_waypoint(call_sign="UUV Alpha", latitude=56.15, longitude=15.58)
Tool returns: {"pending_task_id": "pt_4719", "readback": "UUV-Alpha, transit to
fife-six decimal one-fife north, one-fife decimal fife-eight east. Confirm.",
"call_sign": "UUV-Alpha", "latitude": 56.15, "longitude": 15.58}
Agent says: "UUV-Alpha, transit to fife-six decimal one-fife north, one-fife
decimal fife-eight east. Confirm."
</example>

<example>
Operator: "Recall UUV Alpha to base."
Action: call propose_recall(call_sign="UUV Alpha")
Tool returns: {"pending_task_id": "pt_5210", "readback": "UUV-Alpha, recall to
base. Confirm.", "call_sign": "UUV-Alpha", "latitude": 56.16, "longitude": 15.59}
Agent says: "UUV-Alpha, recall to base. Confirm."
</example>

<example>
Operator: "Send the closest UUV to investigate that contact."
Action: no tool call.
Agent says: "Negative. Specify call sign and coordinates."
</example>

<example>
Operator: "What's the status of UUV Bravo?"
Action: call get_platform_state(call_sign="UUV Bravo")
Tool returns: {"call_sign": "UUV-Bravo", "type": "UUV", "status": "ready",
"latitude": 56.17, "longitude": 15.65, "heading": 90.0, "speed": 0.0,
"current_task": null}
Agent says: "UUV-Bravo, ready. Position fife-six decimal one-seven north,
one-fife decimal six-fife east. Heading zero-niner-zero degrees. Speed zero
knots. No active task."
</example>

<example>
Operator: "Task UUV Foxtrot to fifty-six point one north, fifteen point five
east."
Action: call task_waypoint(call_sign="UUV Foxtrot", latitude=56.1, longitude=15.5)
Tool raises: ToolError("UNKNOWN_CALLSIGN")
Agent says: "Unable to raise UUV Foxtrot. Advise."
</example>

<example>
Operator: "What do you think we should do about the contact at bearing two-seven-zero?"
Action: no tool call.
Agent says: "Negative. Request outside operational scope."
</example>

</examples>
"""
            ),
        )
        self.world = MockWorldModel()
        self._session = session
        self._pending_task: Optional[dict] = None
        self._pending_staged_at: float = 0.0
        self._pending_lock = asyncio.Lock()

    async def on_user_turn_completed(
        self,
        turn_ctx: llm.ChatContext,
        new_message: llm.ChatMessage,
    ) -> None:
        text = new_message.text_content
        if not text:
            return
        async with self._pending_lock:
            pending = self._pending_task
            if pending is None:
                return
            age = time.monotonic() - self._pending_staged_at
            if age < _MIN_PENDING_AGE_S:
                # Echo-bleed guard: ignore turns that arrive before the pending
                # task has had time to settle (e.g. the agent's own readback
                # leaking into STT).
                return

            if _is_confirmation(text):
                ptid = pending["pending_task_id"]
                try:
                    self.world.mark_dispatched(ptid)
                    logger.info(
                        "dispatch",
                        extra={
                            "ptid": ptid,
                            "call_sign": pending["call_sign"],
                            "latitude": pending["latitude"],
                            "longitude": pending["longitude"],
                            "outcome": "ok",
                        },
                    )
                    if self._session is not None:
                        self._session.say(f"Dispatched. Task {ptid}.")
                except Exception:
                    logger.exception("dispatch_failed", extra={"ptid": ptid})
                    if self._session is not None:
                        self._session.say("Tool failure. Advise.")
                finally:
                    self._pending_task = None
                raise StopResponse()

            if _is_cancellation(text):
                ptid = pending["pending_task_id"]
                try:
                    self.world.cancel_pending_task(ptid)
                    logger.info("cancel", extra={"ptid": ptid, "outcome": "ok"})
                except Exception:
                    logger.exception("cancel_failed", extra={"ptid": ptid})
                if self._session is not None:
                    self._session.say("Cancelled.")
                self._pending_task = None
                raise StopResponse()
            # else: fall through, LLM gets the turn

    @function_tool
    async def list_platforms(self) -> list[dict]:
        """
        List all platforms currently registered in the fleet world model.

        Returns:
            List of dicts, each with:
                - call_sign (str): e.g. "UUV Alpha"
                - type (str): e.g. "UUV", "USV"
                - status (str): "ready" | "tasked" | "offline"
                - latitude (float): decimal degrees, positive north
                - longitude (float): decimal degrees, positive east

        Call when the operator asks for fleet status, available platforms, or
        a roster ("list the fleet", "what's out there", "available platforms").
        Do not call unprompted.

        Example: operator says "List the fleet."
        """
        return self.world.list_platforms()

    @function_tool
    async def get_platform_state(self, call_sign: str) -> dict:
        """
        Return full current state for one named platform.

        Args:
            call_sign: Platform call sign as spoken by operator, e.g. "UUV Alpha".
                Case-insensitive.

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
        return self.world.get_platform_state(call_sign)

    @function_tool
    async def task_waypoint(
        self,
        call_sign: str,
        latitude: float,
        longitude: float,
    ) -> dict:
        """
        Stage a transit-to-waypoint task for a platform. STAGES ONLY — does not
        dispatch. Confirmation and dispatch are handled by the agent orchestrator
        outside this LLM. After calling this tool, speak the returned readback
        verbatim and stop.

        Args:
            call_sign: Platform call sign as spoken, e.g. "UUV Alpha".
            latitude: Decimal degrees, positive north. Range -90 to 90.
            longitude: Decimal degrees, positive east. Range -180 to 180.

        Returns:
            Dict with:
                - pending_task_id (str)
                - readback (str): pre-formatted NATO-style readback to speak verbatim
                - call_sign (str)
                - latitude (float)
                - longitude (float)

        Raises:
            ToolError("UNKNOWN_CALLSIGN")
            ToolError("INVALID_COORDINATE"): coordinates out of range.
            ToolError("PLATFORM_NOT_READY"): platform offline or already tasked.

        Only call when the operator has stated BOTH an explicit call sign AND
        explicit decimal-degree coordinates. If either is implied, relative, or
        missing, do not call — issue the underspecified refusal instead.

        Example: operator says "Task UUV Alpha to fifty-eight point two five
        north, fifteen point five east."
        """
        if self._pending_task is not None:
            raise ToolError("PENDING_TASK_EXISTS")
        result = self.world.task_waypoint(call_sign, latitude, longitude)
        self._pending_task = result
        self._pending_staged_at = time.monotonic()
        return result

    @function_tool
    async def propose_recall(self, call_sign: str) -> dict:
        """
        Stage a recall-to-base task for a fleet platform. STAGES ONLY — does
        not dispatch. Confirmation and dispatch are handled by the orchestrator
        outside this LLM. After calling this tool, speak the returned readback
        verbatim and stop.

        Args:
            call_sign: Platform call sign as spoken, e.g. "UUV-Alpha".
                Case-insensitive. Hyphens and spaces are interchangeable.

        Returns:
            Dict with:
                - pending_task_id (str)
                - readback (str): pre-formatted NATO-style readback to speak verbatim
                - call_sign (str)
                - latitude (float): fixed base coordinate
                - longitude (float): fixed base coordinate

        Raises:
            ToolError("UNKNOWN_CALLSIGN")
            ToolError("PLATFORM_NOT_READY"): platform offline or already tasked.
            ToolError("PENDING_TASK_EXISTS"): a task is already staged; the
                operator must confirm or cancel it before staging another.

        This is the ONLY tasking tool that does not require explicit
        coordinates from the operator — there is exactly one base, and its
        location is a fixed system constant. Do not invent recall locations.

        Call when the operator says "recall <call sign> to base", "bring
        <call sign> home", or similar.

        Example: operator says "Recall UUV-Alpha to base."
        """
        if self._pending_task is not None:
            raise ToolError("PENDING_TASK_EXISTS")
        result = self.world.recall_to_base(call_sign)
        self._pending_task = result
        self._pending_staged_at = time.monotonic()
        return result

    @function_tool
    async def get_pending_task(self, pending_task_id: str) -> dict:
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
        return self.world.get_pending_task(pending_task_id)

    @function_tool
    async def cancel_pending_task(self, pending_task_id: str) -> dict:
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

        Call when the operator says "cancel", "belay that", "scrub the order",
        or similar before they have spoken a confirmation keyword.
        """
        result = self.world.cancel_pending_task(pending_task_id)
        if (
            self._pending_task is not None
            and self._pending_task.get("pending_task_id") == pending_task_id
        ):
            self._pending_task = None
        return result

        # To add tools, use the @function_tool decorator.
        # Here's an example that adds a simple weather tool.
        # You also have to add `from livekit.agents import function_tool, RunContext` to the top of this file
        # @function_tool
        # async def lookup_weather(self, context: RunContext, location: str):
        #     """Use this tool to look up current weather information in the given location.
        #
        #     If the location is not supported by the weather service, the tool will indicate this. You must tell the user the location's weather is unavailable.
        #
        #     Args:
        #         location: The location to look up weather information for (e.g. city name)
        #     """
        #
        #     logger.info(f"Looking up weather for {location}")
        #
        #     return "sunny with a temperature of 70 degrees."


server = AgentServer()


def prewarm(proc: JobProcess):
    proc.userdata["vad"] = silero.VAD.load()


server.setup_fnc = prewarm


@server.rtc_session(agent_name="command-agent")
async def my_agent(ctx: JobContext):
    # Logging setup
    # Add any other context you want in all log entries here
    ctx.log_context_fields = {
        "room": ctx.room.name,
    }

    # Set up a voice AI pipeline using OpenAI, Cartesia, Deepgram, and the LiveKit turn detector
    session = AgentSession(
        # Speech-to-text (STT) is your agent's ears, turning the user's speech into text that the LLM can understand
        # See all available models at https://docs.livekit.io/agents/models/stt/
        stt=speechmatics.STT(language="en"),
        # Text-to-speech (TTS) is your agent's voice, turning the LLM's text into speech that the user can hear
        # See all available models as well as voice selections at https://docs.livekit.io/agents/models/tts/
        llm = anthropic.LLM(model="claude-sonnet-4-6",temperature=0),
        tts=cartesia.TTS(
            model="sonic-3", voice="573e3144-a684-4e72-ac2b-9b2063a50b53"
        ),
        # VAD and turn detection are used to determine when the user is speaking and when the agent should respond
        # See more at https://docs.livekit.io/agents/build/turns
        turn_detection=MultilingualModel(),
        vad=ctx.proc.userdata["vad"],
        # allow the LLM to generate a response while waiting for the end of turn
        # See more at https://docs.livekit.io/agents/build/audio/#preemptive-generation
        preemptive_generation=False,
        min_endpointing_delay=1.0,
    )

    # Build the agent with a back-reference to the session so the
    # confirmation state machine can speak deterministically without the LLM.
    assistant = Assistant(session=session)

    # Start the session, which initializes the voice pipeline and warms up the models
    await session.start(
        agent=assistant,
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=ai_coustics.EnhancerModel.QUAIL_VF_L
                ),
            ),
        ),
    )

    # # Add a virtual avatar to the session, if desired
    # # For other providers, see https://docs.livekit.io/agents/models/avatar/
    # avatar = anam.AvatarSession(
    #     persona_config=anam.PersonaConfig(
    #         name="...",
    #         avatarId="...",  # See https://docs.livekit.io/agents/models/avatar/plugins/anam
    #     ),
    # )
    # # Start the avatar and wait for it to join
    # await avatar.start(session, room=ctx.room)

    # Join the room and connect to the user
    await ctx.connect()

    # Opening greeting — spoken outside the LLM by the orchestrator.
    await session.say("Station ready, awaiting orders")


if __name__ == "__main__":
    cli.run_app(server)
