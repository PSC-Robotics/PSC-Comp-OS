# psc-sr-robot3

[![MIT license](https://img.shields.io/badge/license-MIT-brightgreen.svg?style=flat)](https://opensource.org/licenses/MIT)

`psc-sr-robot3` is PSC Robotics' version of [sr-robot3](https://github.com/srobo/sr-robot),
the Student Robotics Python API. It adds PSC's brain board: GPIO motors and steppers,
servo drivers and I2C, the brain board LED and battery sensor, the buzzer and gamepads.
See the [PSC Robotics docs](https://docs.psc-robotics.uk).

Your code imports it as `sr.robot3`, the same as Student Robotics' library, so the two
can't be installed together. The PSC Robotics OS image already has it installed; install
it on a computer to get code completion and type checking while you write robot code.

## Installation

```bash
pip install psc-sr-robot3
```

This also installs OpenCV, for the camera.

Versions are Student Robotics' base version followed by PSC's own release number:
2026.0.3.18 is PSC release 18 of sr-robot3 2026.0.3.

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

### The brain board

Everything on the brain board (the Raspberry Pi and its HAT) is under `robot.brain`.
Pins are BCM GPIO numbers, as printed on the HAT.

```python
from sr.robot3 import Colour, Note, OUTPUT, Robot

robot = Robot()

robot.brain.led.colour = Colour.RED          # or (255, 80, 0) for any colour
robot.brain.led.g = True                     # one channel, as on SR's LEDs (or 0 to 255)
robot.brain.buzzer.buzz(Note.A6, 0.5)        # volume=0 to 1; also robot.buzzer
if robot.brain.start_button.was_pressed():   # or .is_pressed, once the code has started
    ...
print(robot.brain.battery_sensor.voltage)

robot.brain.pins[17].mode = OUTPUT           # INPUT, INPUT_PULLUP or OUTPUT
robot.brain.pins[17].digital_write(True)

motor = robot.brain.dc_motor(in1_pin=23, in2_pin=24)   # e.g. a DRV8871
motor.power = 0.5                                      # -1 to 1, BRAKE or COAST

stepper = robot.brain.stepper(25, 26)        # step, direction
stepper.move(200)                            # also move_to() and position

driver = robot.brain.servo_driver(sda=17, scl=27)      # a PCA9685
driver.servos[0].position = 0.5                        # -1 to 1, or None to go limp

bus = robot.brain.i2c_bus(sda=17, scl=27)
print(bus.scan())
```

The pins the robot uses itself can't be used, and say what to use instead: the start
button (`robot.brain.start_button`), the buzzer (`robot.brain.buzzer`), the LEDs
(`robot.brain.led`), the HAT's EEPROM (0, 1), I2C with the battery sensor (2, 3) and the
serial port for bus servos (14, 15). A pin can only be used for one thing at a time.

### The Arduino

As well as SR's `pins` and `ultrasound_measure()`, an Arduino with PSC's firmware drives
motor drivers the same way as the brain board: `robot.arduino.dc_motor(in1_pin,
in2_pin)` and `robot.arduino.stepper(step_pin, direction_pin)`.

`stepper_move()` (on the brain board and the Arduino) is deprecated: use
`stepper(step_pin, direction_pin).move(steps)`, which also counts the position.

### Bus servos (experimental)

Serial bus servos (Waveshare ST/SC, Feetech STS/SCS) on a bus servo adapter, such as the Waveshare Bus Servo Adapter (A):
`robot.bus_servo_adapter.servos[1].position = 0.5`, from -1 to 1 like the other servos
(or `None` to go limp), or in the servo's own units with `steps`, or in degrees with
`angle`.

### Student Robotics code

`robot.kch.leds[LED_A]` raises an error saying to use `robot.brain.led`, as a PSC
robot has no KCH hat. `robot.power_board`, `robot.motor_board` and `robot.servo_board`
work when those boards are plugged in.

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
