"""Run the robot's hotspot on 5GHz where the Wi-Fi supports it, to keep 2.4GHz free for Bluetooth.

Astoria's hotspot is fixed to 2.4GHz channel 6. Bluetooth shares the Pi's radio and the
2.4GHz band, so a 2.4GHz hotspot makes Bluetooth gamepads fail to connect. Usage:
    python patch_astwifid.py <path to astoria/astwifid.py>
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
text = text.replace(old, '''            **_hotspot_band(),
''')
helper = '''

def _hotspot_band() -> dict:
    """5GHz on boards whose Wi-Fi supports it (PSC Robotics): keeps 2.4GHz for Bluetooth."""
    try:
        model = Path("/proc/device-tree/model").read_text()
    except OSError:
        model = ""
    if any(name in model for name in (
        "Pi 4", "Pi 400", "Pi 3 Model B Plus", "Pi 3 Model A Plus", "Compute Module 4", "Pi 5",
    )):
        return {"channel": 36, "hw_mode": "a"}
    return {"channel": 6, "hw_mode": "g"}
'''
marker = "\nLOGGER = "
assert text.count(marker) == 1
text = text.replace(marker, helper + marker, 1)
assert "from pathlib import Path" in text
path.write_text(text)
print("patched")
