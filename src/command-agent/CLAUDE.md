# CLAUDE.md

This folder is the **voice and operator UX layer** of *Speak to the Fleet* — a voice-driven command interface for an unmanned maritime fleet, built for the defense-tech hackathon.

For LiveKit Agents conventions (uv, ruff, pytest, handoffs/tasks, the LiveKit CLI/MCP, TDD when modifying agent behavior) see @AGENTS.md. This file is the **project-specific** context that AGENTS.md does not cover.

## What "Speak to the Fleet" is

NATO is moving toward operating heterogeneous fleets of unmanned maritime systems (UUVs, USVs) under unified command. **STANAG 4817** is the in-development NATO standard for that interoperability; **CATL** is the publicly described messaging protocol inside it. The system prototypes two practical frictions:

1. Heterogeneous, vendor-specific sensor data normalised into a 4817-shaped wire format.
2. Operators tasking fleets by voice instead of clicking through nested console menus.

Real-time NMEA sensor input is parsed into CATL-shaped contact reports and published over UDP multicast to a live world model. An operator queries and tasks the fleet by voice; every utterance is resolved against world state, **confirmed by readback before dispatch**, and logged. Outbound task assignments are emitted as CATL-shaped envelopes on the same multicast bus.

## The three-role split

The system splits into three roles that communicate through documented contracts so each layer can be built and tested independently:

1. **Ingest & translation** — NMEA → CATL contact reports, published to multicast.
2. **World model & dispatch seam** — maintains live state, exposes an HTTP API, dispatches authorised tasks to the multicast bus. Lives in [b-service/](../../b-service/).
3. **Voice & operator UX** ← *this folder*, plus the map UI in [src/command-ui/](../command-ui/).

Do not reach across role boundaries. The world model is consumed via its HTTP API, never by reading its internal state directly. Dispatch happens through the world model's seam, not from this layer.

## The two-fleet split (load-bearing)

The world model holds **two kinds of platform**, distinguished by an `is_controllable` flag on each track. Both publish ContactReports in the same envelope shape — the flag is the only behavioural difference.

| kind                  | `is_controllable` | source                                | naming                              | operator can…   |
|-----------------------|-------------------|---------------------------------------|-------------------------------------|-----------------|
| **Ambient contacts**  | `False`           | replayed NMEA logs / synthetic ambient (real-world traffic) | ship-style: `MV Northern Star`, `FV Karlsvik`, `MV Stumholmen` (PCAP source) | query only      |
| **Fleet platforms**   | `True`            | simulated processes that subscribe to TaskAssignments and publish their own position | single-word distinctive: `Falcon`, `Raven`, `Osprey`, `Marlin`, `Tarpon` | query + task    |

The voice agent's resolver **only matches tasking against `is_controllable=True`**. Ambient contacts can appear in queries (and on the map) but are never valid task targets — attempts to task them must refuse with `UNKNOWN_CALLSIGN` rather than reveal that the contact exists but isn't ours.

## Voice agent design

Pattern: **one LiveKit agent, several tools, a small confirmation state machine**. Not a multi-agent handover system.

The agent itself can only **propose** tasks. Dispatch happens only when the operator's *next* utterance matches a confirmation phrase, checked by a deterministic phrase matcher — **not by the LLM**.

```
operator speaks
  └─► STT
      └─► if pending_task and is_confirmation(text):
            → call /dispatch
            → TTS "Dispatched, task X."
          elif pending_task and is_cancellation(text):
            → clear pending_task
            → TTS "Cancelled."
          else:
            → LLM with tools
            → tool call (resolve via /resolve, then propose_*)
            → TTS readback with RESOLVED params
```

- **Confirmation phrases** (deterministic match): `confirm`, `authorize`, `go ahead`.
- **Cancellation phrases** (deterministic match): `cancel`, `abort`, `stop`.
- **Readback rule**: the readback always uses the **resolved** values (platform ID, coordinates) returned by the tool, never the operator's raw utterance. This is what catches grounding errors before dispatch.
- **Confirmation/cancellation never reaches the LLM.** The state-machine intercepts those utterances first.

### Agent-internal tools vs operator-facing tools

