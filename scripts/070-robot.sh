#!/bin/bash
set -eux -o pipefail

# udev rules
mv /tmp/packer/robot/10-srobo.rules /usr/lib/udev/rules.d/
chmod 644 /usr/lib/udev/rules.d/10-srobo.rules
install -o root -g root -m 644 /tmp/packer/robot/90-psc-webcam-power.rules /usr/lib/udev/rules.d/
