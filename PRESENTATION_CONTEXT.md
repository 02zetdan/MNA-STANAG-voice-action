# Speak to the Fleet — Presentation Context

> Self-contained context for an LLM that has not seen this codebase. Use it to draft slides, a script, or a live demo narration. Everything below is grounded in the actual repo state on `master` as of 2026-05-10.

The presentation must answer four questions, in order:

- **a)** What problem are you solving?
- **b)** How are you solving the problem technically?
- **c)** How will this be deployed and mass-manufactured?
- **d)** What have you achieved during the hackathon?

The four sections below give you everything you need to answer each one. After them, §5–§8 carry supporting detail (architecture diagram, demo script, stack, slide outline) you can pull from as needed.

---

## a) What problem are you solving?

**Two real, compounding frictions in unmanned maritime command.**

NATO is moving toward operating heterogeneous fleets of unmanned maritime systems — UUVs, USVs — under unified command. The in-development standard is **STANAG 4817**; the publicly described messaging protocol inside it is **CATL**. In that world the operator faces two concrete problems:

1. **Heterogeneous, vendor-specific sensor data.** Every platform speaks its own dialect — NMEA-0183 for surface GNSS, vendor binaries for sonar, AIS for civilian traffic. The operator needs **one normalised picture of the world**, not ten consoles.
2. **Console UX does not scale to fleet command.** Tasking an unmanned platform by clicking through nested menus is slow, eyes-down, and pulls attention off the situation. **Voice can be 5–10× faster for tasking** — *if* it is grounded, confirmed, and auditable. Most "AI voice control" demos are none of those, which is why no one trusts them with real tasking authority.

The deeper problem behind both: **a voice control surface for a defence-grade system has to be safe by construction.** An LLM that can directly dispatch a vessel is unacceptable. The interesting design question is how you keep the convenience of natural language *and* the safety of a deterministic, auditable command path.

That is the problem we are solving.

---

## b) How are you solving the problem technically?

A three-layer system communicating through documented contracts. Each layer can be built and tested independently. The architecture is intentionally **CATL-shaped** — same multicast pub/sub topology STANAG 4817 deployments use, JSON envelopes in place of the ratified wire format, so the design is portable to a real deployment.

### The three layers

| layer                         | responsibility                                                          | location                                                                       |
| ----------------------------- | ----------------------------------------------------------------------- | ------------------------------------------------------------------------------ |
| **A. Ingest & translation**   | NMEA-0183 → CATL-shaped contact reports, published to the world model   | [src/parser.py](src/parser.py)                                                 |
| **B. World model & dispatch** | Live state, HTTP API, dispatch on multicast bus, audit log, WS broadcast | [b-service/](b-service/)                                                       |
| **C. Voice & operator UX**    | LiveKit voice agent + Leaflet map UI with mic                           | [src/command-agent/](src/command-agent/), [src/command-ui/](src/command-ui/)   |

### The grounded-voice loop

```
operator speaks
  └─► STT (Speechmatics)
      └─► if pending_task and is_confirmation(text):
            → POST /api/v1/dispatch     (deterministic phrase match — NOT the LLM)
            → TTS "Dispatched, task X."
          elif pending_task and is_cancellation(text):
            → clear pending_task
            → TTS "Cancelled."
          else:
            → LLM with tools
            → tool calls /api/v1/resolve  (ground fuzzy reference → platform ID)
            → tool stages a task and returns a readback
            → TTS reads back the RESOLVED parameters
```

Every utterance flows: **STT → grounding (resolve) → LLM proposes a tool call → readback with resolved IDs → operator says "confirm" → deterministic phrase match → dispatch on the multicast bus → audit log.**

### Five architectural rules — load-bearing for the safety story

1. **Dispatch is gated outside the LLM.** The LLM can only *stage* a task and produce a readback. A deterministic phrase matcher (not the LLM) fires the actual command after explicit operator authorisation. The LLM never has a tool that dispatches directly.
2. **Resolve before propose.** The agent resolves fuzzy references ("the closest one", "Falcon") to specific platform IDs against the world model before staging anything.
3. **Readback before confirm.** Every staged task is read back with the **resolved** IDs and parameters, never the raw utterance — that is what catches grounding errors before they reach the bus.
4. **Confirmation phrases are deterministic, not LLM-judged.** `confirm | authorize | go ahead` → dispatch. `cancel | abort | stop` → clear stage. The state machine intercepts these *before* the LLM sees them.
5. **Everything is auditable.** Utterance → resolved entity → staged task → confirmation → dispatch is captured end-to-end and visible to the operator in real time.

