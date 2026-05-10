import asyncio
import json
import logging
import os
import re
import textwrap
import threading
import time
import urllib.request
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


# ---------------------------------------------------------------------------
# Speechmatics custom vocabulary
# ---------------------------------------------------------------------------
# Biases the recognizer toward maritime-command vocabulary and corrects the
# specific misrecognitions we have observed live:
#   "north" → "naught"          (acoustically similar)
#   "east"  → "e" / "acres"     (often clipped)
#   "task"  → "ask"             ("t" dropped)
#
# `sounds_like` entries tell Speechmatics: when the audio matches one of these
# variants, emit the canonical `content` instead. Safe because the variant
# words ("naught", "ask", "acres") are not part of the operator lexicon.
def _build_stt_vocab() -> list[speechmatics.AdditionalVocabEntry]:
    entry = speechmatics.AdditionalVocabEntry
    return [
        # Cardinal directions — most error-prone in the traces.
        entry(content="north", sounds_like=["naught", "north."]),
        entry(content="east",  sounds_like=["e", "ease", "acres", "east."]),
        entry(content="south", sounds_like=["sows"]),
        entry(content="west",  sounds_like=["wess"]),
        # Decimal anchors.
        entry(content="point"),
        entry(content="decimal"),
        # Single-word fleet call signs — STT-resilient names that don't
        # rely on letter-spelled prefixes.
        entry(content="Falcon", sounds_like=["falcon.", "falken", "falconer"]),
        entry(content="Raven",  sounds_like=["raven.", "ravon"]),
        entry(content="Osprey", sounds_like=["osprey.", "ospray", "ospri"]),
        entry(content="Marlin", sounds_like=["marlin.", "marland", "marlon"]),
        entry(content="Tarpon", sounds_like=["tarpon.", "tarpan", "tarpin"]),
        # Ambient ship names — the Swedish-derived ones are STT-fragile.
        entry(content="Stumholmen", sounds_like=[
            "stumholmen", "stol moment", "storm hallman", "stormholman",
            "holman", "stump holman", "stem holman", "stum holm",
        ]),
        entry(content="Östersjön", sounds_like=[
            "ostersjon", "uster shawn", "uster john", "oster yon",
        ]),
        entry(content="Karlsvik", sounds_like=[
            "carlsvick", "carls week", "carls vic", "karls week",
        ]),
        entry(content="Northern Star", sounds_like=["northern star", "norther star"]),
        # Type acronyms — kept for context (still appear in TTS output) but
        # not required in operator utterances.
        entry(content="UUV", sounds_like=["you you V", "U U V"]),
        entry(content="USV", sounds_like=["you S V", "U S V", "us V", "us the"]),
        # Verbs and prepositions.
        entry(content="task",      sounds_like=["ask", "tusk"]),
        entry(content="recall",    sounds_like=["recall to base"]),
        entry(content="status"),
        entry(content="toward",    sounds_like=["towards", "tword", "to word"]),
        entry(content="intercept", sounds_like=["inter cept", "in to set"]),
        entry(content="vector",    sounds_like=["vector"]),
        # Confirmation / cancellation phrases (whole words; the deterministic
        # matcher operates on these exact strings post-normalisation).
        entry(content="confirm"),
        entry(content="authorize"),
        entry(content="cancel"),
        entry(content="abort"),
    ]

load_dotenv(".env.local")


# ============================================================================
# Confirmation state machine — out-of-LLM intercept
# ============================================================================

_CONFIRM_PHRASES = {"confirm", "authorize", "go ahead"}
_CANCEL_PHRASES = {"cancel", "abort", "stop"}
_PUNCT_RE = re.compile(r"^[\s\W_]+|[\s\W_]+$")
_WS_RE = re.compile(r"\s+")
_MIN_PENDING_AGE_S = 3.0  # raised from 1.5 — readback TTS often runs >2 s
                          # and the agent's own "...Confirm" can echo back
                          # through STT before the original guard expires.


def _normalise(text: str) -> str:
    t = text.strip().lower()
    t = _PUNCT_RE.sub("", t)
    return _WS_RE.sub(" ", t)


def _is_confirmation(text: str) -> bool:
    return _normalise(text) in _CONFIRM_PHRASES


def _is_cancellation(text: str) -> bool:
    return _normalise(text) in _CANCEL_PHRASES


