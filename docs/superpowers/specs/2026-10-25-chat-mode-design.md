# Chat mode (hand-off to a chat agent, no API) — design

Date: 2026-10-25. Requested by the owner: a third mode where the app writes a hand-off file, he pastes it into
a chat (Claude.ai or any agent, no API key), the chat answers with a config string he pastes back, and the
chat also tells him where to put the lights from where he stands behind the camera.

## Flow (the card, three steps)
1. **Take a sample.** Captures a frame, saves `<home>/handoff/<stamp>/sample.jpg` (the oriented frame) and
   `<home>/handoff/<stamp>/handoff.md`, and shows the text with **Copy** and **Show in Finder**. The text has:
   who the agent is talking to (a non-photographer), what to attach (the sample path), the setup (output
   look, scene, lens and focal length, orientation, camera choices and current settings, lights with ranges
   and current values), the targets with plain definitions, the measurements of the sample, and the exact
   reply format. Positions are asked for "from where the photographer stands behind the camera": left/right,
   angle off the lens axis in degrees, height relative to the subject's eyes, distance in feet.
2. **Paste the reply.** A text box and **Apply**. The server parses the fenced block:
   ```camlab
   camera: iso=800 shutter=1/60 aperture=f/2.8 wb_mode=kelvin kelvin=4300
   lights: key=70@4300 fill=30@4300
   ```
   (a JSON object with `camera` and `lights` is accepted too), applies camera then lights through the
   Controller (source "chat", every guardrail kept, nothing while recording), and reports what landed and what
   the body snapped. Text outside the block is kept as advice.
3. **Place the lights.** Shows the advice (the agent's placement sentences) and "Take another sample".

## Server
`server/handoff.py`: `build_handoff(ctl) -> {dir, md_path, jpg_path, text}`; `parse_reply(text) -> {camera,
lights, advice}`; `apply_reply(ctl, text) -> {applied_camera, applied_lights, advice, errors}`. API:
`POST /api/handoff` (build), `POST /api/handoff/apply {text}`, `POST /api/handoff/reveal {path}` (Finder).
State: `claude.mode = "chat"` is not needed; the card keeps its own state in the page.

## Not in scope
Reading the chat's reply automatically; any API use; the phone (the card works there but Copy needs a
secure context, so the text is shown for manual selection).
