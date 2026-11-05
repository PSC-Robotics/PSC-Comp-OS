"""
Drive two DC motors through two-input motor driver boards (IN1 and IN2), such as
the DRV8871, wired to the Arduino.

Wiring (Arduino Uno pin numbers). Use one driver board per motor. IN1 and IN2
must both go to PWM pins: on an Uno these are 3, 5, 6, 9, 10 and 11 (marked ~).

    Left motor driver           Right motor driver
      IN1 -> pin 9                IN1 -> pin 5
      IN2 -> pin 10               IN2 -> pin 6
      GND -> Arduino GND          GND -> Arduino GND

    On each driver board:
      VM / power +  -> motor battery + (6.5V to 45V for a DRV8871, e.g. 12V)
      GND / power - -> motor battery -
      OUT1, OUT2    -> the motor's two wires

The motor battery's ground must be joined to the Arduino's ground (the GND
connections above do this). Never power the motors from the Arduino.

Needs version 6 or later of the PSC
Arduino firmware (download it from the Arduino page of the docs). Copy this file
to the top of your USB stick, named robot.py, and press the start button.
"""
from sr.robot3 import BRAKE, COAST, Robot

robot = Robot()  # Waits here until the start button is pressed

left_motor = robot.arduino.dc_motor(9, 10)  # IN1, IN2
right_motor = robot.arduino.dc_motor(5, 6)  # IN1, IN2


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
