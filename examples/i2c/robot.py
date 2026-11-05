"""
List the devices on the I2C bus, then read a register from the first one.

Connect SDA to GPIO 2 (header pin 3), SCL to GPIO 3 (pin 5), and give the device
power and a shared ground with the Pi. Check its datasheet for its voltage: the
Pi's I2C pins are 3.3V only.

Needs PSC Robotics OS 2026.0.20 or later. Copy this file to the top of your USB
stick, named robot.py, and press the start button.
"""
from sr.robot3 import Robot

robot = Robot()  # Waits here until the start button is pressed

i2c = robot.raspberry_pi.i2c

addresses = i2c.scan()
print("Devices found:", [hex(address) for address in addresses])

if addresses:
    device = i2c.device(addresses[0])

    # Read 2 bytes starting at register 0. Your device's datasheet lists its registers.
    data = device.read(0x00, 2)
    print("Registers 0 and 1:", data[0], data[1])

    # Writing looks like this, but check what the register does on your device first:
    # device.write(0x01, [0x10])
