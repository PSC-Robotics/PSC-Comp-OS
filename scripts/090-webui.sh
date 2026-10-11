#!/bin/bash
set -eux -o pipefail

# Install the webserver
apt-get -y install nginx

# Download latest kit-ui release
UI_URL="https://api.github.com/repos/srobo/kit-ui/releases/latest"

mkdir /tmp/webui
curl --fail --location --silent --show-error "$UI_URL" -o /tmp/webui/release.json
UI_ZIP_URL="$(sed -nr 's/^[[:space:]]*"browser_download_url": "(.*\.zip)".*/\1/p' /tmp/webui/release.json | head -n 1)"
if [ -z "$UI_ZIP_URL" ]; then
	echo "Unable to find a kit-ui release archive at $UI_URL" >&2
	exit 1
fi
curl --fail --location --silent --show-error "$UI_ZIP_URL" -o /tmp/webui/webui.zip

# Move website to webserver dir
unzip /tmp/webui/webui.zip -d /tmp/webui/

mkdir /usr/share/kit-ui
mv /tmp/webui/dist/* /usr/share/kit-ui/
# PSC additions to the main UI: camera calibration dialog, uncalibrated-camera
# banner and multi-camera view. /calibrate redirects to the dialog.
install -o root -g root -m 644 /tmp/packer/webui/calibrate.html /usr/share/kit-ui/calibrate.html
install -o root -g root -m 644 /tmp/packer/webui/psc-ui.js /usr/share/kit-ui/psc-ui.js
install -o root -g root -m 644 /tmp/packer/webui/psc-ui.css /usr/share/kit-ui/psc-ui.css
sed -i 's|</body>|<script src="/psc-ui.js" defer></script></body>|' /usr/share/kit-ui/index.html
grep -q 'psc-ui.js' /usr/share/kit-ui/index.html

chown -R root:www-data /usr/share/kit-ui
chmod -R 755 /usr/share/kit-ui

# Set config
rm /etc/nginx/sites-enabled/default
mv /tmp/packer/webui/kit-ui.nginx /etc/nginx/sites-enabled/

nginx -t
systemctl enable nginx.service
