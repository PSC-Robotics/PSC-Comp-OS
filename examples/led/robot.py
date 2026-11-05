"""
Set the colour of the LED on the brain board.

Copy this file to the top of your USB stick, named robot.py, and press the start
button.
"""
from sr.robot3 import Colour, Robot

robot = Robot()  # Waits here until the start button is pressed

led = robot.brain.led

for colour in (Colour.RED, Colour.YELLOW, Colour.GREEN, Colour.CYAN,
               Colour.BLUE, Colour.MAGENTA, Colour.WHITE):
    led.colour = colour
    robot.sleep(1)

# Any colour: (red, green, blue), each from 0 to 255
led.colour = (255, 80, 0)  # orange
robot.sleep(1)

led.colour = Colour.OFF
# The LED also turns off by itself when your code ends.
