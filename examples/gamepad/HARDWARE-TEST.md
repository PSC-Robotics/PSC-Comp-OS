# Gamepad hardware test (PSC Robotics OS 2026.0.16)

Run this on a **Pi 3** and a **Pi 4**. Everything below was tested off-device
(real Linux input layer, a mock BlueZ, the web page in a browser), but radio,
pairing and the mini UART can only be proved on real boards.

Sign in over SSH (`ssh robot@robot.lan`, password `robot`) for the commands.

## 1. Bluetooth comes up (each board, from a cold boot)
- [ ] `hciconfig` shows `hci0` `UP RUNNING` within a minute of boot.
- [ ] `systemctl status psc-gamepadd` is `active (running)`, and
      `journalctl -u psc-gamepadd -b` shows `Bluetooth powered on`.
- [ ] Reboot 5 times: Bluetooth comes up every time. (If it ever doesn't,
      the journal should show `restarting hciuart`; send me the log.)
- [ ] Bus servos still work on GPIO 14/15 at the same time (if you have one).

## 2. Pairing from the web page (each controller you have)
Open http://robot.lan, click the gamepad button, then for each controller:
- [ ] Put it in pairing mode, click Search: it appears within ~10 s.
- [ ] Click Pair: it shows Connected, and appears under Connected gamepads.
- [ ] Moving sticks/triggers and pressing every button lights the right
      thing (a = bottom face button, b = right, x = left, y = top).
- [ ] Turn it off and on: it reconnects by itself (press its home button).
- [ ] Reboot the robot: it reconnects without pairing again.
- [ ] Forget it, then pair it again: works.

Controllers: Xbox Series/One S, DualShock 4, DualSense, Stadia (Bluetooth
mode), plus any USB pad (just plug in).

## 3. Robot code
Put `examples/gamepad/robot.py` on the code USB:
- [ ] With no controller on, the log says `Waiting for a gamepad` every 10 s.
- [ ] Turning a paired controller on starts the program.
- [ ] Driving works; turning the controller off stops the motors; turning it
      back on carries on.

## 4. Competition USB
- [ ] A stick with `autonomous/zone-0/astoria.json`: Bluetooth controllers
      disconnect, the Gamepads window says Bluetooth is switched off, the
      robot's Wi-Fi network disappears (use Ethernet or wait), USB pads still work.
- [ ] Remove the stick: Wi-Fi and Bluetooth come back, controllers reconnect.
- [ ] A `driver-controlled` stick: controllers keep working; `robot.mode` is
      `COMP` and `robot.zone` matches the stick.

If something fails, the most useful thing to send is:
`journalctl -u psc-gamepadd -u bluetooth -u hciuart -b > /tmp/bt.log`
