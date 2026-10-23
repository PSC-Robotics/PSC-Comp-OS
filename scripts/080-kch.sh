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

# Bus servos on the Pi's serial port (GPIO 14 TX, GPIO 15 RX). Bus servos run at
# 1Mbaud, which needs the full PL011 UART, so give it to the GPIO pins and move
# Bluetooth (for gamepads) to the mini UART. The mini UART's baud rate follows the
# VPU core clock, so fix that clock or Bluetooth stops working when it changes:
# 250MHz on BCM2837 boards (Pi 3, Zero 2 W) and 500MHz on the Pi 4.
cat << 'CONFIG' >> /boot/config.txt

# PL011 serial port on GPIO 14/15 for bus servos; Bluetooth on the mini UART
[all]
enable_uart=1
dtoverlay=miniuart-bt
[pi3]
core_freq=250
[pi02]
core_freq=250
[pi4]
core_freq=500
core_freq_min=500
[all]
CONFIG
sed -i 's/console=serial0,[0-9]* //' /boot/cmdline.txt
systemctl enable hciuart.service
systemctl mask serial-getty@ttyAMA0.service serial-getty@serial0.service
