"""
Move a servo on a PCA9685 16-channel servo driver.

Wiring:

    Driver    Brain board
      GND  -> GND
      SDA  -> GPIO 17, or any two free GPIO pins (change them below)
      SCL  -> GPIO 27
      VCC  -> 3.3V, or 5V if SDA and SCL stay at 3.3V with it
      V+   -> 5V for the servos, from the HAT or a separate supply

Plug the servo into the port marked 0 on the driver.

A normal servo sweeps to one end, the middle, then the other end. A continuous
rotation servo spins one way, stops, then spins the other way.

Copy this file to the top of your USB stick, named robot.py, and press the start
button.
"""
from sr.robot3 import Robot

robot = Robot()  # Waits here until the start button is pressed

driver = robot.brain.servo_driver(sda=17, scl=27)
servo = driver.servos[0]

for _ in range(3):
    for position in (-1, 0, 1, 0):
        print(f"Position {position}")
        servo.position = position
        robot.sleep(1)

# Servos also stop by themselves when your code ends.
servo.position = None
print("Done")
