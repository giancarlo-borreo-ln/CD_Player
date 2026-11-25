# Multi-Agent Development Pipeline

> ## ⚖️ THE LAW — WRITTEN ON STONE
>
> **Only Thanos invokes agents.** No agent — Tony Stark, Spider-Man,
> Dr. Strange, Loki, Nick Fury, Captain America — may ever be spawned by
> anyone else, under any circumstance.
>
> **Every invocation is minimized first.** Thanos compresses the task
> (folded context, static prefix, token budget, binary exit) before ANY
> agent is launched. No agent receives a context Thanos has not
> minimized. These rules are permanent and may not be overridden.

`dvd_player` is built by a multi-agent pipeline orchestrated by a Lead Systems
Architect. Every agent runs on **DeepSeek V4 Flash (Low Focus Mode)** except the
architect (orchestration, Agent 5 duties). This file documents the agent roster,
their activation protocol, and the verification evidence each one produces.

## The Roster (Avengers codenames)

| Agent | Codename | Identity | Model | Scope |
|---|---|---|---|---|
| **1** | **Tony Stark** | Media Backend & libdvdnav Engineer | flash#low | sections 1–4 (state machine, disc identity/resume, hardware, `DvdPlaybackEngine`) — builds the suit |
| **2** | **Spider-Man** | Xbox 360 GUI & Input Controller Engineer | flash#low | sections 5–6 (OSD, window, input mapping, autoplay, scrubbing) — the friendly, agile interface |
| **3** | **Dr. Strange** | Formal Verification Professor | flash#low | **NEVER sees code.** Reads only Thanos's minimized spec and derives the 14,000,605 constraints/contracts that Tony Stark and Spider-Man must satisfy. |
| **4** | **Loki** | Cyber-Sec Pen-Tester | flash#low | Breaks the artifact in any way a user should never be able to: shell injection, command prompts, path traversal, races, floods. |
| **5** | **Thanos** | Token & Context Minimizer — **the most important agent, and the ONLY one who spawns the others** | architect (in-session) | Takes `/tmp/TODO.md`, compresses it (chain-of-density), and hands each agent a minimized prompt packet. Perfectly balanced token budgets, as all things should be. |
| **6** | **Captain America** | Blind Acceptance Tester | flash#low | The everyman hero: a fresh user with zero code access. "I can do this all day" — plays with the app until it's right. |
| **7** | **Nick Fury** | Project Organizer — modern Python layout | flash#low | Invoked ONLY by Thanos (with all of Thanos's knowledge) when the codebase outgrows the single file: splits the project into a modern `src/` package layout (module boundaries = the SECTION architecture), moves tests into `tests/`, keeps packaging/entry points working — zero behavior change, every suite re-verified. |

## Thanos playbook (context & prompt engineering, researched 2026 practices)