The fleet-state query tools (`list_platforms`, `get_platform_state`) are **agent-internal grounding tools**, not operator-facing narration. The map UI shows fleet state — the agent should not read it aloud. The agent uses these tools to resolve references and validate before staging; it does **not** narrate query results back to the operator.

Operator-facing tool surface (these produce readbacks):
- `propose_task_waypoint(call_sign, lat, lon)` — stages a transit
- `propose_recall(call_sign)` — stages a transit to base
- `cancel_pending_task(pending_task_id)` — clears a stage before confirmation

Agent-internal tools (results are not spoken):
- `list_platforms()` / `get_platform_state(call_sign)` — for resolution and validation only

## Architectural rules (load-bearing)

These are the rules the voice layer must enforce. They exist for safety and auditability — do not relax them without an explicit conversation.

- **Dispatch is gated outside the LLM.** The LLM can only stage a task and produce a readback. A confirmation state machine — *not* the LLM — fires the actual command on the multicast bus after explicit operator authorisation. Never give the LLM a tool that dispatches directly.
- **Resolve before propose.** The agent must resolve fuzzy references to specific platform IDs against the world model before staging anything. Resolution is restricted to controllable platforms. No guessing on ambiguous references — ask the operator.
- **Readback before confirm.** Every staged task gets read back with the resolved IDs and parameters. The operator confirms separately; do not assume confirmation from conversational acknowledgements.
- **Voice is for tasking, the map is for awareness.** TTS is reserved for confirmation readbacks and refusals. Don't narrate fleet state — that's the map's job.
- **Everything is auditable.** Every utterance flows through to the audit trail — utterance, resolved entities, staged task, confirmation, dispatch. The audit-log UI traces this end-to-end.
- **CATL envelope shape is the contract.** Outbound tasks are CATL-shaped JSON over UDP multicast. The exact schema lives with the world model / dispatch seam — consume it from there, don't reinvent it here.

## Demo scope (90 seconds, tasking-heavy)

```
00:00  Map populated with ambient contacts (NMEA replay) + fleet (sim, idle).
00:15  "Task Falcon to 56.15 north, 15.58 east."   → readback → "Confirm." → dispatch
00:30  Platform visibly moves on map.
00:40  "Task Raven to 56.18 north, 15.64 east."    → readback → "Confirm." → dispatch
01:00  "Recall Falcon to base."                    → readback → "Confirm." → dispatch
01:20  Audit log shown — full trace, ingest to dispatch.
```

**Pitch line:** *"The map shows the world. Voice acts on it. The operator's attention never leaves the situation."* Voice is for tasking (where it beats clicking through nested menus by 5–10×). Map is for situational awareness. TTS is reserved for confirmation readbacks — voice's output role is verification, not narration.

## Component plan

- **Operator UI** ([src/command-ui/](../command-ui/)): start from the existing Next.js scaffold. For demo, a **simple HTML/JS page with Leaflet** is acceptable if faster than wiring into the React app. Subscribes to b-service `WS /api/v1/ws` for live track updates and renders an audit-log panel. **Must include a minimal mic-on/mic-off control** that connects to the LiveKit room so the operator can talk to the agent from the same surface — no separate terminal needed during demo.
- **Sim platforms**: Python, ~60 lines per platform, multicast pub/sub. Each sim subscribes to TaskAssignments matching its call sign and publishes its own ContactReport at intervals.
- **Transport**: UDP multicast via stdlib `socket` only. Unicast fallback via env var (e.g. `TRANSPORT_MODE=unicast`, `UNICAST_TARGET=host:port`) for environments where multicast is blocked.

## Current state of the code

This is hackathon-stage. Be honest about what is and isn't wired up:

