"""
List the devices on an I2C bus, then read a register from the first one.

Connect the device's SDA to GPIO 17 and SCL to GPIO 27, or any two free GPIO pins
(change them below), and give it power and a shared ground with the brain board.
The GPIO pins are 3.3V only, so check the device doesn't pull SDA or SCL up to 5V.

Copy this file to the top of your USB stick, named robot.py, and press the start
button.
"""
from sr.robot3 import Robot

robot = Robot()  # Waits here until the start button is pressed

i2c = robot.brain.i2c_bus(sda=17, scl=27)

addresses = i2c.scan()
print("Devices found:", [hex(address) for address in addresses])

if addresses:
    device = i2c.device(addresses[0])

    # Read 2 bytes starting at register 0. Your device's datasheet lists its registers.
    data = device.read(0x00, 2)
    print("Registers 0 and 1:", data[0], data[1])

    # Writing looks like this, but check what the register does on your device first:
    # device.write(0x01, [0x10])
