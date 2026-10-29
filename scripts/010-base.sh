#!/bin/bash
set -eux -o pipefail

# Delete downloaded .debs once installed; the 4G image has little spare room.
echo 'APT::Keep-Downloaded-Packages "false";' > /etc/apt/apt.conf.d/99no-package-cache

apt-get -y update

# Package install
apt-get -y install \
    nano \
    vim \
    htop \
    git