- [src/agent.py](src/agent.py) — LiveKit agent with system prompt + six `@function_tool` methods on `Assistant` (`list_platforms`, `get_platform_state`, `task_waypoint`, `propose_recall`, `get_pending_task`, `cancel_pending_task`) wired through `MockWorldModel`. **Confirmation state machine is in place**: out-of-LLM `on_user_turn_completed` intercept matches strict `confirm | authorize | go ahead` / `cancel | abort | stop` against a `_pending_task`, with a 1.5 s echo-bleed guard, `asyncio.Lock`, structured audit logging, and `StopResponse` to skip the LLM. Greeting via `session.say(...)` is in place.
- [src/mock_world_model.py](src/mock_world_model.py) — in-memory stand-in for the world model HTTP API. `task_waypoint` produces a NATO-phonetic readback; `recall_to_base` produces a crisp `"<call sign>, recall to base. Confirm."` and stages with `BASE_LATITUDE=56.16`, `BASE_LONGITUDE=15.59` (Karlskrona harbor; matches b-service operator default). `Platform` carries `is_controllable`; fleet (`Falcon`, `Raven`, `Osprey`, `Marlin`, `Tarpon`) is `True`, ambient seed (`MV Northern Star`, `FV Karlsvik`, `MV Stumholmen`) is `False` — Stumholmen sits at the captured PCAP coordinate and is stationary. Both staging paths refuse ambient with `UNKNOWN_CALLSIGN`. Resolver is case-insensitive and tolerates STT-inserted fillers via tier-3 suffix match.
- [src/world.py](src/world.py) — empty `WorldModel` HTTP client stub. Real implementation needed before swapping out the mock.
- [b-service/](../../b-service/) — FastAPI world model. Has `/api/v1/ingest`, `/api/v1/tracks`, `WS /api/v1/ws`, `/api/v1/resolve`, `/api/v1/dispatch`. `CimTrack` carries `is_controllable` (defaults `False`); ingest endpoint accepts the flag; `_resolve` accepts `controllable_only`; dispatch validates the target is controllable; mapper emits `isControllable` to the wire. **`pcap_replay` import is referenced but the file doesn't exist** → no continual ingestion today; the world is empty until something POSTs to `/ingest`.
- STT/TTS: **Speechmatics** STT and **Cartesia sonic-3** TTS, with **ai-coustics** noise cancellation, all via LiveKit plugins.
- Tests live in [tests/](tests/). 11 unit tests cover the state machine. Three pre-existing eval tests (`test_offers_assistance`, `test_grounding`, `test_refuses_harmful_request`) currently fail because they construct `AgentSession()` with no LLM — pre-existing infra issue, not a regression. Per AGENTS.md, use TDD when modifying agent instructions, tools, or workflow structure.

## Punch list

Demo-blocking (in suggested order):

- [x] **1. Confirmation state machine in command-agent** — done. See "Voice agent design" above and [src/agent.py](src/agent.py) `on_user_turn_completed`.
- [x] **2. Two-fleet split (`is_controllable`)** — done. Flag added to `CimTrack` ([b-service/models.py](../../b-service/models.py)) and threaded through `/api/v1/ingest`, `_resolve` (with `controllable_only`), `/api/v1/dispatch` validation, and `mapper.map_track` ([b-service/main.py](../../b-service/main.py), [b-service/mapper.py](../../b-service/mapper.py)). Mock world ([src/mock_world_model.py](src/mock_world_model.py)) seeds ambient contacts and refuses ambient tasking with `UNKNOWN_CALLSIGN`. Fleet uses single-word call signs (`Falcon`, `Raven`, `Osprey`, `Marlin`, `Tarpon`) for STT-resilience.
- [x] **3. `recall_to_base` tool** — done. `BASE_LATITUDE=56.16`, `BASE_LONGITUDE=15.59` (Karlskrona harbor) constants in [src/mock_world_model.py](src/mock_world_model.py); `recall_to_base(call_sign)` world-model method; `propose_recall(call_sign)` `@function_tool` on `Assistant` honours `_pending_task` state and refuses with `PENDING_TASK_EXISTS` if a task is already staged. System prompt classifies recall as a separate task type and loosens the explicit-coords rule for it (call sign is the only required parameter). Readback shape: `"<call sign>, recall to base. Confirm."`
- [x] **4. Sim platforms** — done. [sims/sim_platform.py](../../sims/sim_platform.py) is a stdlib-only Python script that subscribes to UDP multicast `239.1.2.3:5000` for dispatch commands matching its `--call-sign`, transits toward the staged waypoint at `--speed` knots using flat-earth bearing math, and POSTs its position to b-service `/api/v1/ingest` every tick (default 1.0 s). [sims/launch.sh](../../sims/launch.sh) starts Falcon, Raven, and Tarpon concurrently. 9 unit tests in [sims/test_sim_platform.py](../../sims/test_sim_platform.py) cover movement, arrival, heading update, and dispatch parsing (target/non-target/malformed). End-to-end demo path: start b-service → `bash sims/launch.sh` → POST `/api/v1/dispatch` → sim moves on the map.
- [x] **5. Live ingestion** — done via [sims/ambient_replay.py](../../sims/ambient_replay.py): continuously POSTs four ambient surface contacts to `/api/v1/ingest` with `is_controllable=false`. Three are synthetic moving traffic in Karlskrona waters (`MV Northern Star`, `FV Karlsvik`, `MV Östersjön`, advancing on heading each 2 s, `source=SIM_AMBIENT`). The fourth — `MV Stumholmen` — is **stationary at the captured PCAP coordinate** (56.16080495°N, 15.56721734°E) and posts with `source=PCAP_REPLAY` so the audit trail can distinguish real-data points from synthetic ones. [sims/launch.sh](../../sims/launch.sh) bundles ambient replay with the controllable-fleet sims so the map populates from t=0.
- [x] **6. Map UI** — done as a single static file [src/command-ui/public/operator.html](../command-ui/public/operator.html) (~300 lines). Leaflet via CDN, OpenStreetMap tiles, centered on Karlskrona harbor. Connects to b-service `ws://<host>:8000/api/v1/ws`, renders one circle marker per track: **blue** for `isControllable=true`, **grey** for ambient, **amber** for the PCAP-sourced `MV Stumholmen`. Right sidebar has a clickable track list (pans + opens popup), a scrolling audit log of all track updates, and a mic on/off button that POSTs the existing Next.js `/api/connection-details` token endpoint to join the LiveKit room and toggle the microphone. WS auto-reconnects. Served at `http://localhost:3000/operator.html` once the Next dev server is running.
- [ ] **7. Audit log endpoint** — in-memory deque on b-service capturing ingest/resolve/stage/dispatch; `GET /api/v1/audit?limit=50`. Agent posts staged proposals to keep the trace complete.

