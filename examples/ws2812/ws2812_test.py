#!/usr/bin/env python3
"""
On PSC Robotics OS 2026.0.18 or later, don't use this: the robot drives the LEDs
itself (on GPIO 18 and GPIO 10), so use robot.leds in your robot code instead
(see robot.py in this folder).

Test a chain of WS2812 / WS2812B ("NeoPixel") LEDs on a Raspberry Pi running
PSC Robotics OS 2026.0.17. Needs nothing installed: it drives the LEDs through
the Pi's SPI port using only the Python standard library.

Wiring (one LED, or a daisy chain: each LED's DOUT goes to the next one's DIN):

    First LED      Raspberry Pi
      VDD / 5V  -> 5V      (header pin 2 or 4)
      GND       -> GND     (header pin 6)
      DIN       -> GPIO 10 (header pin 19, SPI MOSI)

For more than a few LEDs, power them from their own 5V supply and join its
ground to the Pi's. The Pi sends 3.3V data, which most WS2812s accept; if the
colours flicker or are wrong, add a 74AHCT125 level shifter on DIN.

Run it over SSH on the robot (sudo is needed for /dev/spidev0.0):

    sudo python3 ws2812_test.py --enable-spi    # first time only, then reboot
    sudo python3 ws2812_test.py                 # test 2 LEDs
    sudo python3 ws2812_test.py --count 8       # test a chain of 8

LED 1 plays the status LED's colours (blue booting, green ready, flashing red
error); the other LEDs show a rainbow. Press Ctrl+C to stop; the LEDs go off.
"""
import argparse
import colorsys
import fcntl
import os
import struct
import sys
import time

SPI_DEVICE = "/dev/spidev0.0"
CONFIG_FILES = ("/boot/config.txt", "/boot/firmware/config.txt")
SPI_IOC_WR_MODE = 0x40016B01
SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04
# Each WS2812 bit is sent as 4 SPI bits at about 3.2MHz (about 310ns each):
# a 0 is high for one SPI bit then low for three, a 1 is high for three then low.
SPI_HZ = 3_200_000
ZERO, ONE = 0b1000, 0b1110
# Low bytes before and after the data: the LEDs latch the colours after 280us low.
# This also keeps every transfer long enough for the SPI driver to use DMA, which
# sends it without gaps.
RESET_BYTES = 128
SPIDEV_BUFFER = 4096  # the spidev driver's default largest transfer
BYTES_PER_LED = 12


def encode_byte(value: int) -> bytes:
    bits = 0
    for i in range(7, -1, -1):
        bits = (bits << 4) | (ONE if value >> i & 1 else ZERO)
    return bits.to_bytes(4, "big")


ENCODED = [encode_byte(v) for v in range(256)]


class WS2812:
    """A chain of WS2812 LEDs on SPI0 MOSI (GPIO 10)."""

    def __init__(self, count: int, brightness: float = 0.3) -> None:
        if count * BYTES_PER_LED + 2 * RESET_BYTES > SPIDEV_BUFFER:
            sys.exit(f"At most {(SPIDEV_BUFFER - 2 * RESET_BYTES) // BYTES_PER_LED} LEDs")
        self.count = count
        self.brightness = brightness
        self.pixels = [(0, 0, 0)] * count
        self._fd = os.open(SPI_DEVICE, os.O_WRONLY)
        fcntl.ioctl(self._fd, SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(self._fd, SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
        fcntl.ioctl(self._fd, SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", SPI_HZ))

    def show(self) -> None:
        data = bytearray(RESET_BYTES)
        for red, green, blue in self.pixels:
            for value in (green, red, blue):  # WS2812s take green first
                data += ENCODED[round(max(0, min(255, value)) * self.brightness)]
        data += bytes(RESET_BYTES)
        os.write(self._fd, data)

    def off(self) -> None:
        self.pixels = [(0, 0, 0)] * self.count
        self.show()

    def close(self) -> None:
        self.off()
        os.close(self._fd)


def enable_spi() -> None:
    config = next((path for path in CONFIG_FILES if os.path.exists(path)), None)
    if config is None:
        sys.exit("Couldn't find config.txt")
    with open(config) as file:
        text = file.read()
    if "\ndtparam=spi=on" in "\n" + text:
        print(f"SPI is already on in {config}.")
    else:
        with open(config, "a") as file:
            # [all], so it applies whatever board section the file ends in.
            file.write("\n# WS2812 LEDs on GPIO 10 (SPI MOSI)\n[all]\ndtparam=spi=on\n")
        print(f"Turned SPI on in {config}.")
    if not os.path.exists(SPI_DEVICE):
        print("Reboot the robot (sudo reboot), then run this script again without --enable-spi.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Test WS2812 LEDs on GPIO 10.")
    parser.add_argument("--count", type=int, default=2, help="number of LEDs in the chain (default 2)")
    parser.add_argument("--brightness", type=float, default=0.3, help="0 to 1 (default 0.3)")
    parser.add_argument("--enable-spi", action="store_true", help="turn the SPI port on (then reboot)")
    args = parser.parse_args()

    if args.enable_spi:
        enable_spi()
        return
    if not os.path.exists(SPI_DEVICE):
        sys.exit(f"{SPI_DEVICE} doesn't exist: run with --enable-spi, then reboot.")
    if not os.access(SPI_DEVICE, os.W_OK):
        sys.exit(f"Can't write to {SPI_DEVICE}: run with sudo.")

    leds = WS2812(args.count, args.brightness)
    try:
        print("All LEDs red, green, blue, white")
        for colour in ((255, 0, 0), (0, 255, 0), (0, 0, 255), (255, 255, 255)):
            leds.pixels = [colour] * leds.count
            leds.show()
            time.sleep(1)

        print("LED 1: status colours. Other LEDs: rainbow. Ctrl+C to stop.")
        status = [("blue (booting)", (0, 0, 255), False), ("green (ready)", (0, 255, 0), False),
                  ("flashing red (error)", (255, 0, 0), True)]
        start = time.monotonic()
        last_name = None
        while True:
            elapsed = time.monotonic() - start
            name, colour, flashing = status[int(elapsed / 3) % len(status)]
            if flashing and int(elapsed * 2) % 2:
                colour = (0, 0, 0)
            pixels = [colour]
            for i in range(1, leds.count):
                hue = (elapsed / 4 + i / max(1, leds.count - 1)) % 1
                pixels.append(tuple(round(c * 255) for c in colorsys.hsv_to_rgb(hue, 1, 1)))
            if name != last_name:
                print(f"  LED 1 {name}")
                last_name = name
            leds.pixels = pixels
            leds.show()
            time.sleep(0.02)
    except KeyboardInterrupt:
        pass
    finally:
        leds.close()
        print("LEDs off")


if __name__ == "__main__":
    main()
