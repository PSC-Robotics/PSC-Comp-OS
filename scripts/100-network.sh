#!/bin/bash
set -eux -o pipefail

# Hostname
sed -i "s/raspberrypi/robot/" /etc/hosts
sed -i "s/raspberrypi/robot/" /etc/hostname

# dnsmasq
apt-get -y update
apt-get -y install dnsmasq

mv /tmp/packer/network/dnsmasq.conf /etc/dnsmasq.d/srobo.conf
chmod 644 /etc/dnsmasq.d/srobo.conf

# systemd network
mv /tmp/packer/network/br0.netdev /etc/systemd/network/
chmod 644 /etc/systemd/network/br0.netdev

# Bridge address (systemd-networkd), and keep NetworkManager off the hotspot.
mv /tmp/packer/network/br0.network /etc/systemd/network/
chmod 644 /etc/systemd/network/br0.network
install -o root -g root -m 644 /tmp/packer/network/nm-psc-unmanaged.conf \
    /etc/NetworkManager/conf.d/psc-unmanaged.conf
systemctl enable systemd-networkd.service
# networkd only runs br0 (not needed for being online): don't hold up boot waiting.
systemctl mask systemd-networkd-wait-online.service


echo "net.ipv4.ip_forward=1" > /etc/sysctl.d/ip_forward.conf

# Firewall
mv /tmp/packer/network/nftables.conf /etc/nftables.conf
chmod 644 /etc/nftables.conf
systemctl enable nftables.service
