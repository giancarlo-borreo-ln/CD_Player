# Multi-Agent Development Pipeline

`dvd_player` is built by a multi-agent pipeline orchestrated by a Lead Systems
Architect. Every agent runs on **DeepSeek V4 Flash (Low Focus Mode)** except the
architect (orchestration, Agent 5 duties). This file documents the agent roster,
their activation protocol, and the verification evidence each one produces.

## The Roster

| Agent | Identity | Model | Scope |
|---|---|---|---|
| **1** | Media Backend & libdvdnav Engineer | flash#low | `dvd_player.py` sections 1–4 (`StateMachine`, `DiscIdentity` + `ResumeStore`, hardware helpers, `DvdPlaybackEngine`) |
| **2** | Xbox 360 GUI & Input Controller Engineer | flash#low | sections 5–6 (`Xbox360Osd`, `MainWindow` — video embed, OSD, D-pad keys/gamepad, autoplay) |
| **3** | Formal Verification Professor | flash#low | **Black-box** mathematical auditor: receives ONLY the stripped spec (signatures + state-transition table + I/O contracts, all method bodies folded away). Derives safety/liveness invariants, pre/post conditions and executable boundary vectors; audits the SPEC itself for contradictions. |
| **4** | Black-Box Red-Team Attacker | flash#low | Attacks the shipped artifact with zero source access: drives only the documented public API/signals with hostile inputs (injection, NaN/inf seeds, dialog floods, event floods, lifecycle abuse, state fuzz). |
| **5** | Token & Context Engineering Controller | architect (in-session) | Compiles every handoff: AST body-folding, static-prefix prompts, `SHARED_STATE`/CLUES handoffs, ≤15-line masked observations, diff-only agent outputs with token ceilings (A1/A2 ≤600, A3/A4 ≤350), loop-breaker circuit (two failures on the same `(agent, symbol, error-signature)` without progress ⇒ rollback + root-cause escalation). |
| **6** | Blind Acceptance Tester | flash#low | A fresh user with zero code access: reads only `README.md` + the UAT transcript, uses the app like a normal user, and judges the Xbox 360 experience. Final gate of every release. |

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

`dvd_player.py` is the single application file. Agent-owned regions are
delimited by `# ── [AGENTn: SECTION k BEGIN/END] ──` marker pairs; the
architect-owned SPEC comment block directly above each region is the
authoritative contract. Agents edit only between their markers (plus the
Section 0 import block); the SPEC is never re-litigated.

```
SECTION 0  imports / framework shim        architect
SECTION 1  constants, enums, state machine agent 1
SECTION 2  DiscIdentity + ResumeStore      agent 1
SECTION 3  hardware helpers                agent 1
SECTION 4  DvdPlaybackEngine (libmpv)      agent 1
SECTION 5  Xbox360Osd                      agent 2
SECTION 6  MainWindow                      agent 2
SECTION 7  main()                          architect
```

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

## Protocol overrides (user-issued, mid-execution)

1. **Agent 3 became a blind Formal Verification Professor** — never sees code,
   models from the spec alone, emits INVARIANTS/ASSERTIONS/VECTORS/VERDICT.
2. **Agent 4 became a blind black-box attacker** — imports the package, drives
   the public API only; static source audits moved to Agent 5.
3. **Agent 5 became the relentless Token & Context Engineering Controller**
   with the seven-point playbook above.