### The two-fleet split (the safety property worth a slide of its own)

The world model holds **two kinds of platform**, distinguished by an `is_controllable` boolean on each track. Both publish the same envelope shape — the flag is the only behavioural difference.

| kind                  | `is_controllable` | source                                              | example call signs                                            | operator can…   |
|-----------------------|-------------------|-----------------------------------------------------|---------------------------------------------------------------|-----------------|
| **Ambient contacts**  | `False`           | NMEA replay / synthetic ambient (real-world traffic) | `MV Northern Star`, `FV Karlsvik`, `MV Stumholmen` (PCAP)     | **query only**  |
| **Fleet platforms**   | `True`            | sim processes that subscribe to TaskAssignments     | `Falcon`, `Raven`, `Osprey`, `Marlin`, `Tarpon`               | query + **task** |

`is_controllable` is enforced in **three** places — agent resolver, world-model `/api/v1/resolve?controllable_only=true`, and `/api/v1/dispatch` validation. Tasking an ambient contact refuses with `UNKNOWN_CALLSIGN` — same UX as an unknown call sign, deliberately not revealing that the contact exists but isn't ours.

Single-word fleet call signs (`Falcon`, `Raven`, …) chosen for **STT resilience**; multi-word names get mangled by speech recognition.

### Per-track quality scoring

The world model computes a quality vector from GNSS fix quality + sat count + HDOP and clamps it to [0, 1]. Stale or lower-quality updates lose to fresher, higher-quality ones — the world model never overwrites a confident fix with a degraded one within a 5-second window. This is the foundation for trustworthy voice grounding: the agent resolves against state the world model has already vetted.

### Voice / audio stack

LiveKit Agents (Python) + **Speechmatics** STT + **Cartesia sonic-3** TTS + **ai-coustics** noise cancellation. One agent, several `@function_tool` methods, a small confirmation state machine — not a multi-agent handover system.

---

## c) How will this be deployed and mass-manufactured?

The unit of "manufacture" here is software, not hardware — but the architecture is deliberately shaped so that scaling out is a matter of **replicating small, stdlib-only processes onto existing platforms**, not redesigning the system.

### Deployment topology

A single operator post runs three things:

1. **The world model service** — one FastAPI process, one machine, ~20 MB resident.
2. **The voice agent** — one LiveKit Agents process; can be self-hosted or run on LiveKit Cloud.
3. **The operator UI** — a static HTML page + the Next.js token-endpoint for LiveKit; served from any web server.

Each unmanned platform runs **one small subscriber process** that joins the dispatch multicast group and publishes its own contact reports. Today that subscriber is [sims/sim_platform.py](sims/sim_platform.py) — **stdlib-only Python** (`socket`, `urllib`, `threading`), ~300 lines, no external deps. Drop it onto a UUV/USV's onboard computer and it is on the bus.

### Why the architecture supports mass replication

- **No bespoke wire format.** CATL-shaped JSON over UDP multicast runs on commodity Linux without kernel modules, without a vendor stack, and without ratification gating. A platform manufacturer can integrate by implementing one POST and one multicast subscriber.
- **Pluggable transport.** The default is UDP multicast; a unicast fallback is selected by env var (`TRANSPORT_MODE=unicast`, `UNICAST_TARGET=host:port`) for environments where multicast is blocked. No code change to switch.
- **Layered contracts mean per-vendor work is bounded.** Adding a new sensor dialect means writing one ingest adapter (Layer A) — the world model and voice agent do not change. Adding a new fleet platform vendor means writing one subscriber stub — the world model does not change.
- **Stateless, replicable services.** The world model is in-memory; an operational deployment swaps the dict for Redis/Postgres without changing the API surface. The voice agent is stateless per-session.
- **STANAG-4817-portable.** Because the topology is the same as a real CATL deployment (multicast pub/sub with envelope-shaped messages), the only thing that changes when porting to the ratified wire format is the codec on each end of the bus.

### Production hardening path (ordered)

