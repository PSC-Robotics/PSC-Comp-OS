"""
Drive two DC motors through two-input motor driver boards (IN1 and IN2), such as
the DRV8871, wired to the brain board.

Wiring (BCM GPIO numbers, header pin in brackets). Use one driver board per motor.

    Left motor driver           Right motor driver
      IN1 -> GPIO 20 (pin 38)     IN1 -> GPIO 19 (pin 35)
      IN2 -> GPIO 21 (pin 40)     IN2 -> GPIO 26 (pin 37)
      GND -> GND     (pin 39)     GND -> GND     (pin 34)

    On each driver board:
      VM / power +  -> motor battery + (6.5V to 45V for a DRV8871, e.g. 12V)
      GND / power - -> motor battery -
      OUT1, OUT2    -> the motor's two wires

The motor battery's ground must be joined to the brain board's ground (the GND
pins above do this through the driver board). Never power the motors from the Pi.

Copy this file to the top of your USB
stick, named robot.py, plug it into the robot and press the start button.
"""
from sr.robot3 import BRAKE, COAST, Robot

robot = Robot()  # Waits here until the start button is pressed

left_motor = robot.brain.dc_motor(20, 21)   # IN1, IN2
right_motor = robot.brain.dc_motor(19, 26)  # IN1, IN2


def drive(left, right, seconds):
    """Set both motors' power (-1 to 1) for a number of seconds."""
    left_motor.power = left
    right_motor.power = right
    robot.sleep(seconds)


print("Forwards at half speed")
drive(0.5, 0.5, 2)

print("Stop")
drive(BRAKE, BRAKE, 1)

print("Backwards at half speed")
drive(-0.5, -0.5, 2)

print("Turn on the spot to the right")
drive(0.5, -0.5, 1)

print("Turn on the spot to the left")
drive(-0.5, 0.5, 1)

print("Full speed forwards")
drive(1, 1, 1)

print("Coast to a stop")
drive(COAST, COAST, 1)

print("Done")
# The motors are also stopped automatically when your code ends.
