#!/bin/bash
set -eux -o pipefail

# Load pyenv python into path
source ~/.profile

pip install --upgrade pip setuptools wheel

# Install base python packages
pip install -r /tmp/packer/python/requirements.txt

# Install the customized robot API shipped by the image build.
SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SR_ROBOT3=2026.0.3+psc.1 \
	pip wheel --no-deps --wheel-dir /tmp/psc-sr-robot3-wheel /tmp/psc-sr-robot3
pip install /tmp/psc-sr-robot3-wheel/sr_robot3-2026.0.3+psc.1-py3-none-any.whl

# Install additional useful libraries for the competitors
pip install -r /tmp/packer/python/libraries.txt

# Make the entrypoint folder
mkdir -p ~/.local/bin

# Record the installed packages
pip freeze > py-packages.txt