1. Replace the agent's `MockWorldModel` with the real HTTP client at [src/command-agent/src/world.py](src/command-agent/src/world.py) (currently a stub).
2. Persist the world model and audit log to durable storage.
3. Replace JSON envelopes with the ratified CATL wire format on a per-link basis (codec at the bus boundary; nothing above it changes).
4. Authenticate the multicast bus (per-platform keys, signed envelopes).
5. Run the voice agent under a hardened LiveKit deployment (self-hosted or LiveKit Cloud); add per-operator auth on the mic endpoint.

### Per-platform manufacturing cost

A new platform integration is **one Python file plus a call sign**. The sim platform is a working reference: ~300 lines, stdlib only, runs on anything that runs Python 3.10+.

---

## d) What have you achieved during the hackathon?

Honest version, written so the audience can trust the rest of the talk.

### Wired up and live

- **NMEA-0183 ingest with quality scoring.** Real PCAP captures from [src/kraken_data/](src/kraken_data/) feed [src/parser.py](src/parser.py), which fuses RMC + HDT + GGA, computes a quality vector, and POSTs to the world model. Captured stationary contact at 56.16080495°N, 15.56721734°E renders as `MV Stumholmen` (amber) on the map to distinguish PCAP-sourced data from synthetic.
- **The world model service** ([b-service/main.py](b-service/main.py)) — `/api/v1/ingest`, `/api/v1/tracks`, `WS /api/v1/ws`, `/api/v1/resolve` (with `controllable_only`, spatial intent, multilingual tokens), `/api/v1/dispatch` (controllable-only validation, UDP emit on `239.1.2.3:5000`), and the audit endpoints (`GET /audit`, `WS /audit/stream`, `POST /audit/event`).
- **The two-fleet split end-to-end.** `is_controllable` threaded through `CimTrack`, ingest, resolve, dispatch, mapper, and the wire envelope. Refusal UX does not leak existence of ambient contacts.
- **Voice agent confirmation state machine.** Out-of-LLM `on_user_turn_completed` intercept matches strict `confirm | authorize | go ahead` / `cancel | abort | stop` against `_pending_task`, with a 1.5 s echo-bleed guard, `asyncio.Lock`, structured audit logging, and `StopResponse` to skip the LLM. **11 unit tests covering the state machine.**
- **`recall_to_base` tool** with Karlskrona harbor base coordinates (`56.16, 15.59`); call sign is the only required parameter; readback shape `"<call sign>, recall to base. Confirm."`
- **Sim platforms** — Falcon, Raven, Tarpon. Stdlib-only; subscribe to dispatch multicast, transit using flat-earth bearing math, post position back to ingest. **9 unit tests** covering movement, arrival, heading, dispatch parsing.
- **Ambient replay** ([sims/ambient_replay.py](sims/ambient_replay.py)) — three synthetic moving contacts plus the stationary PCAP contact, so the map populates from t=0.
- **Operator UI** ([src/command-ui/public/operator.html](src/command-ui/public/operator.html)) — Leaflet + OpenStreetMap centered on Karlskrona, blue/grey/amber markers, clickable track list, scrolling audit panel from `WS /api/v1/audit/stream`, mic on/off button that joins a LiveKit room via the existing Next.js token endpoint. WS auto-reconnects.
- **End-to-end audit trail.** Every meaningful event — first-contact ingest, resolve, resolve_failed, stage, cancel, dispatch_emitted, dispatch_refused — appears in the operator's audit panel within the same second it happens.

### What is mocked or partial (be honest in the talk)

- **`MockWorldModel`** is the agent's current backend. The real HTTP client at [src/command-agent/src/world.py](src/command-agent/src/world.py) is a stub. Demo currently dispatches via the world-model endpoint directly.
- **Three pre-existing eval tests** in [tests/test_agent.py](src/command-agent/tests/test_agent.py) fail because they construct `AgentSession()` with no LLM — pre-existing infra issue, not a regression.

### What changed — at a glance

Five recent commits give a clean story:

```
e2b83a1 Merge full-integration: audit log, operator UI, real WorldModel + cleanup
07863df Audit log + real WorldModel client + deterministic readbacks + cleanup
ccae419 Operator UI: Leaflet map, mic, audit panel, dispatch path lines
a25050e feat(integration): connect Layer A NMEA parser to Layer B World Model
```

### Three things worth taking away

