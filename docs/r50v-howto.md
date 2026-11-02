# Canon EOS R50 V how-to (for camlab)

Plain-language steps for settings camlab cannot change over USB. Paraphrased from Canon's official EOS R50 V Advanced User Guide (firmware 1.2.0 or later), https://cam.start.canon/C021/ . Canon's text does not show the small dial/menu icons, so positions are named by words. Generated from `server/howto.py` (the source of truth).

Readiness items link here: `dial` -> dial_manual, iso_fixed; `camera` -> power_saving_off, movie_switch; `lights` -> wb_kelvin; `subject` -> servo_af_faces

## Set the camera to manual movie exposure (`dial_manual`)

*Why:* So camlab can hold the brightness steady and the lights are not undone by the camera changing its mind.

1. Turn the big Mode dial on top of the camera to the Advanced movies position (the one for creative movie modes, not the green Auto one and not the still-photo camera one).
2. Look at the top-left of the screen: there is a small icon for the shooting mode. Tap it.
3. In the list that opens, tap Movie manual exposure.
4. Check the screen now shows shutter speed, aperture and ISO as numbers you can set yourself.
5. Do not change these three while a clip is recording; that gets recorded as a brightness jump.

Manual page: https://cam.start.canon/en/C021/manual/html/UG-02_ShootingMovie_0120.html  
Applies to: dial  
Verified against the R50 V manual: yes

## Turn ISO Auto off and pick a fixed ISO (`iso_fixed`)

*Why:* So the camera stops brightening and darkening the picture by itself while you shoot.

1. First set the camera to Movie manual exposure (see the manual exposure help).
2. Tap the ISO number on the screen.
3. Turn the dial to choose a number, such as 400 for a lit room. Anything except AUTO is a fixed ISO.
4. If it still says AUTO, turn the dial again until a number shows; AUTO stays on until you pick a number.
5. Lower ISO means a cleaner picture; go higher only if the picture is too dark.

Manual page: https://cam.start.canon/en/C021/manual/html/UG-04_Shooting_0130.html  
Applies to: dial  
Verified against the R50 V manual: yes

## Switch from photo to movie (`movie_switch`)

*Why:* So the camera records video clips instead of taking still photos when camlab presses record.

1. There is no separate photo/movie switch on this camera; the Mode dial on top does it.
2. Turn the Mode dial away from the still-photo camera position to one of the movie positions (Advanced movies is the one camlab wants).
3. Press the movie button (the red dot, on the top or front) once to start and once to stop, to test it.
4. The screen should show a REC mark and a red frame while recording. Stop it again.
5. After switching, glance at your settings: Canon advises re-checking them when you change between photo and movie.

Manual page: https://cam.start.canon/en/C021/manual/html/UG-04_Shooting_0010.html  
Applies to: camera  
Verified against the R50 V manual: yes

## Set white balance to a colour temperature (`wb_kelvin`)

*Why:* So colours stay the same from clip to clip instead of drifting when the camera re-guesses the light.

1. Open the menu and go to the shooting tab that holds White balance.
2. Choose White balance, then pick the Color temperature option (the one that shows a K number).
3. Turn the dial to set the number, from 2500K to 10000K in steps of 100K; press the centre button to confirm.
4. Rough guide: about 3200K for warm bulbs, about 5200K for daylight and most LED panels set to daylight.
5. If the picture looks slightly green or pink, use White balance correction right next to it.

Manual page: https://cam.start.canon/en/C021/manual/html/UG-04_Shooting_0250.html  
Applies to: lights  
Verified against the R50 V manual: yes

## Choose the movie size and frame rate (`movie_size_fps`)

*Why:* So every clip is the same quality, and fast enough for motion without filling the card too quickly.

1. Open the menu, go to the movie shooting tab and choose Movie rec. size.
2. Turn the dial to switch between the tabs, then use the arrow keys to pick the resolution: 3840x2160 is 4K, 1920x1080 is Full HD.
3. Pick the frame rate. In North America that is 29.97 (a normal look), 59.94 (smoother, good for slow motion later) or 23.98 (film look).
4. Press the centre button to confirm.
5. If the frame rates look wrong (for example 25 or 50), the System frequency setting in the setup menu is on PAL; set it to NTSC and pick the size again.

Manual page: https://cam.start.canon/en/C021/manual/html/UG-04_Shooting_0040.html  
Applies to: camera  
Verified against the R50 V manual: yes

## Stop the camera going to sleep (`power_saving_off`)

*Why:* So the camera does not switch itself off in the middle of a session and drop the USB connection.

1. Open the menu and go to the setup (wrench) tabs.
2. Choose Power saving.
3. Set Auto power off to Disable. Screen dimmer and Screen off can stay as they are.
4. Know that the screen may still go dark after about 30 minutes idle, but the camera itself stays on.
5. Tap the shutter button or the screen to wake it if it has dimmed.

Manual page: https://cam.start.canon/en/C021/manual/html/UG-08_Set-up_0200.html  
Applies to: camera  
Verified against the R50 V manual: yes

## Turn on image stabilisation for movies (`stabilisation`)

*Why:* So hand-held or table-bumped clips look steadier.

1. If your lens has an IS switch on its side, slide it to ON. That switch controls the lens stabiliser.
2. Open the menu and choose IS (Image Stabilizer) mode on the shooting tab.
3. Set Movie digital IS to On (or Enhanced for strong shake). Expect the picture to zoom in a little.
4. On a tripod or clamp, set Movie digital IS to Off; Canon recommends that.
5. Digital IS crops the picture, so check your framing after changing it.

Manual page: https://cam.start.canon/en/C021/manual/html/UG-04_Shooting_0410.html  
Applies to: camera  
Verified against the R50 V manual: yes

## Keep focus following faces (`servo_af_faces`)

*Why:* So the camera keeps the face in focus without help while it records.

1. Turn the Mode dial to a movie position.
2. Open the menu, go to the AF tab and set Movie Servo AF to Enable.
3. Set Subject to detect to People (or Auto), so the camera hunts for faces first.
4. Set Eye detection to Auto so it focuses on the eyes.
5. Aim at the person: a frame should appear around the face. Tapping the face on the screen picks who to follow.

Manual page: https://cam.start.canon/en/C021/manual/html/UG-05_AF-Drive_0050.html  
Applies to: subject  
Verified against the R50 V manual: yes

## Put the camera in photo mode, on M (`photo_dial`)

*Why:* So the shutter takes a still photo and the look camlab picked (aperture, shutter, ISO) is not undone by the camera.

1. Turn the big Mode dial on top of the camera from the movie positions to the still-photo range (the camera icon, not the movie camera).
2. Within the photo range pick M (manual): tap the shooting-mode icon at the top-left of the screen if the dial does not show the letters.
3. The screen should show shutter speed, aperture and ISO as numbers; camlab sets them when you pick a look.
4. If the camera asks about the lens or stabiliser after the switch, accept and carry on.
5. camlab cannot turn this dial over USB; it only reads which side it is on.

Manual page: https://cam.start.canon/en/C021/manual/html/UG-04_Shooting_0010.html  
Applies to: photo_dial  
Verified against the R50 V manual: yes
