"""
Drive the robot with a gamepad (Bluetooth or USB).

Needs PSC Robotics OS 2026.0.16 or later. Pair a Bluetooth controller from the
Gamepads button in the web interface, or plug one in with a USB cable.

Wiring: two DRV8871 DC motor drivers on the Raspberry Pi (BCM GPIO numbers)
    Left motor:  IN1 -> GPIO 20 (pin 38), IN2 -> GPIO 21 (pin 40)
    Right motor: IN1 -> GPIO 19 (pin 35), IN2 -> GPIO 26 (pin 37)

Controls
    Left stick up/down       drive forwards and backwards
    Right stick left/right   turn
    Right trigger            hold for full speed (otherwise half speed)
    A                        beep
    B                        stop until the stick is moved again

If the controller disconnects, its sticks read 0, so the robot stops. It
carries on when the controller reconnects.
"""
from sr.robot3 import BRAKE, Robot

robot = Robot()
left_motor = robot.raspberry_pi.dc_motor(20, 21)
right_motor = robot.raspberry_pi.dc_motor(19, 26)

robot.wait_for_gamepad()  # Waits here until a controller connects
gamepad = robot.gamepad
print(f"Driving with {gamepad.name}")

was_connected = True
while True:
    if gamepad.connected != was_connected:
        was_connected = gamepad.connected
        print("Gamepad reconnected" if was_connected else "Gamepad disconnected: stopping")

    speed = 1.0 if gamepad.right_trigger > 0.5 else 0.5
    forward = gamepad.left_y * speed
    turn = gamepad.right_x * speed

    # Mix forward and turn, keeping each motor's power between -1 and 1
    left_power = max(-1.0, min(1.0, forward + turn))
    right_power = max(-1.0, min(1.0, forward - turn))

    if gamepad.is_pressed("b"):
        left_motor.power = BRAKE
        right_motor.power = BRAKE
    else:
        left_motor.power = left_power
        right_motor.power = right_power

    if gamepad.was_pressed("a"):
        robot.buzzer.buzz(880, 0.1)

    robot.sleep(0.02)
