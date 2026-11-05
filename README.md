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

`psc-statusd` shows the robot's state on the first LED of a WS2812 ("NeoPixel")
chain, plays sounds on a buzzer and handles a physical start button. The pins
(BCM numbers), the LED chain and the buzzer type are set in `/etc/psc-status.toml`:

| Signal | BCM | Header pin |
| --- | --- | --- |
| WS2812 LED chain (data in, Brain Board HAT) | 18 | 12 |
| WS2812 LED chain (data in, alternative; off by default) | 10 | 19 |
| Start button | 5 | 29 |
| Buzzer | 13 | 33 |

| State | Status LED |
| --- | --- |
| Booting | Blue, pulsing |
| No code USB plugged in | Pink, flashing |
| Waiting for the start button | Green, pulsing slowly (dim) |
| Waiting for the start button in competition mode | Yellow, pulsing slowly (dim) |
| Competition USB just plugged in, or plugged in with no code USB | Yellow, flashing rapidly |
| Robot code running | Green |
| Robot code finished or stopped | White (dim) |
| Robot code crashed | Red, flashing |

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

Wire the first LED's DIN to GPIO 18 (as on the Brain Board HAT), VDD to 5V and
GND to GND, and its DOUT to the second LED's DIN. Robot code sets the second LED
with `robot.brain.led.colour = Colour.RED`, or `(255, 80, 0)` for any colour, over
MQTT (`psc/leds`), and it turns off when the code stops. GPIO 18 is driven through
the PWM hardware (`rpi_ws281x`; the analogue audio output is turned off, as it also
uses the PWM hardware). The LEDs light once `psc-statusd` starts, 10 to 20 seconds
after power-on.

`psc-statusd` follows Astoria's `astoria/astmetad` topic for competition mode, and
`astoria/astdiskd` for competition USBs: a mounted disk with `astoria.json` and no
`robot.py`, the same rule Astoria uses. It flashes yellow rapidly for 3 seconds when
one is plugged in, and for as long as there's no code USB.

`sr.robot3` reads `/etc/psc-status.toml` to decide which pins robot code can't
use: the start button, the buzzer and the LEDs' data pin, plus 14/15 (serial,
for bus servos), 2/3 (I2C) and 0/1 (HAT EEPROM). Wire the start button between GPIO 5 and
GND; the internal pull-up is enabled. Never connect a GPIO pin to 5V. A press
sends the same start event as the web UI's Start button.

The Student Robotics `kchd` and `boot_40` services are not installed because
they drive the KCH hat's pins, and robot code runs with `SR_KCH_HAT=0` so the
API leaves those pins alone.

```sh
systemctl status psc-statusd
journalctl -u psc-statusd
```

## I2C And Servo Drivers

`robot.brain.servo_driver(sda=17, scl=27)` gives a PCA9685 driver on any two free
GPIO pins, with positions from -1 to 1 as on Student Robotics' servo board.
`robot.brain.i2c_bus(sda=17, scl=27)` gives the bus on those pins, which is the same
bus if a servo driver is already on them: `scan()` lists devices, and
`device(address)` gives `read`, `write`, `send` and `receive`. These buses are driven
in software through RPi.GPIO (`SoftwareI2CBus`), so they need no boot config.

A pin can only belong to one bus or motor. Addresses only have to differ within one
bus, and giving a device a servo driver's address on the same bus raises a
`ValueError`. Problems on the bus raise `I2CError`, with a message saying what to
check. See `examples/servo_driver` and `examples/i2c`.

Hardware I2C is also on (`dtparam=i2c_arm=on`, the `i2c-dev` module and the `robot`
user in the `i2c` group), on GPIO 2 and 3, for devices on the HAT itself. Those pins
have no screw terminals. `robot.brain.i2c_bus(sda=2, scl=3)` gets that bus.

A power board is optional. `robot.power_board` raises an error if there isn't one,
and nothing is logged otherwise.

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
