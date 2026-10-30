# PSC Robotics OS

PSC Robotics OS is a Raspberry Pi image for PSC Robotics. It is based on the
Student Robotics image build system, with PSC branding, Astoria-compatible
services, Wi-Fi networking, a web UI, and a local USB-camera calibration tool.

The image boots and provides its web and calibration services without Student
Robotics kit boards. SR-specific USB rules and the optional `SR_COMP_USB`
read-only mount are compatibility features, not boot requirements. The robot
user is also configured for generic USB serial, video, and GPIO access. PSC
competition hardware can use the GPIO assignments in `files/astoria/astoria.toml`.
The same arm64 image supports Raspberry Pi 3 Model B and Pi 4. On Pi 3 Model B,
a boot-time fallback retries the onboard BCM43430 Wi-Fi driver with the
alternate firmware shipped in Raspberry Pi OS if the normal driver fails to
create `wlan0`. This is a recovery attempt; the AP still requires valid USB
Wi-Fi metadata and a working radio. Pi 3 B+ and Pi 4 retain their own normal
firmware paths.

## Web Interface

The kit UI is served by nginx on port 80. On a device connected to the robot's
network, open `http://robot.lan/`. If local DNS is unavailable, use the
Raspberry Pi's IP address.

### Camera calibration

Camera calibration is part of the main page: select the calibration button
(the focus icon) in the toolbar. `http://robot.lan/calibrate` opens the same
dialog. When a connected camera isn't calibrated, a banner appears with a
**Calibrate now** button, and the toolbar button shows a dot. Detection reads
sysfs only, so it never opens a camera that robot code is using.

The dialog guides you through four steps:

1. **Choose camera.** Each camera shows its name in robot code
	(`robot.cameras["..."]`) and whether it is calibrated, uses a built-in
	calibration, or can't be used (no USB ID to match a calibration to).
2. **Print the board.** Download the A4 PDF, print it at actual size (100%,
	"fit to page" off), check a black square measures 40 mm, and keep it flat.
3. **Capture views.** A live preview, a suggested next pose, a map of where
	the board has been, and a thumbnail of each view (remove any bad ones).
	At least 10 views are needed; up to 15 improves accuracy. Each view needs at
	least 6 clearly detected tags. Space captures a view. Capturing is disabled
	while robot code is running, as it may be using the camera.
4. **Save.** Calibrations with an average error above 2 px are rejected and not
	saved; the worst views are highlighted so they can be removed and replaced.

Saved calibrations are also written to any robot code USB drive that is
plugged in, in `camera-calibrations/`, with a `camera-calibrations.toml`
manifest. Every few seconds the calibration service checks plugged-in USB
drives for calibrations matching the connected cameras, and imports any the
robot doesn't have (a replaced calibration is kept in `replaced/`).

Calibrations are saved in `/var/lib/psc-robotics/calibrations`, one per camera
model (USB ID), so identical cameras share one. Robot programs find them through
`OPENCV_CALIBRATIONS` and use them the next time they start. Saved calibrations
can be downloaded from the dialog.

The Logitech calibrations bundled with `april_vision` appear as built-in,
read-only cameras in the calibration dialog. To provide team calibrations on a code USB,
place XML files in a directory such as `camera-calibrations/` and add a root
manifest named `camera-calibrations.toml` containing
`calibrations = ["camera-calibrations"]`, or JSON containing
`{"calibrations": ["camera-calibrations"]}`. Those cameras become available
only when a matching calibration is loaded.

Useful service diagnostics:

```sh
systemctl status nginx psc-camera-calibration
journalctl -u nginx -u psc-camera-calibration
```

## Status LED, Buzzer And Start Button

`psc-statusd` shows the robot's state on an RGB LED, plays sounds on a buzzer
and handles a physical start button. The pins (BCM numbers), LED polarity and
buzzer type are set in `/etc/psc-status.toml`:

