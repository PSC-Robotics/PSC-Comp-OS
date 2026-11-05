PSC Robotics competition USB sticks
====================================

To test your robot as it will run in a match, copy one astoria.json to the top
level of an empty USB stick (FAT32 or exFAT), and plug it into a spare USB port.

  zone-0/astoria.json   Starting in zone 0 (top left)
  zone-1/astoria.json   Starting in zone 1 (bottom right)

The robot's Wi-Fi and Bluetooth are off while the stick is in, and your code stops
180 seconds after the start button is pressed. Unplug the stick to return the robot
to normal. Don't put a robot.py on this stick: use a separate stick for your code.
