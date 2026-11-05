# sr-robot3

[![Lint & build](https://github.com/srobo/sr-robot/actions/workflows/test_build.yml/badge.svg)](https://github.com/srobo/sr-robot/actions/workflows/test_build.yml)
[![PyPI version](https://badge.fury.io/py/sr-robot3.svg)](https://badge.fury.io/py/sr-robot3)
[![Documentation Status](https://readthedocs.org/projects/pip/badge/?version=stable)](http://pip.pypa.io/en/stable/?badge=stable)
[![MIT license](https://img.shields.io/badge/license-MIT-brightgreen.svg?style=flat)](https://opensource.org/licenses/MIT)
![Bees](https://img.shields.io/badge/bees-110%25-yellow.svg)

`sr-robot3` - Python 3 API for Student Robotics Kit

This is the API for Student Robotics, library for writing Robotics APIs.
It will first be deployed at Student Robotics 2024.

## Installation

If you wish to install openCV from your package manager, you can install the base package with:

```bash
pip install sr-robot3
```

To install the full package, including openCV, you can install with:

```bash
pip install sr-robot3[vision]
```

## Usage

The main entry point for the API is the `Robot` class.
Instantiating this class will automatically detect and connect to any supported SR v4 boards connected to the device.
All SR boards are optional; connected boards are initialized and exposed as usual. By default, the `Robot` class waits for a start signal before continuing. When a power board is present, its start button is used; without one, the start signal can come from Astoria.

```python

from sr.robot3 import Robot

r = Robot()

```

To disable the waiting for the start button, you can pass `wait_for_start=False` to the constructor.
The `wait_for_start` method needs to be called before the metadata is available.

```python

from sr.robot3 import Robot

r = Robot(wait_for_start=False)

# Setup in here

r.wait_start()

```

An Arduino running firmware version 4 or later can drive a step/direction
stepper driver with `arduino.stepper_move(step_pin, direction_pin, steps)`. The
call blocks until the requested pulses are emitted; pass `direction=False` to
drive DIR low, and use `step_delay_us` to set the low interval between pulses.
Moves are limited to 350 ms per command so they fit within the serial timeout.
This API does not provide acceleration or track motor position.

### Buzzer

`robot.buzzer.buzz(frequency, duration, blocking=False)` plays a tone on the
buzzer connected to the brain board, like the Student Robotics power board's
`piezo.buzz()`. `Note` values such as `Note.A6` can be used for the frequency.
A new tone replaces the one playing. Tones are played by the robot's status
service, which also beeps when code is waiting for the start button, when start
is pressed, and with fast short beeps when code crashes.

### Steppers on the brain board's GPIO pins

`robot.brain.stepper_move(step_pin, direction_pin, steps)` drives a
step/direction stepper driver wired to the Pi's GPIO header, using BCM GPIO
numbers. It takes the same arguments as `arduino.stepper_move`, but has no
350 ms limit: `steps` can be up to 1000000 and `step_delay_us` from 50 to
1000000. Timing is done in software, so step timing can vary slightly. GPIO 5,
13 and 18 are used by the robot and can't be used.

### Multiple cameras

Every connected camera with a calibration is available in `robot.cameras`.
`robot.camera` still works when exactly one camera is connected.

```python
from sr.robot3 import Robot

r = Robot()

for camera_id, camera in r.cameras.items():
    print(camera_id, camera.see())

front = r.cameras["A1B2C3D4"]
```

Cameras are keyed by their USB serial number. Cameras with no serial number,
a generic placeholder serial or a serial shared with another connected camera
are keyed by the USB port they are plugged into, such as `usb-1-1.2`. Keep
those cameras in the same ports so their names stay the same. The camera
calibration dialog in the robot web UI shows the name of each connected camera.
Cameras of the same model share one calibration.

### Camera calibrations

The bundled Logitech calibrations are used automatically and are read-only in
the camera calibration dialog. Custom calibration directories can be added to the code
USB root with either `camera-calibrations.toml`:

```toml
calibrations = ["camera-calibrations"]
```

or `camera-calibrations.json`:

```json
{"calibrations": ["camera-calibrations"]}
```

Place the AprilVision calibration XML files in the listed directory. Paths are
relative to the manifest and must remain on the code USB. Only cameras matching
a loaded calibration are added to `Robot.cameras`.

## Developer Notes

There are a number of considerations that have been made in the design of this API.
Some of these may not be immediately obvious, so they are documented below.

- The API is designed to raise exceptions for incorrect actions, such as trying to modify the output dictionary or assign a value directly to the motor object.
- `MappingProxyType` is used to prevent the user from adding, removing or overwriting keys in any parts of the API that return a dictionary.
- `tuple` is used to prevent the user from adding, removing or overwriting items in any parts of the API that would return a list.
- `__slots__` is used to prevent the user from adding, removing or overwriting attributes in any parts of the API.
- `sr.robot3.serial_wrapper.SerialWrapper` handles automatic reconnection to the serial port if the connection is lost and impleents 3 retries on any serial operation before raising a `BoardDisconnectionError`.
