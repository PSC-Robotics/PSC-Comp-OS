#!/bin/bash
set -eux -o pipefail

# Load pyenv python into path
source ~/.profile

# Don't keep pip's download cache: it would fill the image and ship in it.
export PIP_NO_CACHE_DIR=1

pip install --upgrade pip setuptools wheel

# Install base python packages
pip install -r /tmp/packer/python/requirements.txt

# Install the customized robot API shipped by the image build.
# Build from a clean copy of the source so stale build/, *.egg-info and
# _version.py files from the host checkout can't leak old code into the wheel.
PSC_SR_SRC=/tmp/psc-sr-robot3
PSC_SR_CLEAN=/tmp/psc-sr-robot3-clean
PSC_SR_WHEELS=/tmp/psc-sr-robot3-wheel
PSC_SR_VERSION=2026.0.3+psc.15
rm -rf "$PSC_SR_CLEAN" "$PSC_SR_WHEELS"
mkdir -p "$PSC_SR_CLEAN"
tar -C "$PSC_SR_SRC" \
	--exclude=./.git --exclude=./build --exclude=./dist --exclude='*.egg-info' \
	--exclude=__pycache__ --exclude=.pytest_cache --exclude=.DS_Store \
	--exclude=./sr/robot3/_version.py \
	-cf - . | tar -C "$PSC_SR_CLEAN" -xf -
SETUPTOOLS_SCM_PRETEND_VERSION_FOR_SR_ROBOT3=$PSC_SR_VERSION \
	pip wheel --no-deps --no-cache-dir --wheel-dir "$PSC_SR_WHEELS" "$PSC_SR_CLEAN"
pip install "$PSC_SR_WHEELS"/sr_robot3-*.whl

# Fail the build unless the installed API is exactly the source we were given.
python - "$PSC_SR_CLEAN" "$PSC_SR_VERSION" << "EOF"
# Don't import sr.robot3 here: RPi.GPIO refuses to load outside a real Pi.
import ast, filecmp, importlib.util, pathlib, sys
from importlib.metadata import version

src = pathlib.Path(sys.argv[1]) / "sr" / "robot3"
dst = pathlib.Path(importlib.util.find_spec("sr.robot3").origin).parent
files = sorted(p.relative_to(src) for p in src.rglob("*") if p.is_file())
bad = [str(f) for f in files if not (dst / f).is_file() or not filecmp.cmp(src / f, dst / f, shallow=False)]
assert not bad, f"installed sr.robot3 differs from source: {bad}"
assert version("sr-robot3") == sys.argv[2], version("sr-robot3")
arduino = ast.parse((dst / "arduino.py").read_text())
methods = {
    n.name for c in arduino.body if isinstance(c, ast.ClassDef) and c.name == "Arduino"
    for n in c.body if isinstance(n, ast.FunctionDef)
}
assert "stepper_move" in methods, "stepper support missing"
print(f"sr-robot3 {sys.argv[2]} verified: {len(files)} files match source")
EOF
rm -rf "$PSC_SR_CLEAN" "$PSC_SR_WHEELS"

# Install additional useful libraries for the competitors
pip install -r /tmp/packer/python/libraries.txt

# Make the entrypoint folder
mkdir -p ~/.local/bin

# Record the installed packages
pip freeze > py-packages.txt
