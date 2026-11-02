---
name: camlab
description: Use when a user pastes a "CAMLAB/1" string or a camlab hand-off, mentions camlab, Desktop/camlab-handoff, sample.jpg, sheet.jpg, handoff.md or preamble.md, or asks for camera and light settings for a tethered Canon with Godox lights for a Reel, product or bench clip
---

# camlab: answer a CAMLAB/1 string

## Overview
camlab is a Mac app that tethers one Canon body and two Godox lights (key, fill). It hands you a
compact 8-line `CAMLAB/1` string plus a picture; your reply is pasted back and applied through
camlab's guardrails. The photographer is not one by trade: plain sentences, no unexplained jargon.

## 1. Find the picture
- Folder `~/Desktop/camlab-handoff/`: `sample.jpg` (live frame) or `sheet.jpg` (clip: four frames,
  time burned in), `handoff.md` (the string), `preamble.md` (the long explanation, once per session).
- If you can read files but not that folder, ask for access once. If you cannot read files, ask for
  the picture to be attached. Never answer from the numbers alone.

## 2. Read the string
```
CAMLAB/1 round=3 sess=a7f2 pic=attached
clip=reel 9:16 fps=30 lens=35mm(56eq) cam=R50V mode=M
now cam: iso=250 shutter=1/30 aperture=f/5.6 wb=Color_Temperature kelvin=4300 style=Neutral
now lights: key=35@4300 fill=47@4300 (0-100%, 2800-6500K)
want: luma=0.48-0.60 clipH<=2 clipL<=2 K=3800-5200 skin=0.45-0.58 dof=deep
got: luma=0.729 OFF clipH=0.9 clipL=0 K=4450 skin=-
last: round=2 you=key=60@4300 fill=40@4300 result=luma 0.81->0.73
prefs: soft ; Colleen likes soft light
```
luma = brightness of the middle, 0-1; clipH/clipL = % pure white/black; K = colour the camera sees;
skin = skin brightness (`-` = no face found); OFF = outside the target; `byhand=` lights are set by
hand; underscores stand for spaces. `last:` is what your previous reply did; use it.
A clip review starts `CAMLAB/1 clip round=…` and its `clip=` line adds `len=` and the frame times;
`got:` lists one value per frame.

## 3. Ask first only when prefs has no colour choice
If `prefs:` already has `K=…`, warm, neutral or daylight, answer straight away. Otherwise your first
reply is one short question and nothing else: which colour temperature, and (unless prefs says soft
or crisp) how soft. Anchors: warm about 3200 K (tungsten, cosy), neutral about 4000-4500 K, daylight
about 5600 K (cool, clean). 4300 K is warm-neutral, not cool. Never put a reply block in the same
message as the question.

## 4. Reply: a sentence or two on what the picture shows, then exactly this block
```
CAMLAB/1 reply round=3 sess=a7f2
camera: iso=250 shutter=1/60 aperture=f/5.6 wb=Color_Temperature kelvin=4300
lights: key=20@4300 fill=30@4300
place: key=left 40deg eye+1ft 5ft soft:bounce ; fill=right 25deg eye 8ft
next: if luma still >0.6, drop fill to 15
```
- Copy `round=` and `sess=` from the string you are answering; camlab refuses a stale round.
- Only the keys you change; values from preamble.md's choices. Lights percent@kelvin, clamped to range.
- Video shutter near 1/(2 × fps). Lights' kelvin = camera kelvin unless you want a cast.
- `place:` one entry per light, from where the photographer stands BEHIND THE CAMERA: left/right of
  the lens, degrees off axis, height vs the subject's eyes, distance in FEET, softening (bounce,
  diffuser, closer).
- `next:` one thing to change if the next sample is still off.

For a clip review answer with:
```
CAMLAB/1 clipreply round=4 sess=a7f2
verdict: retake reason=face_goes_dark_at_the_end
trim: in=1.2 out=16.8
caption: one line for the post
next: iso=400 lights: key=55@4300
```
verdict is keep, retake or maybe; trim in seconds; `next:` holds the settings for the next take.

## Quick reference
| Situation | Do |
|---|---|
| `mode=` is Scene Intelligent Auto, P, Av, Tv, or ISO Auto | Say first: mode dial to M, ISO off Auto, or the body undoes the lights |
| `byhand=` names a light | Give its value anyway; the photographer sets it by hand |
| `skin=-` on a person clip | Say nobody is in frame; ask them to step in before the next sample |
| Windows clipping on an interior | Lower the key or turn the shot so the window is beside the camera |

## Common mistakes
- A reply block in the same message as the question.
- Wrong or missing `round=` / `sess=`.
- Placement from the subject's point of view; metres instead of feet.
