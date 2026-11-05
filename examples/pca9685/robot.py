"""
Move a servo on a PCA9685 16-channel servo driver, at I2C address 0x40.

Wiring (header pin numbers in brackets):

    Driver    Raspberry Pi
      GND  -> GND     (pin 6)
      VCC  -> 3.3V    (pin 1)
      SDA  -> GPIO 2  (pin 3)
      SCL  -> GPIO 3  (pin 5)
      V+   -> 5V for the servos, from the HAT or a separate supply

Plug the servo into the port marked 0 on the driver.

A normal servo sweeps to one end, the middle, then the other end. A continuous
rotation servo spins one way, stops, then spins the other way.

Needs PSC Robotics OS 2026.0.20 or later. Copy this file to the top of your USB
stick, named robot.py, and press the start button.
"""
from sr.robot3 import Robot

robot = Robot()  # Waits here until the start button is pressed

servo = robot.raspberry_pi.servos[0]

for _ in range(3):
    for position in (-1, 0, 1, 0):
        print(f"Position {position}")
        servo.position = position
        robot.sleep(1)

# Servos also stop by themselves when your code ends.
servo.position = None
print("Done")