1. **A grounded voice loop with deterministic safety boundaries** — the LLM proposes; a state machine outside the LLM authorises; readback uses resolved IDs. Generalises to any voice control surface where mistakes have weight.
2. **A two-fleet model for command surfaces** — `is_controllable` is one boolean that solves "voice agent must never accidentally task the wrong thing." Enforced in three layers, with refusal UX that doesn't leak.
3. **STANAG-4817-shaped data plumbing on commodity transports** — same multicast pub/sub topology, JSON envelopes instead of the ratified wire format, so the architecture is portable to a real CATL deployment.

---

## §5 Architecture diagram (for the technical slide)

```
NMEA-0183 sensors / replays            ┌──────────────────────────┐
   (UDP multicast 239.192.43.79:4379)  │      LIVE WORLD MODEL     │
            │                          │   (b-service, FastAPI)    │
            ▼                          │                           │
   parser.py  ──ingest──►   /api/v1/ingest  ──►  CimTrack store    │
                                        │       (per-track quality, │
                                        │        is_controllable)   │
   sim platforms (Falcon, Raven, …) ───►│                           │
            ▲                           │       /api/v1/resolve     │
            │   dispatch UDP 239.1.2.3  │       /api/v1/dispatch    │
            │   ◄──────────────────────  WS    /api/v1/ws (live)    │
            │                            WS    /api/v1/audit/stream │
            │                          └─────────────┬─────────────┘
            │                                        │
            │                                        ▼
            │                              ┌────────────────────┐
            │                              │   OPERATOR UI       │
            │                              │   (Leaflet + mic)   │
            │                              │   operator.html     │
            │                              └─────────┬──────────┘
            │                                        │ mic on/off
            │                                        ▼
            │                              ┌────────────────────┐
            │                              │  VOICE AGENT        │
            │                              │  (LiveKit, Python)  │
            │                              │  STT + LLM + TTS    │
            │                              └─────────┬──────────┘
            │                                        │
            └────── /api/v1/dispatch ◄── confirm ────┘
                    (multicast bus)
```

## §6 The 90-second demo script

```
00:00  Map populated with ambient contacts (NMEA-style replay) + fleet (sim, idle).
00:15  "Task Falcon to 56.15 north, 15.58 east."   → readback → "Confirm." → dispatch
00:30  Falcon visibly transits across the map.
00:40  "Task Raven to 56.18 north, 15.64 east."    → readback → "Confirm." → dispatch
01:00  "Recall Falcon to base."                    → readback → "Confirm." → dispatch
01:20  Audit log shown — full trace, ingest → resolve → stage → confirm → dispatch.
```

Two safety beats worth showing on a slide:

- **Refusal beat:** "Task `MV Northern Star` to 56.15 north." → agent refuses with `UNKNOWN_CALLSIGN`. Demonstrates the controllable-only resolver.
- **Cancel beat:** "Task Raven to 56.18 north, 15.64 east." → readback → "Cancel." → stage cleared, no dispatch. Demonstrates the deterministic phrase matcher.

## §7 Tech stack at a glance

| layer | tech |
|---|---|
| ingest parser | Python, `pynmea2`, stdlib socket multicast |
| world model | FastAPI, Pydantic, asyncio, WebSockets, UDP multicast |
| voice agent | LiveKit Agents (Python), Speechmatics STT, Cartesia sonic-3 TTS, ai-coustics noise cancellation |
| sim platforms | stdlib only (`socket`, `urllib`, `threading`) |
| operator UI | static HTML + Leaflet + OpenStreetMap; Next.js scaffold for the LiveKit token endpoint |
| transport | UDP multicast (`239.192.43.79:4379` ingest, `239.1.2.3:5000` dispatch); unicast fallback via env vars |

## §8 Suggested slide outline

1. **Title** — *Speak to the Fleet.* Voice command for unmanned maritime fleets.
2. **(a) The problem** — STANAG 4817 / CATL, heterogeneous sensor dialects, click-through console UX vs voice, why naive voice control isn't safe.
3. **(b) The solution — the loop** — diagram from §5, plus the five architectural rules from §b.
4. **(b) Three layers + two fleets** — the role table and the `is_controllable` table.
5. **(b) The safety story** — LLM proposes, state machine authorises, readback with resolved IDs, audit trail.
6. **(c) Deployment & manufacture** — single operator post + one stdlib subscriber per platform; CATL-shaped on commodity transports.
7. **(d) What we built** — the "wired up and live" list; honest about what is mocked.
8. **Live demo** — 90 seconds, ending on the audit log.
9. **Pitch line** — *"The map shows the world. Voice acts on it. The operator's attention never leaves the situation."*
