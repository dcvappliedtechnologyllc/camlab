# Light bring-up plan (LE300Bi key + LP800Bi fill over Bluetooth Mesh)

Goal: both lights driven by camlab over Bluetooth with the least hands-on time
from the owner. Everything not marked **OWNER** is done by a Claude session.
Target: about 12 minutes of the owner's time in total, in two sittings.

## Phase 0 — before the lights arrive (agents, 0 min of the owner's time) — DONE 2026-10-19/21

1. **Read the command echo.** Every Godox light answers each command on
   opcode 0x0211F1 (`A0 bri K/100 tint ...` for CCT, `AE` for power). Add a
   reply listener in `GodoxMeshClient` (`_handle` for `kind == "network"`,
   decrypt with the app key, match opcode F1 11 02) and expose
   `last_ack` per node. With this, bring-up is pass/fail from the log; nobody
   has to look at the light.
   *Finding:* neither light echoes unprompted. The reply has to be requested
   with `FD 01 FF FF FF FF A0 crc` (binary-person docs/consolidated/protocol.md);
   the radio chip then answers on 0x0211F1 within ~60 ms. `query_status()` does this.
2. **Add `python -m server.lights.godox_mesh bringup --export <file>`.**
   One command that: parses the export and lists nodes; scans and prints
   which nodes advertise our Network ID with RSSI; connects; probes the
   sequence floor; for each node sends power on, 30 % / 5600 K, 100 % / 2800 K,
   100 % / 6500 K, 0 % (power off); waits for each echo; prints a table
   PASS/FAIL per node and step; on any failure dumps the scan and GATT
   services for the gateway. Exit code 0 only when all pass.
3. **Make the Bluetooth permission a one-click step.** Add
   `scripts/bringup.command` (double-clickable; runs step 2 in Terminal.app
   so macOS shows the Bluetooth prompt instead of killing Python with
   signal 134). Also test whether the Claude app's own terminal panel
   (`run_in_terminal`) gets a prompt; if it does, the `.command` file is a
   fallback only.
4. **Camera-based double check (optional, if the 77D is tethered).** A
   `bringup --camera` flag that captures a frame at fill 0 % and fill 100 %
   and asserts the luma median rises; same for key. Catches a light that
   acks but does not change output.
5. Run the full test suite, update README and the hand-over note.

## Phase 1 — unboxing day (OWNER, about 8 minutes) — DONE 2026-10-21 (names left as GD_LED_xxxx in the mesh; the app's display names LE300Bi-xxxx / LP800Bi-xxxx are read from the DB's Node table instead)

1. Power both lights on AC. Set each to Bluetooth mode per its quick-start
   (LE300Bi: MENU → Bluetooth on; LP800Bi: hold the Bluetooth button until it
   flashes if it is not already discoverable). Do not plug in DMX.
2. Godox Light app on the phone: add both lights, name them exactly
   `LE300Bi` and `LP800Bi` (the names go in config). Confirm the app can dim
   each one once. Then force-quit the app (a light accepts one connection).
3. Export: plug the iPhone into the Mac, Finder → the phone → Files tab →
   drag the Godox app's folder to `camlab/`. The file we need is
   `GodoxLight.db` inside it. If the app has a share/export network option,
   that JSON works too.
4. Tell Claude: "lights are in, export is in camlab/<folder>". Done for now.

## Phase 2 — connect and verify (agent, OWNER about 1 minute) — steps 1-2, 4 DONE 2026-10-21: bringup PASS from the Claude terminal panel, no Bluetooth prompt needed; the owner saw the LE300Bi go dark/bright. Found: FE 01 does not switch these lights off, 0 % brightness does; end-byte tenths ignored

1. Agent: `python -m server.lights.godox_mesh export <file>`; confirm both
   nodes, cid 0211, addresses. Set `[lights.key]` and `[lights.fill]` in
   `config.toml` to `backend = "godox_mesh"`, `mesh_export`, `mesh_address`.
2. Agent runs `bringup` from the Claude terminal panel. **OWNER**: when macOS
   asks to allow Bluetooth, click Allow (once). If no prompt appears and the
   process aborts with signal 134, the owner double-clicks
   `scripts/bringup.command` instead; same one click.
3. Agent reads the PASS/FAIL table. All pass → start the server
   (`.venv/bin/python -m server.main -v`), check `/api/state` shows both
   lights `backend: GodoxMeshLight`, `connected: true`, open the UI and move
   both sliders and CCT boxes, confirm echoes in the log. Update README
   "Known gaps" (LP800Bi/LE300Bi now verified), the hand-over note, and the
   memory file.
4. **OWNER** (30 seconds, optional): glance at the lights while the agent
   sweeps 0 → 100 % and 2800 → 6500 K, and say whether warm/cool are the
   right way round. Skip if the camera double check passed.

## Phase 3 — if something fails (agent first, OWNER only if asked)

| Symptom | Agent does | OWNER needed? |
|---|---|---|
| No node advertises our Network ID | Check the phone app is closed; rescan 20 s; try `probe` on anything named GD_LED; compare export netKey with the project that has `deviceNum` > 0 | Maybe: re-export the right project, or power-cycle a light |
| Connects, no Filter Status at any sequence | Keys wrong or export stale: re-parse; try `mesh_src` 0x0600 | Re-export |
| Echo arrives but output does not change | Compare frame bytes with binary-person `docs/firmware/mcu/c1.md` (LE300Bi) / `c8.md` (LP800Bi) and their live records; try `godox_power(True)` before CCT; check light is in Bluetooth mode and not DMX | Tell agent what the panel display shows |
| One light works, the other not | Send to the failing node through the working gateway; check its `unicastAddress`; verify it is in the same project | Power-cycle the failing light |
| Commands stop after a day | Sequence floor moved: delete `<export>.state.json`, rerun `bringup` (re-probes) | No |
| LE300Bi ignores Bluetooth | A DMX cable is plugged in: unplug | Yes, 10 seconds |

Rule: never guess at protocol changes. Capture `scan` + `probe` output and the
decrypted echo bytes, write them to the hand-over note, and compare against
the two reference repos first.

## Phase 4 — after both lights work (agent, 0 min)

1. Camera-in-the-loop: run the acceptance flow (`product_hero` + `part_dark`,
   Optimize) with both lights live and record the result in README.
2. Save a first recipe for each preset with the lights' converged values.
3. Decide whether to keep the DMX code path at all; if the USB-DMX adapter is
   never bought, mark `[dmx]` as unused in `config.toml`.

## Owner's total time

| Phase | Minutes |
|---|---|
| 1 | ~8 (power, pair, export) |
| 2 | ~1 (one Bluetooth click, optional glance) |
| 3 | 0 unless asked |
| 4 | 0 |
