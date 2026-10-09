#!/bin/bash
set -eux -o pipefail

# Bullseye is archived; its security package indexes must come from the archive.
sed -i 's|http://security.debian.org/debian-security|http://archive.debian.org/debian-security|' /etc/apt/sources.list
echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99archive

apt-get -y update

# Package install
apt-get -y install \
    nano \
    vim \
    htop \
    git
