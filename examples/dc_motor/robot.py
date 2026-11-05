"""
DC motor test for a DRV8871 motor driver wired to the brain board.

Wiring (BCM GPIO numbers, header pin in brackets):
    Driver IN1  -> GPIO 20 (pin 38)
    Driver IN2  -> GPIO 21 (pin 40)
    Driver GND  -> Pi GND  (pin 39)
    Driver VM / GND power terminals -> motor supply (6.5V to 45V, e.g. 12V)
    Driver OUT1 / OUT2 -> the motor's two wires

Copy this file to the root of the
USB stick as robot.py, plug it into the robot, then press the start button.
"""
from sr.robot3 import BRAKE, COAST, Robot

robot = Robot()  # waits here until the start button is pressed

motor = robot.brain.dc_motor(20, 21)  # IN1, IN2

print("Speeding up forwards")
for step in range(1, 11):
    motor.power = step / 10  # 0.1, 0.2 ... 1.0
    robot.sleep(0.3)

print("Full speed forwards")
robot.sleep(2)

print("Brake")
motor.power = BRAKE
robot.sleep(1)

print("Half speed in reverse")
motor.power = -0.5
robot.sleep(2)

print("Full speed in reverse")
motor.power = -1
robot.sleep(2)

print("Coast to a stop")
motor.power = COAST
robot.sleep(1)

print("Done")
