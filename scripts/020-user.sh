#!/bin/bash
set -eux -o pipefail

user="robot"
userdel -r pi || true

# Create robot user with the canonical password expected by the kit image.
useradd \
    --create-home \
    -s /bin/bash \
    -u 1000 \
    -G sudo,video,dialout,adm,gpio,input \
    $user

# Set the known default credential explicitly so Raspberry Pi OS accepts it.
echo "$user:robot" | chpasswd

mv /etc/sudoers.d/010_pi-nopasswd /etc/sudoers.d/010_robot-nopasswd
sed -i 's/pi/robot/g' /etc/sudoers.d/010_robot-nopasswd

# Raspberry Pi OS reads this file on first boot to set the initial user.
printf '%s:%s\n' "$user" "$(openssl passwd -crypt -salt robot robot)" > /boot/userconf

# Enable ssh
touch /boot/ssh
