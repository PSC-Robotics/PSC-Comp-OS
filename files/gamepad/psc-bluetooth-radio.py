#!/usr/bin/env python3
"""Make Bluetooth share the radio gently when the robot's hotspot is on 2.4GHz.

Wi-Fi and Bluetooth share one radio on a Raspberry Pi. With the hotspot on 2.4GHz
(always on a Pi 3 Model B, which has no 5GHz, or with wifi_band = 2.4), busy Bluetooth
starves the hotspot and phones and laptops lose the robot. Before bluetoothd starts,
this sets, for a 2.4GHz hotspot:

  - FastConnectable off: listening for controllers every 1.28s instead of 160ms
    (a controller turned on still connects within a couple of seconds)
  - a light background scan for paired Bluetooth LE controllers (Xbox) that are off:
    15ms in every 160ms instead of 30ms in every 60ms
  - DualShock 4s report every 16ms instead of 4ms, a quarter of the radio time

With a 5GHz hotspot Bluetooth has 2.4GHz to itself, so the faster settings are kept.
Runs from psc-bluetooth-radio.service; the band is chosen as astwifid chooses it.
"""
from __future__ import annotations  # the system Python 3.9 runs this

import re
from pathlib import Path

MAIN_CONF = Path("/etc/bluetooth/main.conf")
SETTINGS = (Path("/boot/psc-robot.txt"), Path("/boot/firmware/psc-robot.txt"))
# Keep in step with FIVE_GHZ_BOARDS in files/astoria/patch_astwifid.py
FIVE_GHZ_BOARDS = ("Pi 4", "Pi 400", "Pi 3 Model B Plus", "Pi 3 Model A Plus", "Compute Module 4", "Pi 5")
DS4_RULE = Path("/run/udev/rules.d/90-psc-ds4-poll.rules")
# hid-sony's bt_poll_interval, in ms (4 by default, up to 62)
DS4_RULE_TEXT = ('ACTION=="bind", SUBSYSTEM=="hid", DRIVER=="sony", TEST=="bt_poll_interval", '
                 'ATTR{bt_poll_interval}="16"\n')


def wifi_band() -> str:
    for settings in SETTINGS:
        try:
            lines = settings.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            key, _, value = line.split("#", 1)[0].partition("=")
            if key.strip().lower() == "wifi_band":
                return value.strip().strip('"').lower() or "auto"
    return "auto"


def hotspot_on_2_4ghz() -> bool:
    try:
        model = Path("/proc/device-tree/model").read_text()
    except OSError:
        model = ""
    supports_5ghz = any(name in model for name in FIVE_GHZ_BOARDS)
    return wifi_band() == "2.4" or not supports_5ghz


def set_option(text: str, section: str, key: str, value: str | None) -> str:
    """Set key in [section], uncommented, or remove it (value None) to use the default."""
    text = re.sub(rf"^#?\s*{key}\s*=.*\n", "", text, flags=re.MULTILINE)
    if value is None:
        return text
    header = f"[{section}]\n"
    if header not in text:
        text += f"\n{header}"
    return text.replace(header, f"{header}{key} = {value}\n", 1)


def main() -> None:
    shared = hotspot_on_2_4ghz()
    text = original = MAIN_CONF.read_text()
    text = set_option(text, "General", "FastConnectable", "false" if shared else "true")
    # BlueZ 5.55 reads these as decimal, in units of 0.625ms
    text = set_option(text, "Controller", "LEScanIntervalAutoConnect", "256" if shared else None)
    text = set_option(text, "Controller", "LEScanWindowAutoConnect", "24" if shared else None)
    if text != original:
        MAIN_CONF.write_text(text)
    if shared:
        DS4_RULE.parent.mkdir(parents=True, exist_ok=True)
        DS4_RULE.write_text(DS4_RULE_TEXT)
    else:
        DS4_RULE.unlink(missing_ok=True)
    print(f"Hotspot on {'2.4' if shared else '5'}GHz: "
          f"{'gentle' if shared else 'fast'} Bluetooth settings")


if __name__ == "__main__":
    main()
