"""
Move servos on a PCA9685 16-channel servo driver at I2C address 0x40.

Plug the servo into port 0, the port marked 0 on the driver. Set SERVOS below to
use other ports.

A normal servo sweeps to one end, the middle, then the other end. A continuous
rotation servo spins one way, stops, then spins the other way.

Copy this file to the top of your USB stick, named robot.py, and press the start
button. It needs I2C turned on: see the setup steps in the message that came with
this file.
"""
import fcntl
import os
import time

from sr.robot3 import Robot

SERVOS = [0]  # The ports to use, from 0 to 15
ADDRESS = 0x40
MIN_PULSE_US = 1000  # One end; a few servos go further, with 500 to 2500
MAX_PULSE_US = 2000  # The other end

I2C_DEVICE = "/dev/i2c-1"
I2C_SLAVE = 0x0703
MODE1 = 0x00
PRESCALE = 0xFE
CHANNEL_0 = 0x06
OSCILLATOR_HZ = 25_000_000
SERVO_HZ = 50


class ServoDriver:
    def __init__(self, address=ADDRESS):
        self._fd = os.open(I2C_DEVICE, os.O_RDWR)
        fcntl.ioctl(self._fd, I2C_SLAVE, address)
        self._write(MODE1, 0x10)  # Sleep, as the frequency can only be set asleep
        self._write(PRESCALE, round(OSCILLATOR_HZ / (4096 * SERVO_HZ)) - 1)
        self._write(MODE1, 0x20)  # Wake up, with register auto-increment
        time.sleep(0.005)

    def _write(self, register, *values):
        os.write(self._fd, bytes([register, *values]))

    def set_pulse(self, port, pulse_us):
        """Send a pulse of this length, in microseconds, to a port."""
        off = round(pulse_us * 4096 * SERVO_HZ / 1_000_000)
        self._write(CHANNEL_0 + 4 * port, 0, 0, off & 0xFF, off >> 8)

    def set_position(self, port, position):
        """Move a servo: -1 is one end, 0 the middle and 1 the other end."""
        middle = (MIN_PULSE_US + MAX_PULSE_US) / 2
        self.set_pulse(port, middle + position * (MAX_PULSE_US - MIN_PULSE_US) / 2)

    def off(self, port):
        """Stop sending pulses, so the servo goes limp."""
        self._write(CHANNEL_0 + 4 * port, 0, 0, 0, 0x10)


robot = Robot()  # Waits here until the start button is pressed

try:
    driver = ServoDriver()
except FileNotFoundError:
    raise SystemExit(f"{I2C_DEVICE} doesn't exist: I2C isn't turned on yet") from None
except PermissionError:
    raise SystemExit(f"Can't open {I2C_DEVICE}: add the robot user to the i2c group") from None

try:
    for _ in range(3):
        for position in (-1, 0, 1, 0):
            print(f"Position {position}")
            for port in SERVOS:
                driver.set_position(port, position)
            robot.sleep(1)
except OSError as error:
    raise SystemExit(f"No reply from the servo driver at {ADDRESS:#x}: check SDA, SCL, VCC "
                     f"and GND ({error})") from None
finally:
    try:
        for port in SERVOS:
            driver.off(port)
    except OSError:
        pass

print("Done")