Important but not strictly demo-blocking:

- [ ] **8. Real `WorldModel` HTTP client** ([src/world.py](src/world.py)) — async `httpx` client with the same method surface as `MockWorldModel`, so the agent can drive b-service `/api/v1/dispatch` instead of the mock (otherwise `mark_dispatched` never reaches multicast and the map doesn't move).
- [ ] **9. System-prompt cleanup** — explicitly mark `list_platforms`/`get_platform_state` as agent-internal (no narration). The orchestrator-intercept note is already in place.

Cleanup (low risk, high signal):

- [ ] Delete the dead `MOCK_PLATFORMS` / `LIST_PLATFORMS_RETURN` / etc constants at the top of [src/agent.py](src/agent.py) — leftover scaffolding, the state lives in `MockWorldModel` now.
- [ ] Wire an LLM into the three pre-existing eval tests in [tests/test_agent.py](tests/test_agent.py) (e.g. `AgentSession(llm=inference.LLM(model="..."))`), or rewrite them against the maritime persona.

## When extending the agent

- New tools go on the `Assistant` class via `@function_tool`, and they must call the world model through `WorldModel` (the HTTP client, once it exists) — not reach into shared state or other layers.
- A "propose task" tool returns a *staged* task plus a readback string. It must not have a side effect on the multicast bus. The dispatch path is a separate, non-LLM code path that the operator triggers.
- Confirmation/cancellation phrase matching belongs in the state machine, not the LLM. Don't try to teach the LLM to detect "confirm" — intercept it before the LLM sees it.
- Query tools (`list_platforms`, `get_platform_state`) are for the agent's own grounding. Don't add prompts that ask the LLM to verbally summarize them — the map is the surface for that.
- If a workflow grows past a couple of tools or phases, prefer LiveKit handoffs/tasks over a longer system prompt (see AGENTS.md → "Handoffs and tasks").
- Voice-output rules already in the system prompt (plain text, no markdown/JSON, spell out numbers, no acronyms with unclear pronunciation) are TTS-correctness rules — preserve them on any prompt edit.

## Quick commands

```bash
uv run python src/agent.py download-files   # one-time: VAD + turn detector
uv run python src/agent.py console          # speak to the agent in the terminal
uv run python src/agent.py dev              # run for use with frontend / telephony
uv run pytest                               # tests + evals
uv run ruff format && uv run ruff check     # formatting + lint
```