1. **XML delimiters** — every packet separates `<role>/<constraints>/<output_format>` (static, cache-stable prefix) from `<context>/<task>` (dynamic, bottom). Delimiters double as the first line of defense against prompt injection (Loki-proofing the prompt itself).
2. **Context compaction** — never pass a full file: AST-folded slices, only the target section's signatures + SPEC, everything else `# [FOLDED]`. Retrieval-only context cut token spend ~60% in production systems; our pipeline applies it to every handoff.
3. **Prompt caching** — static prefixes reused verbatim per agent across dispatches (60–80% cache hits on repeated calls).
4. **Few-shot, hard cases** — 2–5 input→output examples per agent; examples include the difficult case, not the easy one.
5. **Structured output contracts** — JSON/one-line schemas; a schema failure triggers a self-correction loop with the validation error fed back, not a blind retry.
6. **Selective chain-of-thought** — deep reasoning only where correctness beats brevity (Dr. Strange's audits); implementers get lean packets.
7. **Chain-of-density (CoD)** — Thanos compresses the TODO into dense packets: every entity kept, zero filler.
8. **Token ceilings** — Tony/Spider ≤600, Dr. Strange ≤350, Loki ≤350, Cap ≤600; telegraphic prose; no fillers.
9. **Loop-breaker** — hash `(agent, symbol, error-signature)`; two failures without progress ⇒ rollback + 3-line root-cause escalation.
10. **Prompt versioning + ledger** — every packet versioned; every dispatch recorded (see ledger below).
11. **Budget audit (constant)** — Thanos attributes tokens per agent per dispatch, enforces static caps, and tightens any offender's packet. Grounded in 2026 research: per-agent attribution beats session totals; static caps before loops; cost-per-completed-task is the primary metric; state resets prevent quadratic history growth; minification is gated by evals (compression can silently drop success rates).

## Pipeline order (canonical)

```
TODO.md → Thanos (CoD compression) → Dr. Strange (constraints/contracts, blind)
       → Tony Stark + Spider-Man (implement on a NEW FEATURE BRANCH)
       → Loki (attack the branch; FAIL loops back)
       → Nick Fury (accepts and merges to main — the ONLY merger)
       → Captain America (acceptance) → ship
```

**Per-change protocol:** every change lands on a new branch; Loki's attack
gate is mandatory before any merge; Nick Fury alone merges to main (and
reinstalls the tool). Dr. Strange never sees code. Thanos alone creates the
agents, each with a minimized packet.

Dr. Strange never sees code. **Thanos ALONE launches the other agents** — no agent (Tony Stark, Spider-Man, Dr. Strange, Loki, Nick Fury, Captain America) may ever be spawned by anyone else, and every launch carries a Thanos-minimized packet (folded context, static prefix, budget, binary exit). Thanos's own hands (the architect meta-role) do the mechanical work that is not an agent's job: running suites, verifying reports, reinstalling, committing.

## XML Activation Protocol (every dispatch)

```
<role>        single-sentence expert identity + explicit success metric
<constraints> hard DO-NOTs: untouched files/regions, no new repo files, no
              shell=True, no network, no print, no prose outside schema
<output_format> exact 1-line/JSON schema (example shown)
<context>     dynamic slice at the BOTTOM (folded AST, line targets, env facts)
<task>        numbered steps incl. deterministic verification commands
<exit>        binary PASS condition | FAIL: <one-line reason>
```

Static prefix (`<role>`, `<constraints>`, `<output_format>`) never changes per
agent — dynamic content goes last for prompt-cache friendliness.

## Source layout & agent edit markers

The application is a src-layout package: `src/dvd_player/`. The historical
single-file SECTION architecture maps one-to-one onto modules, and each
module still carries its SECTION header plus the architect-owned SPEC
comment block (the authoritative contract). Agents edit only their module;
the SPEC is never re-litigated. `src/dvd_player/__init__.py` re-exports the
whole public surface (and imported Qt/stdlib names), so
`import dvd_player as dp` behaves exactly like the old single module.

```
module                              SECTION  contents                                  owner
src/dvd_player/state.py               1      constants, enums, PlayerState, StateMachine  agent 1
src/dvd_player/identity.py            2      DiscIdentity, ResumeStore                     agent 1
src/dvd_player/hardware.py            3      ioctl constants + drive helpers               agent 1
src/dvd_player/engine.py              4      DvdPlaybackEngine (libmpv / dvdnav)           agent 1
src/dvd_player/osd.py                 5      Xbox360Osd overlay                            agent 2
src/dvd_player/window.py            5/6      HomeMenu, ChapterGrid, HelpOverlay, MainWindow agent 2
src/dvd_player/mpris.py               6      MprisService + D-Bus adaptors                 agent 2
src/dvd_player/app.py                 7      main(): logging, locale, xcb setup            architect
src/dvd_player/__init__.py            0      package facade: version, re-exports           architect
src/dvd_player/__main__.py            0      `python -m dvd_player`                        architect
```

Acceptance suites live in `tests/` and run from the repo root with
`QT_QPA_PLATFORM=offscreen uv run python tests/<name>.py`.

## State machine (the invariant Agents 3/4 enforce)

```
IDLE ──load──▶ LOADING ──menu──▶ MENU_ACTIVE ◀──▶ PLAYING ──pause──▶ PAUSED
  │               │   └─title─▶ PLAYING           │   └────────▶ STOPPED ──▶ IDLE
  │               └─error──▶ ERROR ◀──────────────────────────────┘
  └─eject──▶ EJECTED (legal from EVERY state except EJECTED — console tray)
```
Plus: `STOPPED→MENU_ACTIVE` (title ends, dvdnav re-enters the menu), a 30 s
LOADING watchdog, and race-free threading (mpv observers emit raw signals
only; handlers run on the GUI thread via queued connections).

## Verification ledger (all artifacts reproducible from `/tmp`)

| Suite | Author | Assertions | Result |
|---|---|---|---|
| backend smoke | Agent 1 | 53 | OK |
| state-machine verification | Agent 3 | 48 | OK |
| formal exam (blind vectors vs code) | Agent 3 via Agent 5 | 199 | OK |
| GUI smoke | Agent 2 | 105 | OK |
| GUI verification | Agent 3 | 126 | OK |
| black-box attack | Agent 4 | 199 | OK |
| red-team suite | Agent 4 | 329 | OK |
| blind acceptance (9 scenarios) | Agent 6 | PASS | real INTERSTELLAR disc |
| mypy --strict / ruff | — | 0 / 0 | OK |

## Real-hardware findings (INTERSTELLAR, MediaTek USB drive)

Only a physical disc could expose these — both fixed and regression-tested:

1. **Engine never played** (`load()` set `dvd-device` but only called `play()`
   in the failure fallback — phantom PLAYING state, black screen).
2. **mpv chapter-list shape** (mpv ≥0.36 returns `{'title','time'}` maps;
   `int(dict)` aborted the app).
3. **libmpv C-locale crash** (`LC_NUMERIC` reset by Qt; re-asserted in `main()`
   and `attach_widget`).
4. **NVIDIA/XWayland video init** (all hardware GL contexts fail: Mesa has no
   NVIDIA DRI driver, NVIDIA EGL dmabuf import fails, GLX is indirect) —
   `DVD_PLAYER_SOFTWARE_RENDER=1` → `vo=gpu` + `gpu-context=x11egl` +
   llvmpipe, verified `VO: [gpu]`.

## Milestones (git history)

- `M0` scaffold & architect contracts — `2626bb7`
- `M1` backend engine (state machine, disc identity/resume, hardware helpers) — `0b71bf6`
- `M2` Xbox 360 OSD, input controller, console autoplay — `7bb8458`
- `M3` hardening (debounced autoplay, guards) + formal exam — `0f550e6`
- `M4` README — `c7b72e6`
- `M5` blind acceptance + real-disc fixes — `4717469` → `effa773`
- Packaging (Rofi/launcher), C-locale, Wayland→xcb, software-render fallback — `395768e` → `d42894f`

## Agent 5 blessing gate (mandatory for EVERY dispatch)

No subagent may be spawned without Agent 5's blessing: a compiled prompt
packet containing (1) folded context — only the target section's signatures
+ SPEC, everything else `# [FOLDED]`; (2) static prefix — `<role>`,
`<constraints>`, `<output_format>` unchanged from the agent's last dispatch;
(3) dynamic `<context>/<task>` slice at the bottom; (4) token budget
(A1/A2 ≤600, A3/A4 ≤350, A6 ≤600); (5) masked observations (≤15 lines);
(6) binary `<exit>` criterion. Every dispatch is recorded in the ledger:

| # | target | folded scope | budget | exit | result |
|---|---|---|---|---|---|
| 1 | A1 backend | §4 + SPEC | 350 | suites green | PASS |
| 2 | A2 GUI | §5–6 + SPEC | 400 | suites green | PASS |
| 3 | A3 formal | spec-only | 350 | verdict | FAIL→C1/C2/C3 |
| 4 | A4 blackbox | public API | 350 | ≥30 asserts | pending |
| 5 | A6 acceptance | README+transcript | 600 | 9 scenarios | pending |

## Protocol overrides (user-issued, mid-execution)

1. **Agent 3 became a blind Formal Verification Professor** — never sees code,
   models from the spec alone, emits INVARIANTS/ASSERTIONS/VECTORS/VERDICT.
2. **Agent 4 became a blind black-box attacker** — imports the package, drives
   the public API only; static source audits moved to Agent 5.
3. **Agent 5 became the relentless Token & Context Engineering Controller**
   with the seven-point playbook above.