def _refusal_phrase(error_code: str, *, call_sign: str = "") -> str:
    """Map a ToolError code to the canonical refusal string the orchestrator
    speaks. Used by the staging tool wrappers so the LLM never paraphrases
    refusals (consistent with the deterministic-readback approach).

    Phrases mirror the FIXED PHRASES list in the system prompt verbatim.
    """
    if error_code == "UNKNOWN_CALLSIGN":
        return f"Unable to raise {call_sign}. Advise." if call_sign else "Unable to raise platform. Advise."
    if error_code in ("PLATFORM_UNREACHABLE", "PLATFORM_NOT_READY"):
        return f"{call_sign} unreachable. Advise." if call_sign else "Platform unreachable. Advise."
    if error_code == "INVALID_COORDINATE":
        return "Negative. Coordinates out of range. Advise."
    if error_code == "INVALID_TARGET":
        return "Negative. Invalid target. Advise."
    if error_code == "PENDING_TASK_EXISTS":
        return "Pending task exists. Confirm or cancel first."
    if error_code == "UNKNOWN_TASK":
        return "Unable to locate pending task. Advise."
    if error_code == "ALREADY_DISPATCHED":
        return "Task already dispatched. Unable to cancel. Advise."
    return "Tool failure. Advise."


# ---------------------------------------------------------------------------
# Audit emission — POSTs structured events to b-service /api/v1/audit/event
# ---------------------------------------------------------------------------
# Stdlib only (urllib + thread) so we don't introduce httpx as a hard dep
# yet. Fire-and-forget: a failed POST is logged but never blocks dispatch.

_AUDIT_BASE_URL = os.environ.get("WORLD_MODEL_URL", "http://localhost:8000")


def _audit_post(kind: str, message: str = "", **details) -> None:
    """Push an audit event to b-service. Returns immediately; the HTTP
    POST happens on a background thread so the agent never blocks."""
    payload = {"kind": kind, "message": message, "details": details}

    def _send():
        try:
            req = urllib.request.Request(
                f"{_AUDIT_BASE_URL}/api/v1/audit/event",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=2.0) as r:
                r.read()
        except OSError as e:
            logger.debug("audit POST failed: %s", e)

    threading.Thread(target=_send, daemon=True).start()


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
   - Stage a transit toward another named platform → call propose_intercept
     **PREFERRED for any tasking** — this is the primary tasking path.
     Use it whenever the operator names a target platform (fleet member,
     ambient contact, anything on the map). Sidesteps STT's unreliability
     with decimal numbers.
   - Stage a recall-to-base task → call propose_recall
   - Stage a transit to explicit decimal coordinates → call task_waypoint
     (FALLBACK only — coordinate dictation is fragile over voice)
   - Query fleet (agent-internal grounding) → call list_platforms
   - Query a specific platform (agent-internal grounding) → call get_platform_state
   - Re-read or cancel a staged task → call get_pending_task or cancel_pending_task
   - Anything else → fixed refusal (see below)

   The two query tools are AGENT-INTERNAL. They resolve fleet state for
   your own grounding. **Do NOT speak the results** — the map renders
   fleet state for the operator. If you call them, do so silently and
   use the result to inform your next decision (e.g. selecting a target
   for propose_intercept). If the operator asks "what's out there", do
   not narrate; refuse with the fixed out-of-scope phrase since fleet
   awareness is the map's job, not yours.

2. FOR TASKING (task_waypoint), require both an explicit call sign and a clean
   pair of decimal-degree coordinates in the utterance. If either is missing,
   ambiguous, relative ("near that contact", "the closest one", "where it was
   last"), or arrives in unparseable form (e.g. "five six 121" — missing a
   decimal point; missing degree word; numbers that don't form valid
   latitude/longitude), do not call the tool. Issue the underspecified refusal
   and stop. **Do not infer what the operator probably meant** — STT errors
   are common; refuse and let them retry. Never invent missing digits.

   FOR RECALLS (propose_recall), only an explicit call sign is required — base
   is a fixed system constant, not a destination the operator states. If the
   call sign is missing or ambiguous, issue the underspecified refusal.

