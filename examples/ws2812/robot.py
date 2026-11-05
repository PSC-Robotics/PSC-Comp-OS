"""
Set the colour of the WS2812 ("NeoPixel") LED chained after the robot's status LED,
the same way as Student Robotics' user LEDs.

Wiring (header pin numbers in brackets):

    First LED (the status LED)    Raspberry Pi
      VDD / 5V                 -> 5V      (pin 2 or 4)
      GND                      -> GND     (pin 6)
      DIN                      -> GPIO 18 (pin 12)

    Second LED (LED_A): DIN -> the first LED's DOUT, and 5V and GND as above.

Needs PSC Robotics OS 2026.0.19 or later. Copy this file to the top of your USB
stick, named robot.py, and press the start button.
"""
from sr.robot3 import LED_A, Colour, Robot

robot = Robot()  # Waits here until the start button is pressed

for colour in (Colour.RED, Colour.YELLOW, Colour.GREEN, Colour.CYAN,
               Colour.BLUE, Colour.MAGENTA, Colour.WHITE):
    robot.raspberry_pi.leds[LED_A].colour = colour
    robot.sleep(1)

# Any colour: (red, green, blue), each from 0 to 255
robot.raspberry_pi.leds[LED_A].colour = (255, 80, 0)  # orange
robot.sleep(1)

robot.raspberry_pi.leds[LED_A].colour = Colour.OFF
# The LED also turns off by itself when your code ends.
