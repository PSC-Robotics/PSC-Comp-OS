#!/bin/bash
set -eux -o pipefail

# PSC status LED and start button service. It replaces the Student Robotics
# kchd and boot_40 units: those drive the SR KCH hat's pins, which on the PSC
# robot are wired to the status LED (BCM 17/22/27) and start button (BCM 5).
install -o root -g root -m 755 /tmp/packer/status/psc-statusd.py /usr/local/lib/psc-statusd.py
install -o root -g root -m 644 /tmp/packer/status/psc-status.toml /etc/psc-status.toml
install -o root -g root -m 644 /tmp/packer/status/psc-statusd.service /usr/lib/systemd/system/psc-statusd.service
systemctl enable psc-statusd.service

# Set the pins up in the firmware so the LED shows blue (booting) from power-on,
# before Linux starts, and the start button input has its pull-up straight away.
cat << 'CONFIG' >> /boot/config.txt

# PSC status LED (blue on while booting), silent buzzer and start button
gpio=17=op,dh
gpio=13,22,27=op,dl
gpio=5=ip,pu
CONFIG
