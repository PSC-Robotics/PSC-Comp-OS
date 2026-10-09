#!/bin/bash
set -u

model="$(tr -d '\0' </proc/device-tree/model 2>/dev/null || true)"
case "$model" in
    "Raspberry Pi 3 Model B Rev"*) ;;
    *) exit 0 ;;
esac

if [ -e /sys/class/net/wlan0 ]; then
    logger -t psc-pi3-wifi "Default firmware initialized wlan0; no fallback needed."
    exit 0
fi

alternate=/lib/firmware/brcm/brcmfmac43430a0-sdio.bin
override=/lib/firmware/updates/brcm/brcmfmac43430-sdio.bin
if [ ! -r "$alternate" ]; then
    logger -t psc-pi3-wifi "Alternate BCM43430 firmware is unavailable."
    exit 0
fi

logger -t psc-pi3-wifi "wlan0 missing; retrying BCM43430 with alternate packaged firmware."
install -D -m 0644 "$alternate" "$override"
modprobe -r brcmfmac 2>/dev/null || true
if ! modprobe brcmfmac; then
    logger -t psc-pi3-wifi "Could not reload brcmfmac with alternate firmware."
    rm -f "$override"
    modprobe brcmfmac 2>/dev/null || true
    exit 0
fi

for attempt in 1 2 3 4 5; do
    [ -e /sys/class/net/wlan0 ] && break
    sleep 1
done

if [ -e /sys/class/net/wlan0 ]; then
    logger -t psc-pi3-wifi "Alternate firmware initialized wlan0."
    exit 0
fi

logger -t psc-pi3-wifi "Alternate firmware also failed; restoring default firmware."
rm -f "$override"
modprobe -r brcmfmac 2>/dev/null || true
modprobe brcmfmac 2>/dev/null || true
exit 0