3. FOR TASK READBACK, your spoken response MUST BE EXACTLY the readback string
   returned by the tool. Nothing else. Zero preamble. Zero summary. Zero
   "to confirm I heard". Zero "tasking X to coordinates". Zero paraphrase of
   the parameters in your own words before or after. The tool's readback
   string IS your entire utterance. Then stop. Do not solicit confirmation —
   the orchestrator listens for it.

   COUNTEREXAMPLE — DO NOT do this:
     Wrong: "To confirm the coordinates fifty-six decimal one-five north,
            fifteen decimal five-eight east, tasking Falcon now. Falcon,
            transit to fife-six decimal one-fife north, one-fife decimal
            fife-eight east. Confirm."
     Right: "Falcon, transit to fife-six decimal one-fife north, one-fife
            decimal fife-eight east. Confirm."

   The right form is the tool's readback string and nothing else.

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
- Call signs are single distinctive words: "Falcon", "Raven", "Osprey",
  "Marlin", "Tarpon" for the controllable fleet. In your SPOKEN output use
  the canonical form returned by the tool. When the operator provides a call
  sign, pass whatever they said verbatim to the tool — the resolver normalises
  case, hyphens, and STT fillers. Do not refuse on form.
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
Operator: "Task Falcon to fifty-six point one five north, fifteen point five
eight east."
Action: call task_waypoint(call_sign="Falcon", latitude=56.15, longitude=15.58)
Tool returns: {"pending_task_id": "pt_4719", "readback": "Falcon, transit to
fife-six decimal one-fife north, one-fife decimal fife-eight east. Confirm.",
"call_sign": "Falcon", "latitude": 56.15, "longitude": 15.58}
Agent says: "Falcon, transit to fife-six decimal one-fife north, one-fife
decimal fife-eight east. Confirm."
</example>

