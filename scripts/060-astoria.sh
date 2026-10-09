#!/bin/bash
set -eux -o pipefail

# Install udiskie and the Wi-Fi access-point support used by the robot image.
apt-get -y install udiskie hostapd

# Configure the built-in hostapd service for the robot's Wi-Fi network.
install -o root -g root -m 600 /tmp/packer/network/hostapd.conf /etc/hostapd/hostapd.conf
if [ -f /etc/default/hostapd ]; then
    sed -i 's|^#?DAEMON_CONF=.*|DAEMON_CONF="/etc/hostapd/hostapd.conf"|' /etc/default/hostapd
fi
systemctl disable hostapd.service
raspi-config nonint do_wifi_country GB

# udisks2
mv /tmp/packer/astoria/udisks/mount_options.conf /etc/udisks2/
chmod 644 /etc/udisks2/mount_options.conf

# polkit
mkdir -p /etc/polkit-1/rules.d/ /etc/polkit-1/localauthority/50-local.d/
mv /tmp/packer/astoria/udiskie/50-udiskie.rules /etc/polkit-1/rules.d/
mv /tmp/packer/astoria/udiskie/10-udiskie.pkla /etc/polkit-1/localauthority/50-local.d/
chmod 644 /etc/polkit-1/rules.d/50-udiskie.rules /etc/polkit-1/localauthority/50-local.d/10-udiskie.pkla

# udiskie config
mv /tmp/packer/astoria/udiskie/astoria-udiskie.yml /etc/
chmod 644 /etc/astoria-udiskie.yml

# astoria configs
mv /tmp/packer/astoria/astoria.toml /etc/
sed -i "s/%SROBO_VERSION%/${SROBO_NAME} ${SROBO_VERSION}/" /etc/astoria.toml
chmod 644 /etc/astoria.toml

mkdir -p /var/srobo/cache/
chown -R robot:robot /var/srobo/cache/

# Writable calibration directory for team-specific USB cameras.
mkdir -p /var/lib/psc-robotics/calibrations
chown -R robot:robot /var/lib/psc-robotics
chmod 755 /var/lib/psc-robotics /var/lib/psc-robotics/calibrations

# Local camera calibration service.
install -o root -g root -m 755 /tmp/packer/calibration/psc-camera-calibration.py \
    /usr/local/lib/psc-camera-calibration.py
install -o root -g root -m 644 /tmp/packer/calibration/psc-camera-calibration.service \
    /usr/lib/systemd/system/psc-camera-calibration.service
systemctl enable psc-camera-calibration.service

# Retry the onboard BCM43430 with alternate packaged firmware on Pi 3 only.
install -o root -g root -m 755 /tmp/packer/network/psc-pi3-wifi-recovery.sh \
    /usr/local/sbin/psc-pi3-wifi-recovery.sh
install -o root -g root -m 644 /tmp/packer/network/psc-pi3-wifi-recovery.service \
    /usr/lib/systemd/system/psc-pi3-wifi-recovery.service
systemctl enable psc-pi3-wifi-recovery.service

# astoria systemd tasks
mv /tmp/packer/astoria/astoria/* /usr/lib/systemd/system/
chmod 644 /usr/lib/systemd/system/ast*d.service /usr/lib/systemd/system/astoria-udiskie.service
systemctl enable astdiskd.service astmetad.service astoria-udiskie.service astprocd.service astwifid.service

# Make pyenv python entrypoint globally available
home="/home/$(id -un 1000)"
if [ -d $home/.pyenv ]; then
    python_bin_path="$(echo $home/.pyenv/versions/*/bin)"
    ln -s $python_bin_path/astdiskd /usr/local/bin/astdiskd
    ln -s $python_bin_path/astmetad /usr/local/bin/astmetad
    ln -s $python_bin_path/astprocd /usr/local/bin/astprocd
    ln -s $python_bin_path/astwifid /usr/local/bin/astwifid
fi
