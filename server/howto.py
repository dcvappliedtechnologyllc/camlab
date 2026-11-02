"""Camera how-to help for the Canon EOS R50 V, for settings camlab cannot change over USB.

Paraphrased from Canon's official EOS R50 V Advanced User Guide (firmware 1.2.0 or later):
https://cam.start.canon/C021/  (HTML pages under /en/C021/manual/html/, PDF at /en/C021/manual/c021.pdf).
`verified` is True when the steps come from the R50 V manual itself. Canon's text does not show the
small dial/menu icons, so each entry names the position or menu item by its words.
"""
from __future__ import annotations

_M = "https://cam.start.canon/en/C021/manual/html/"


def _e(id, title, why, steps, page, applies_to, verified=True):
    return {"id": id, "title": title, "why": why, "steps": steps, "manual_url": (_M + page) if page else None,
            "applies_to": applies_to, "verified": verified}


HOWTO: dict[str, dict] = {e["id"]: e for e in [
    _e("dial_manual", "Set the camera to manual movie exposure",
       "So camlab can hold the brightness steady and the lights are not undone by the camera changing its mind.",
       ["Turn the big Mode dial on top of the camera to the Advanced movies position (the one for creative movie modes, not the green Auto one and not the still-photo camera one).",
        "Look at the top-left of the screen: there is a small icon for the shooting mode. Tap it.",
        "In the list that opens, tap Movie manual exposure.",
        "Check the screen now shows shutter speed, aperture and ISO as numbers you can set yourself.",
        "Do not change these three while a clip is recording; that gets recorded as a brightness jump."],
       "UG-02_ShootingMovie_0120.html", ["dial"]),
    _e("iso_fixed", "Turn ISO Auto off and pick a fixed ISO",
       "So the camera stops brightening and darkening the picture by itself while you shoot.",
       ["First set the camera to Movie manual exposure (see the manual exposure help).",
        "Tap the ISO number on the screen.",
        "Turn the dial to choose a number, such as 400 for a lit room. Anything except AUTO is a fixed ISO.",
        "If it still says AUTO, turn the dial again until a number shows; AUTO stays on until you pick a number.",
        "Lower ISO means a cleaner picture; go higher only if the picture is too dark."],
       "UG-04_Shooting_0130.html", ["dial"]),
    _e("movie_switch", "Switch from photo to movie",
       "So the camera records video clips instead of taking still photos when camlab presses record.",
       ["There is no separate photo/movie switch on this camera; the Mode dial on top does it.",
        "Turn the Mode dial away from the still-photo camera position to one of the movie positions (Advanced movies is the one camlab wants).",
        "Press the movie button (the red dot, on the top or front) once to start and once to stop, to test it.",
        "The screen should show a REC mark and a red frame while recording. Stop it again.",
        "After switching, glance at your settings: Canon advises re-checking them when you change between photo and movie."],
       "UG-04_Shooting_0010.html", ["camera"]),
    _e("photo_dial", "Put the camera in photo mode, on M",
       "So the shutter takes a still photo and the look camlab picked (aperture, shutter, ISO) is not undone by the camera.",
       ["Turn the big Mode dial on top of the camera from the movie positions to the still-photo range (the camera icon, not the movie camera).",
        "Within the photo range pick M (manual): tap the shooting-mode icon at the top-left of the screen if the dial does not show the letters.",
        "The screen should show shutter speed, aperture and ISO as numbers; camlab sets them when you pick a look.",
        "If the camera asks about the lens or stabiliser after the switch, accept and carry on.",
        "camlab cannot turn this dial over USB; it only reads which side it is on."],
       "UG-04_Shooting_0010.html", ["photo_dial"]),
    _e("wb_kelvin", "Set white balance to a colour temperature",
       "So colours stay the same from clip to clip instead of drifting when the camera re-guesses the light.",
       ["Open the menu and go to the shooting tab that holds White balance.",
        "Choose White balance, then pick the Color temperature option (the one that shows a K number).",
        "Turn the dial to set the number, from 2500K to 10000K in steps of 100K; press the centre button to confirm.",
        "Rough guide: about 3200K for warm bulbs, about 5200K for daylight and most LED panels set to daylight.",
        "If the picture looks slightly green or pink, use White balance correction right next to it."],
       "UG-04_Shooting_0250.html", ["lights"]),
    _e("lights_on", "Switch the Godox lights on",
       "So the lights are lit and camlab can set their brightness and colour; with them off the picture is only room light.",
       ["Find the power switch on the back of each panel and switch it on.",
        "Wait for the panel to light up (a few seconds).",
        "Close the Godox Light phone app: a light accepts only one Bluetooth connection at a time, so the app blocks camlab.",
        "Give camlab about 10 seconds; it reconnects to each light by itself.",
        "No lights today? Shoot without them and camlab will leave them alone."],
       "", ["lights"], verified=False),
    _e("movie_size_fps", "Choose the movie size and frame rate",
       "So every clip is the same quality, and fast enough for motion without filling the card too quickly.",
       ["Open the menu, go to the movie shooting tab and choose Movie rec. size.",
        "Turn the dial to switch between the tabs, then use the arrow keys to pick the resolution: 3840x2160 is 4K, 1920x1080 is Full HD.",
        "Pick the frame rate. In North America that is 29.97 (a normal look), 59.94 (smoother, good for slow motion later) or 23.98 (film look).",
        "Press the centre button to confirm.",
        "If the frame rates look wrong (for example 25 or 50), the System frequency setting in the setup menu is on PAL; set it to NTSC and pick the size again."],
       "UG-04_Shooting_0040.html", ["camera"]),
    _e("power_saving_off", "Stop the camera going to sleep",
       "So the camera does not switch itself off in the middle of a session and drop the USB connection.",
       ["Open the menu and go to the setup (wrench) tabs.",
        "Choose Power saving.",
        "Set Auto power off to Disable. Screen dimmer and Screen off can stay as they are.",
        "Know that the screen may still go dark after about 30 minutes idle, but the camera itself stays on.",
        "Tap the shutter button or the screen to wake it if it has dimmed."],
       "UG-08_Set-up_0200.html", ["camera"]),
    _e("stabilisation", "Turn on image stabilisation for movies",
       "So hand-held or table-bumped clips look steadier.",
       ["If your lens has an IS switch on its side, slide it to ON. That switch controls the lens stabiliser.",
        "Open the menu and choose IS (Image Stabilizer) mode on the shooting tab.",
        "Set Movie digital IS to On (or Enhanced for strong shake). Expect the picture to zoom in a little.",
        "On a tripod or clamp, set Movie digital IS to Off; Canon recommends that.",
        "Digital IS crops the picture, so check your framing after changing it."],
       "UG-04_Shooting_0410.html", ["camera"]),
    _e("servo_af_faces", "Keep focus following faces",
       "So the camera keeps the face in focus without help while it records.",
       ["Turn the Mode dial to a movie position.",
        "Open the menu, go to the AF tab and set Movie Servo AF to Enable.",
        "Set Subject to detect to People (or Auto), so the camera hunts for faces first.",
        "Set Eye detection to Auto so it focuses on the eyes.",
        "Aim at the person: a frame should appear around the face. Tapping the face on the screen picks who to follow."],
       "UG-05_AF-Drive_0050.html", ["subject"]),
]}

# readiness id -> how-to ids, in the order they should be shown
BY_READINESS: dict[str, list[str]] = {
    "dial": ["dial_manual", "iso_fixed"],
    "camera": ["power_saving_off", "movie_switch"],
    "lights": ["lights_on", "wb_kelvin"],
    "subject": ["servo_af_faces"],
    "photo_dial": ["photo_dial"],
}