<example>
Operator: "Recall Falcon to base."
Action: call propose_recall(call_sign="Falcon")
Tool returns: {"pending_task_id": "pt_5210", "readback": "Falcon, recall to
base. Confirm.", "call_sign": "Falcon", "latitude": 56.16, "longitude": 15.59}
Agent says: "Falcon, recall to base. Confirm."
</example>

<example>
Operator: "Task Raven toward Falcon."
Action: call propose_intercept(call_sign="Raven", target_call_sign="Falcon")
Tool returns: {"pending_task_id": "pt_6311", "readback": "Raven, transit
toward Falcon. Confirm.", "call_sign": "Raven", "target_call_sign": "Falcon",
"latitude": 56.13, "longitude": 15.50}
Agent says: "Raven, transit toward Falcon. Confirm."
(Operator picked the destination by name — no coordinates spoken. The world
model snapshots Falcon's position at staging time.)
</example>

<example>
Operator: "Vector Tarpon to MV Northern Star for inspection."
Action: call propose_intercept(call_sign="Tarpon", target_call_sign="MV Northern Star")
Tool returns: {"pending_task_id": "pt_6412", "readback": "Tarpon, transit
toward MV Northern Star. Confirm.", "call_sign": "Tarpon",
"target_call_sign": "MV Northern Star", "latitude": 56.21, "longitude": 15.62}
Agent says: "Tarpon, transit toward MV Northern Star. Confirm."
(Tasking toward an ambient contact is allowed — the operator may need to
inspect real-world traffic.)
</example>

<example>
Operator: "Send the closest UUV to investigate that contact."
Action: no tool call.
Agent says: "Negative. Specify call sign and coordinates."
</example>

<example>
Operator: "What's the status of Raven?"
Action: call get_platform_state(call_sign="Raven")
Tool returns: {"call_sign": "Raven", "type": "UUV", "status": "ready",
"latitude": 56.17, "longitude": 15.65, "heading": 90.0, "speed": 0.0,
"current_task": null}
Agent says: "Raven, ready. Position fife-six decimal one-seven north,
one-fife decimal six-fife east. Heading zero-niner-zero degrees. Speed zero
knots. No active task."
</example>

<example>
Operator: "Task Phantom to fifty-six point one north, fifteen point five east."
Action: call task_waypoint(call_sign="Phantom", latitude=56.1, longitude=15.5)
Tool raises: ToolError("UNKNOWN_CALLSIGN")
Agent says: "Unable to raise Phantom. Advise."
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
        # Backend selection: WORLD_MODEL_URL env var → real HTTP client
        # against b-service (closes the voice → map loop end-to-end).
        # Unset → MockWorldModel (unit tests, offline dev).
        world_url = os.environ.get("WORLD_MODEL_URL", "").strip()
        if world_url:
            from world import WorldModel
            self.world = WorldModel(world_url)
            logger.info("using b-service WorldModel at %s", world_url)
        else:
            self.world = MockWorldModel()
            logger.info("using in-process MockWorldModel")
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
                    _audit_post(
                        "dispatch",
                        f"dispatched task {ptid} → {pending['call_sign']}",
                        ptid=ptid,
                        call_sign=pending["call_sign"],
                        latitude=pending["latitude"],
                        longitude=pending["longitude"],
                    )
                    if self._session is not None:
                        self._session.say(f"Dispatched. Task {ptid}.")
                except Exception:
                    logger.exception("dispatch_failed", extra={"ptid": ptid})
                    _audit_post("dispatch_failed", f"dispatch failed: {ptid}", ptid=ptid)
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
                    _audit_post(
                        "cancel",
                        f"cancelled pending task {ptid}",
                        ptid=ptid,
                        call_sign=pending["call_sign"],
                    )
                except Exception:
                    logger.exception("cancel_failed", extra={"ptid": ptid})
                    _audit_post("cancel_failed", f"cancel failed: {ptid}", ptid=ptid)
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
                - call_sign (str): single distinctive word, e.g. "Falcon"
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
            call_sign: Platform call sign as spoken by operator, e.g. "Falcon".
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

        Example: operator says "Status on Raven."
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
            call_sign: Platform call sign as spoken, e.g. "Falcon".
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

        Example: operator says "Task Falcon to fifty-six point one five
        north, fifteen point five eight east."
        """
        if self._pending_task is not None:
            _audit_post("refuse", "task_waypoint refused: pending exists",
                        tool="task_waypoint", call_sign=call_sign, reason="PENDING_TASK_EXISTS")
            if self._session is not None:
                self._session.say(_refusal_phrase("PENDING_TASK_EXISTS"))
            raise StopResponse()
        try:
            result = self.world.task_waypoint(call_sign, latitude, longitude)
        except ToolError as e:
            _audit_post("refuse", f"task_waypoint refused: {e}",
                        tool="task_waypoint", call_sign=call_sign, reason=str(e))
            if self._session is not None:
                self._session.say(_refusal_phrase(str(e), call_sign=call_sign))
            raise StopResponse() from e
        self._pending_task = result
        self._pending_staged_at = time.monotonic()
        _audit_post("stage", f"staged task_waypoint → {result['call_sign']}",
                    tool="task_waypoint", ptid=result["pending_task_id"],
                    call_sign=result["call_sign"],
                    latitude=result["latitude"], longitude=result["longitude"])
        # Speak the readback deterministically — bypasses LLM text generation
        # which has been observed to add narration before the readback even
        # though the system prompt forbids it.
        if self._session is not None:
            self._session.say(result["readback"])
        raise StopResponse()

    @function_tool
    async def propose_recall(self, call_sign: str) -> dict:
        """
        Stage a recall-to-base task for a fleet platform. STAGES ONLY — does
        not dispatch. Confirmation and dispatch are handled by the orchestrator
        outside this LLM. After calling this tool, speak the returned readback
        verbatim and stop.

        Args:
            call_sign: Platform call sign as spoken, e.g. "Falcon".
                Case-insensitive.

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

        Example: operator says "Recall Falcon to base."
        """
        if self._pending_task is not None:
            _audit_post("refuse", "propose_recall refused: pending exists",
                        tool="propose_recall", call_sign=call_sign, reason="PENDING_TASK_EXISTS")
            if self._session is not None:
                self._session.say(_refusal_phrase("PENDING_TASK_EXISTS"))
            raise StopResponse()
        try:
            result = self.world.recall_to_base(call_sign)
        except ToolError as e:
            _audit_post("refuse", f"propose_recall refused: {e}",
                        tool="propose_recall", call_sign=call_sign, reason=str(e))
            if self._session is not None:
                self._session.say(_refusal_phrase(str(e), call_sign=call_sign))
            raise StopResponse() from e
        self._pending_task = result
        self._pending_staged_at = time.monotonic()
        _audit_post("stage", f"staged recall → {result['call_sign']}",
                    tool="propose_recall", ptid=result["pending_task_id"],
                    call_sign=result["call_sign"])
        # Speak the readback deterministically — see note in task_waypoint.
        if self._session is not None:
            self._session.say(result["readback"])
        raise StopResponse()

    @function_tool
    async def propose_intercept(self, call_sign: str, target_call_sign: str) -> dict:
        """
        Stage an intercept: send `call_sign` toward `target_call_sign`'s
        current position. STAGES ONLY — does not dispatch. Confirmation and
        dispatch are handled by the orchestrator outside this LLM.

        Use this tool when the operator picks a destination by referring to
        ANOTHER platform on the map instead of dictating coordinates. This
        is the preferred form for voice tasking because it sidesteps STT's
        unreliability with decimal numbers.

        Args:
            call_sign: The actor — the controllable platform that will move.
                Must be a fleet platform in `ready` status.
            target_call_sign: The destination — any known platform whose
                current position becomes the actor's waypoint. May be a
                fleet platform OR an ambient contact (the operator may want
                to vector a UUV toward a real-world ship for inspection).

        Returns:
            Dict with pending_task_id, readback, call_sign, target_call_sign,
            latitude, longitude (snapshotted from target at staging time).

        Raises:
            ToolError("UNKNOWN_CALLSIGN"): actor or target unknown, OR
                actor is an ambient contact (not controllable).
            ToolError("PLATFORM_NOT_READY"): actor is offline or already tasked.
            ToolError("INVALID_TARGET"): actor and target are the same.
            ToolError("INVALID_COORDINATE"): target is outside the operating area.
            ToolError("PENDING_TASK_EXISTS"): a task is already staged.

        Call when the operator says "task <X> toward <Y>", "intercept <Y>
        with <X>", "vector <X> to <Y>", "send <X> after <Y>", or similar.

        Example: operator says "Task Raven toward Falcon."
                 → propose_intercept(call_sign="Raven", target_call_sign="Falcon")
        """
        if self._pending_task is not None:
            _audit_post("refuse", "propose_intercept refused: pending exists",
                        tool="propose_intercept", call_sign=call_sign,
                        target_call_sign=target_call_sign, reason="PENDING_TASK_EXISTS")
            if self._session is not None:
                self._session.say(_refusal_phrase("PENDING_TASK_EXISTS"))
            raise StopResponse()
        try:
            result = self.world.intercept_platform(call_sign, target_call_sign)
        except ToolError as e:
            # UNKNOWN_CALLSIGN could be actor or target. Try resolving the
            # actor; if that succeeds, the target was the unknown one.
            problem = call_sign
            if str(e) == "UNKNOWN_CALLSIGN":
                try:
                    self.world._resolve(call_sign)
                    problem = target_call_sign  # actor resolved → target failed
                except ToolError:
                    pass  # actor failed
            _audit_post("refuse", f"propose_intercept refused: {e}",
                        tool="propose_intercept", call_sign=call_sign,
                        target_call_sign=target_call_sign,
                        reason=str(e), problem=problem)
            if self._session is not None:
                self._session.say(_refusal_phrase(str(e), call_sign=problem))
            raise StopResponse() from e
        self._pending_task = result
        self._pending_staged_at = time.monotonic()
        _audit_post("stage",
                    f"staged intercept: {result['call_sign']} → {result['target_call_sign']}",
                    tool="propose_intercept",
                    ptid=result["pending_task_id"],
                    call_sign=result["call_sign"],
                    target_call_sign=result["target_call_sign"],
                    latitude=result["latitude"], longitude=result["longitude"])
        # Speak the readback deterministically — see note in task_waypoint.
        if self._session is not None:
            self._session.say(result["readback"])
        raise StopResponse()

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
        try:
            result = self.world.get_pending_task(pending_task_id)
        except ToolError as e:
            if self._session is not None:
                self._session.say(_refusal_phrase(str(e)))
            raise StopResponse() from e
        # Speak the same readback string that was originally produced when
        # the task was staged. Deterministic — no LLM in the loop.
        if self._session is not None:
            self._session.say(result["readback"])
        raise StopResponse()

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
        try:
            self.world.cancel_pending_task(pending_task_id)
        except ToolError as e:
            if self._session is not None:
                self._session.say(_refusal_phrase(str(e)))
            raise StopResponse() from e
        if (
            self._pending_task is not None
            and self._pending_task.get("pending_task_id") == pending_task_id
        ):
            self._pending_task = None
        if self._session is not None:
            self._session.say("Cancelled.")
        raise StopResponse()

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
        # Speechmatics defaults to ADAPTIVE turn detection, but its silence
        # trigger is short enough that a brief pause mid-sentence chunks the
        # trailing word into a separate transcript (e.g. "...fifteen point
        # five eight" then "East." 1 s later). Raise the silence trigger so
        # the whole tasking sentence holds together.
        # additional_vocab biases the recognizer toward our maritime-command
        # vocabulary — fixes the "north"→"naught", "east"→"e", "task"→"ask"
        # misrecognitions seen live.
        stt=speechmatics.STT(
            language="en",
            # 1.5 s silence before declaring end-of-utterance — operators
            # often pause ~1 s mid-sentence between coordinate chunks, and
            # 1.0 s was triggering chunking. 1.5 holds the sentence together
            # at the cost of ~0.5 s extra latency on the final transcript.
            end_of_utterance_silence_trigger=1.5,
            end_of_utterance_max_delay=4.0,
            additional_vocab=_build_stt_vocab(),
        ),
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
