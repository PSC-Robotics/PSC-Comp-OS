"""
Brain board test: the LEDs, the buzzer, the start button and every GPIO terminal.

Copy this file to the top of a USB stick, named robot.py, and press the start button.

- The second LED fades smoothly through every colour.
- The buzzer plays a tune, over and over.
- Every GPIO screw terminal is driven high (3.3V), for checking with a voltmeter:
  measure from each GPIO terminal to GND (the 5V terminal's GND, J4). They stay high
  while the code runs, paused or not, and go low when it stops.
- The start button pauses and plays. While paused, the status LED (the first LED)
  pulses smoothly and the robot beeps, as it does waiting for the start button; press
  it again to carry on. The web interface's Start button does the same.

The first LED is the status LED: it shows green while this runs, and pulses while
paused. Robot code can't set its colour, as the robot uses it to show its status.

Needs PSC Robotics OS 2026.0.26 or later.
"""
import math
import time

from sr.robot3 import BRAKE, BatteryMonitorError, Note, Robot

# The HAT's GPIO screw terminals, in pairs, as printed on the board (BCM numbers).
TERMINALS = {
    "J7": (26, 25),
    "J3": (23, 24),
    "J9": (16, 12),
    "J10": (17, 27),
    "J11": (22, 6),
}

# Ode to Joy (Beethoven): (note, beats). A note of None is a rest.
BEAT = 0.3  # seconds
TUNE = [
    (Note.E6, 1), (Note.E6, 1), (Note.F6, 1), (Note.G6, 1),
    (Note.G6, 1), (Note.F6, 1), (Note.E6, 1), (Note.D6, 1),
    (Note.C6, 1), (Note.C6, 1), (Note.D6, 1), (Note.E6, 1),
    (Note.E6, 1.5), (Note.D6, 0.5), (Note.D6, 2),
    (None, 2),
]

FADE_SECONDS = 6.0  # once round every colour
LOOP_SECONDS = 0.05


def rainbow(position):
    """A colour from a smooth loop through every colour, position from 0 to 1."""
    return tuple(
        round(255 * (0.5 + 0.5 * math.cos(2 * math.pi * (position - offset))))
        for offset in (0, 1 / 3, 2 / 3)
    )


def start_button_pressed(robot):
    """
    Whether the start button (or the web interface's Start button) has been pressed
    since the last check. The robot only shares presses for wait_start, so this uses
    the library's own record of them.
    """
    return robot._astoria.get_start_button_pressed()


robot = Robot()  # Waits here until the start button is pressed

# Every GPIO terminal high: a DC motor's BRAKE drives both of its pins high.
outputs = []
for name, (pin_a, pin_b) in TERMINALS.items():
    motor = robot.brain.dc_motor(pin_a, pin_b)
    motor.power = BRAKE
    outputs.append(motor)
    print(f"{name}: GPIO {pin_a} and GPIO {pin_b} high (3.3V)")

try:
    print(f"Battery: {robot.brain.battery_sensor.voltage:.2f}V")
except BatteryMonitorError as error:
    print(f"Battery: no reading ({error})")

print("Running: press the start button to pause")
start = time.monotonic()
note_index = 0
next_note = start
while True:
    now = time.monotonic()

    robot.brain.led.colour = rainbow(((now - start) / FADE_SECONDS) % 1)

    if now >= next_note:
        note, beats = TUNE[note_index]
        if note is not None:
            # A little shorter than the beat, so repeated notes sound separate
            robot.buzzer.buzz(note, beats * BEAT * 0.9)
        next_note = now + beats * BEAT
        note_index = (note_index + 1) % len(TUNE)

    if start_button_pressed(robot):
        robot.buzzer.buzz(Note.C6, 0)  # stop the tune's note
        print("Paused: press the start button to carry on. GPIO terminals stay high.")
        paused_at = time.monotonic()
        robot.wait_start()
        # Carry on where it left off: the fade and the tune skip the paused time
        paused_for = time.monotonic() - paused_at
        start += paused_for
        next_note += paused_for
        print("Running: press the start button to pause")

    robot.sleep(LOOP_SECONDS)
