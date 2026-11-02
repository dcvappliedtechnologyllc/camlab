# Shoot mode (guided, non-expert) — design

Date: 2026-10-25. Approved by the owner in chat: Shoot is the default on the Mac and the phone; Studio
(today's screen) is a switch away; Record fires on the first press.

## Goal
Get from "camera on the bracket" to a recorded take without reading anything twice. One tall
viewfinder, one card beside it that walks through the take in plain sentences; every control that
needs photography knowledge is hidden behind "Set it up for me" (AI Auto) or a "why?" link to Studio.

## Shell
- `body.shoot` / `body.studio`; header switch "Shoot | Studio" (localStorage `camlab.mode`, default shoot).
  Shoot hides rails, drawers, the Claude drawer, the HUD chips, the viewer tool buttons and the ARM/REC bar.
- Layout: viewfinder centred (portrait or landscape, same fit rules) with `#guide` (360 px) on the right;
  phone: viewfinder on top, the card below (scrolls). Guides canvas stays (eye line, safe areas).

## The card: six steps, current one expanded, finished ones show their result
1. **What are you making?** Tiles: Colleen Reel (reel + person_portrait), Interior Reel (reel_interior +
   interior_room), Product shot (product_hero + part_dark), Bench how-to (bench_doc + assembly). A tap posts
   context. If the output is portrait: "Turn the camera upright on its bracket; the picture should be
   right-way up" with a small "picture is sideways: turn the other way" button (rotation cw/ccw). Ticks when
   the live frame's aspect matches the output.
2. **Get in position.** "Stand where you'll be. Eyes on the blue line." (person outputs) / "Frame the room,
   straight on" (interior) / "Put the part in the middle" (product, bench). Button "I'm ready".
3. **Set it up for me.** Button runs AI Auto; "Load last good setup" when a recipe matches. Narration = the
   Claude messages of this run, as sentences; cost in small grey text. Ends with "Done: looks good" /
   "Stopped: <what to do by hand>". Button "Skip" for manual.
4. **Check.** Three verdicts from the HUD data: Bright enough (luma in band, no clipping), subject line
   (person: Face sharp and not blown out; interior: Windows not blown out; product/bench: Part keeps detail),
   Colour natural (WB in band). Red lines show one fix: key light − / + (±10 %), "Fix it" (AI Auto step), or
   a sentence. Button "Looks good".
5. **Record.** One large Record button = arm + start in one press; big timer; Stop. After Stop: "Saved clip
   <name>" and "Copy to Mac" (pull). Guardrails unchanged (server still arms; nothing changes while rolling).
6. **Keep this setup.** "Save as <tile name>" (recipe) and "Shoot another" (back to step 5) / "Start over".

Every step has "why?" → switches to Studio with the matching drawer open.

## Not in scope
Server changes (verdicts are the HUD rules, client-side; ruling: keeps the phone and Mac in sync since both
run the same page), new presets, changes to AI Auto.
