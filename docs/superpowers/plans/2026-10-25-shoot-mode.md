# Shoot mode Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans. Steps use checkbox syntax.

**Goal:** A guided default mode in `ui/index.html` that walks a non-photographer from tile to recorded take.
**Architecture:** Same page, `body.shoot` class, a `#guide` card rendered from existing state (`S.state`,
`S.hud`, `S.recipes`) by a small step engine (`SHOOT.step`, persisted per browser). No server changes.
**Tech:** vanilla HTML/CSS/JS. **Spec:** docs/superpowers/specs/2026-10-25-shoot-mode-design.md

## Global constraints
Not a git repo; the app serves `ui/index.html` from its bundle (copy + re-sign + relaunch to ship);
brand rules unchanged; keyboard: space = Record/Stop in Shoot mode, Esc = back a step.

## Review focus
1. Record on the first press must still go through the server's ARM (arm then start, two calls).
2. Step 3 must not start AI Auto while recording (the server refuses; the UI disables the button).
3. The tile tap must never leave the page in a half state if the context post fails (toast, stay on step 1).
4. Switching to Studio and back keeps the step.
5. Phone width: the card scrolls under the viewfinder; Record stays reachable.

### Task 1: mode switch + Shoot layout (CSS, header switch, persistence) — verify in the pane.
### Task 2: step engine + steps 1-2 (tiles, orientation hint, position) — verify tile sets context and ticks.
### Task 3: steps 3-4 (AI Auto narration, verdicts with fixes) — verify on mock person scene.
### Task 4: steps 5-6 (Record/Stop/pull, save recipe, loop) — verify a mock take end to end.
### Task 5: copy into the bundle, re-sign, relaunch on the camera; notes.
