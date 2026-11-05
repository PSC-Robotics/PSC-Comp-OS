PSC Robotics competition USB sticks
====================================

Copy ONE astoria.json onto an empty USB stick (FAT32 or exFAT), at the top level.
Don't put a robot.py on it: a stick with robot.py is treated as a code USB.

  driver-controlled/zone-N/astoria.json   Gamepads allowed (Bluetooth on)
  autonomous/zone-N/astoria.json          No gamepads (Bluetooth switched off)

Make one stick for each zone (0 to 3). Plug it into a spare USB port on the robot
before the start button is pressed. The robot reads it as soon as it is plugged in.

What the settings do
  mode               "COMP" puts the robot in competition mode (robot.mode).
  zone               The robot's starting zone, 0 to 3 (robot.zone).
  arena              The arena name (robot.arena).
  game_timeout       Seconds after the start button before the robot's code is
                     stopped. Set it to your match length.
  wifi_enabled       false switches the robot's Wi-Fi network off, so nobody can
                     use the web interface over Wi-Fi during the match.
  bluetooth_enabled  false switches Bluetooth off, so Bluetooth gamepads can't
                     be used. USB gamepads still work.

Unplugging the stick returns the robot to normal: Wi-Fi and Bluetooth come back.
