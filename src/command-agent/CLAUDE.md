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
2. **World model & dispatch seam** — maintains live state, exposes an HTTP API, dispatches authorised tasks to the multicast bus.
3. **Voice & operator UX** ← *this folder*.

Do not reach across role boundaries. The world model is consumed via its HTTP API, never by reading its internal state directly. Dispatch happens through the world model's seam, not from this layer.

## What this layer is

A LiveKit Agents service (reused from a production voice-agent stack), entrypoint [src/agent.py](src/agent.py). It runs a small LLM with tools that map directly onto the world model's HTTP API:

- list platforms
- query a specific platform's state
- **propose** a task (stage only — never dispatch)

Alongside the agent, this layer also owns the operator UI: a live map of platforms and their assignments, plus an audit-log view that traces every utterance through to dispatch.

## Architectural rules (load-bearing)

These are the rules the voice layer must enforce. They exist for safety and auditability — do not relax them without an explicit conversation.

- **Dispatch is gated outside the LLM.** The LLM can only stage a task and produce a readback. A confirmation state machine — *not* the LLM — fires the actual command on the multicast bus after explicit operator authorisation. Never give the LLM a tool that dispatches directly.
- **Resolve before propose.** The agent must resolve fuzzy references to specific platform IDs against the world model before staging anything. No guessing on ambiguous references — ask the operator.
- **Readback before confirm.** Every staged task gets read back with the resolved IDs and parameters. The operator confirms separately; do not assume confirmation from conversational acknowledgements.
- **Everything is auditable.** Every utterance flows through to the audit trail — utterance, resolved entities, staged task, confirmation, dispatch. The audit-log UI traces this end-to-end.
- **CATL envelope shape is the contract.** Outbound tasks are CATL-shaped JSON over UDP multicast. The exact schema lives with the world model / dispatch seam — consume it from there, don't reinvent it here.

## Current state of the code

This is hackathon-stage. Be honest about what is and isn't wired up:

- [src/agent.py](src/agent.py) — LiveKit agent skeleton with the operator-facing system prompt enforcing the rules above. **No tools are implemented yet** — the `function_tool` decorator is referenced in a comment only.
- [src/world.py](src/world.py) — empty `WorldModel` HTTP client stub. This is where the `list_platforms` / `get_platform` / `propose_task` tool implementations should sit, fronting the world model's HTTP API.
- STT/TTS today: **Deepgram nova-3** for STT and **Cartesia sonic-3** for TTS, via LiveKit Inference. The product spec calls for **Speechmatics** STT — not yet integrated. If you swap STT, do it through the LiveKit plugin surface; do not call provider SDKs directly from agent code.
- Tests live in [tests/](tests/). Per AGENTS.md, use TDD when modifying agent instructions, tools, or workflow structure.

## When extending the agent

- New tools go on the `Assistant` class via `@function_tool`, and they must call the world model's HTTP API through `WorldModel` — not reach into shared state or other layers.
- A "propose task" tool returns a *staged* task plus a readback string. It must not have a side effect on the multicast bus. The dispatch path is a separate, non-LLM code path that the operator triggers.
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
