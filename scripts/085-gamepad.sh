#!/bin/bash
set -eux -o pipefail

# Gamepads over Bluetooth (and USB). psc-gamepadd pairs controllers for the web
# interface and keeps Bluetooth working; robot code reads gamepads with
# robot.gamepad. Robot code runs as robot, which is in the input group.
install -o root -g root -m 755 /tmp/packer/gamepad/psc-gamepadd.py /usr/local/lib/psc-gamepadd.py
install -o root -g root -m 644 /tmp/packer/gamepad/psc-gamepadd.service /usr/lib/systemd/system/psc-gamepadd.service
systemctl enable bluetooth.service psc-gamepadd.service
# Gentler Bluetooth settings when the hotspot is on 2.4GHz (see the script)
install -o root -g root -m 755 /tmp/packer/gamepad/psc-bluetooth-radio.py /usr/local/lib/psc-bluetooth-radio.py
install -o root -g root -m 644 /tmp/packer/gamepad/psc-bluetooth-radio.service /usr/lib/systemd/system/psc-bluetooth-radio.service
systemctl enable psc-bluetooth-radio.service

# BlueZ settings for game controllers:
#  - FastConnectable: controllers reconnect quickly when turned on (switched off
#    at boot by psc-bluetooth-radio when the hotspot is on 2.4GHz)
#  - JustWorksRepairing: a controller that lost its pairing can pair again
#    (PlayStation and Xbox controllers forget it when paired to another device)
#  - ReconnectAttempts/Intervals: keep trying to reconnect after a dropout
set_option() {  # file section key value: set it, uncommented, in that section
    python3 - "$@" << 'PY'
import re, sys
path, section, key, value = sys.argv[1:]
text = open(path).read()
text = re.sub(rf"^#?\s*{key}\s*=.*\n", "", text, flags=re.MULTILINE)
header = f"[{section}]\n"
if header not in text:
    text += f"\n{header}"
text = text.replace(header, f"{header}{key} = {value}\n", 1)
open(path, "w").write(text)
PY
}
set_option /etc/bluetooth/main.conf General ControllerMode dual
set_option /etc/bluetooth/main.conf General FastConnectable true
set_option /etc/bluetooth/main.conf General JustWorksRepairing always
set_option /etc/bluetooth/main.conf Policy AutoEnable true
set_option /etc/bluetooth/main.conf Policy ReconnectAttempts 7
set_option /etc/bluetooth/main.conf Policy ReconnectIntervals 1,2,4,8,16,32,64
# LE links (Xbox, Stadia and other BLE controllers) are dropped if no packet gets
# through for this long. The default 420ms is too short next to the robot's Wi-Fi
# hotspot on the same radio; 2s rides out short dropouts but still stops the robot
# soon after a controller really goes (units of 10ms).
set_option /etc/bluetooth/main.conf Controller LEConnectionSupervisionTimeout 200
# Some controllers connect without bonding. psc-gamepadd's agent still only lets
# in controllers that were paired from the web interface.
set_option /etc/bluetooth/input.conf General ClassicBondedOnly false

# Older Xbox One controllers (Bluetooth Classic) disconnect straight away unless
# ERTM is off. Game controllers don't need it.
echo "options bluetooth disable_ertm=Y" > /etc/modprobe.d/psc-bluetooth.conf
chmod 644 /etc/modprobe.d/psc-bluetooth.conf

grep -q "^JustWorksRepairing = always" /etc/bluetooth/main.conf
grep -q "^ClassicBondedOnly = false" /etc/bluetooth/input.conf
/usr/bin/python3 -m py_compile /usr/local/lib/psc-bluetooth-radio.py
