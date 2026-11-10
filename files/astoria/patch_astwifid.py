"""Let the robot's hotspot run on 5GHz, chosen per robot in psc-robot.txt on the SD card.

Astoria's hotspot is fixed to 2.4GHz channel 6. Bluetooth shares the Pi's radio and the
2.4GHz band, so a 2.4GHz hotspot makes Bluetooth gamepads fail to connect. This patches
astwifid to read "wifi_band" from psc-robot.txt on the boot partition:

    auto  5GHz on boards whose Wi-Fi supports it, otherwise 2.4GHz (the default)
    5     5GHz (falls back to 2.4GHz on boards without 5GHz Wi-Fi)
    2.4   2.4GHz, which every phone and laptop can see

Usage: python patch_astwifid.py <path to astoria/astwifid.py>
"""
import sys
from pathlib import Path

path = Path(sys.argv[1])
text = path.read_text()
if "_hotspot_band" in text:
    print("already patched")
    sys.exit(0)
old = '''            "channel": 6,
            "hw_mode": "g",
'''
assert text.count(old) == 1, "astwifid.py has changed: check the hostapd settings by hand"
assert "from pathlib import Path" in text
text = text.replace(old, '''            **_hotspot_band(),
''')
helper = '''

PSC_ROBOT_SETTINGS = (Path("/boot/psc-robot.txt"), Path("/boot/firmware/psc-robot.txt"))
# Keep in step with files/gamepad/psc-bluetooth-radio.py
FIVE_GHZ_BOARDS = ("Pi 4", "Pi 400", "Pi 3 Model B Plus", "Pi 3 Model A Plus", "Compute Module 4", "Pi 5")


def _psc_setting(name: str, default: str) -> str:
    """A "name = value" setting from psc-robot.txt on the SD card (PSC Robotics)."""
    for settings in PSC_ROBOT_SETTINGS:
        try:
            lines = settings.read_text().splitlines()
        except (OSError, UnicodeDecodeError):
            continue
        for line in lines:
            key, _, value = line.split("#", 1)[0].partition("=")
            if key.strip().lower() == name:
                return value.strip().strip('"').lower() or default
    return default


def _hotspot_band() -> dict:
    """The hotspot's band: 5GHz keeps 2.4GHz free for Bluetooth gamepads (PSC Robotics)."""
    try:
        model = Path("/proc/device-tree/model").read_text()
    except OSError:
        model = ""
    supports_5ghz = any(name in model for name in FIVE_GHZ_BOARDS)
    band = _psc_setting("wifi_band", "auto")
    if band not in ("auto", "5", "2.4"):
        LOGGER.warning(f"Unknown wifi_band {band!r} in psc-robot.txt; using auto")
        band = "auto"
    if band == "5" and not supports_5ghz:
        LOGGER.warning("wifi_band is 5, but this board's Wi-Fi is 2.4GHz only")
    if band != "2.4" and supports_5ghz:
        LOGGER.info("Hotspot on 5GHz (channel 36)")
        return {"channel": 36, "hw_mode": "a"}
    LOGGER.info("Hotspot on 2.4GHz (channel 6)")
    return {"channel": 6, "hw_mode": "g"}
'''
marker = "\nLOGGER = logging.getLogger(__name__)\n"
assert text.count(marker) == 1
text = text.replace(marker, marker + helper, 1)
path.write_text(text)
print("patched")