| Signal | BCM | Header pin |
| --- | --- | --- |
| LED blue | 17 | 11 |
| LED green | 27 | 13 |
| LED red | 22 | 15 |
| Start button | 5 | 29 |
| Buzzer (passive) | 13 | 33 |
| WS2812 LED chain (data in) | 10 | 19 |

| LED | Meaning |
| --- | --- |
| Blue, flashing | Booting (solid blue from power-on until the service starts) |
| Green | Ready |
| Green, flashing | Robot code is waiting for the start button |
| Blue | Robot code running |
| Red | Robot code crashed |

| Sound | Meaning |
| --- | --- |
| One beep (A6, as SR uses) | Robot code is waiting for the start button |
| Three rising notes | Start was pressed |
| Fast short beeps | Robot code crashed |

The sounds (frequency, length and volume of each note) and the default volume
for robot code are set under `[buzzer]` in `/etc/psc-status.toml`; restart
`psc-statusd` after editing. Volume is the PWM duty cycle, 0 to 50.

Robot code plays tones with `robot.buzzer.buzz()`, which asks `psc-statusd` to
play them over MQTT (`psc/buzzer`), so only one process drives the buzzer pin.
The buzzer is silenced whenever robot code is not running, so a tone left
playing stops when the code finishes, crashes or is killed.

A chain of WS2812 ("NeoPixel") LEDs can be used as well as, or instead of, the
RGB LED. Wire the first LED's DIN to GPIO 10, VDD to 5V and GND to GND, and each
LED's DOUT to the next one's DIN. The first LED shows the same status; robot
code sets the rest with `robot.leds[0] = (255, 0, 0)`, over MQTT (`psc/leds`),
and they turn off when the code stops. The chain is driven from the SPI port
(`dtparam=spi=on`), which also takes GPIO 7, 8, 9 and 11, so robot code can't use
those pins. Set the number of LEDs under `[led_strip]` in `/etc/psc-status.toml`
(default 2). Unlike the RGB LED, the chain lights only once `psc-statusd` starts,
not from power-on. The Pi's 3.3V data usually works; if colours flicker, add a
74AHCT125 level shifter on DIN.

The LED is common-cathode (`active_high = true`). Wire the start button between
GPIO 5 and GND; the internal pull-up is enabled. Never connect a GPIO pin to
5V. A press sends the same start event as the web UI's Start button. If the
pins change, also update the `gpio=` lines in `/boot/config.txt`.

The Student Robotics `kchd` and `boot_40` services are not installed because
they drive the KCH hat's pins, and robot code runs with `SR_KCH_HAT=0` so the
API's `robot.kch` LEDs leave these pins alone.

```sh
systemctl status psc-statusd
journalctl -u psc-statusd
```

## Networking And USB

The image retains the configured Wi-Fi bridge/hotspot and Astoria Wi-Fi service;
these are independent of SR board hardware. USB storage is optional. Devices
labelled `SR_COMP_USB` are mounted read-only for compatibility, while ordinary
USB webcams and serial devices remain usable without that drive.

## Build

Requirements: Docker Desktop or Docker Engine, Git, and this repository cloned.
The build runs Packer from `mkaczanowski/packer-builder-arm` inside Docker and
downloads the Raspberry Pi base image and current kit-ui release.

```sh
./build-image.sh nocompress
```

Omit `nocompress` to produce an `.img.xz` distribution image. The output name
is `PSC Robotics OS-image-<git-description>.img[.xz]`; it includes the nearest
Git tag (or commit hash). Working-tree changes add `-dirty` to the description.
Existing image files are not removed by the build script. Keep an eye on free
disk space because the uncompressed image is several gigabytes.

The Packer provisioners are ordered in `pi.json`; OS customization is in
`scripts/` and `files/`. Camera service setup is installed by
`scripts/060-astoria.sh`, and nginx/web UI setup is in `scripts/090-webui.sh`.
