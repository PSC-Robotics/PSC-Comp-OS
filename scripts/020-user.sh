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
# (OpenSSL 3 has no -crypt; -6 is SHA-512 crypt, which Raspberry Pi OS uses.)
printf '%s:%s\n' "$user" "$(openssl passwd -6 robot)" > /boot/firmware/userconf.txt

# Enable ssh
touch /boot/firmware/ssh
