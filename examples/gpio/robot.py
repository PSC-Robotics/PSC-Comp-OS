"""
Set and read the brain board's GPIO pins directly.

Wiring, at the HAT's screw terminals:
    - An LED, with a 330R resistor in series, from GPIO 17 (J10) to GND
    - A push button from GPIO 22 (J11) to GND

The LED flashes, and stays on while the button is held.

Pins are BCM GPIO numbers, as printed on the HAT. The pins the robot uses itself (the
start button, the buzzer and the LEDs) can't be used here: use robot.wait_start(),
robot.buzzer and robot.brain.led for those. GPIO pins are 3.3V: never connect one to 5V
or 12V.

Needs PSC Robotics OS 2026.0.27 or later.
"""
from sr.robot3 import INPUT_PULLUP, OUTPUT, Robot

robot = Robot()

led = robot.brain.pins[17]
led.mode = OUTPUT

button = robot.brain.pins[22]
button.mode = INPUT_PULLUP  # reads True until the button connects it to GND

flash = False
while True:
    held = not button.digital_read()
    flash = not flash
    led.digital_write(held or flash)
    robot.sleep(0.25)
