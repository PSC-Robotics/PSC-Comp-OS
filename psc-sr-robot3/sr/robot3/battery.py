"""The battery voltage, measured by the brain board's HAT."""
from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from .exceptions import BatteryMonitorError
from .mqtt import MQTTClient

LOGGER = logging.getLogger(__name__)

BATTERY_TOPIC = 'psc/battery'
# psc-statusd publishes a reading every second; one older than this means it has
# stopped (its last message stays retained on the broker).
STALE_AFTER_S = 5.0
# How long the first read waits for a reading after the code starts.
FIRST_READING_TIMEOUT_S = 3.0
BOOT_ID_FILE = Path('/proc/sys/kernel/random/boot_id')


def _boot_id() -> str:
    try:
        return BOOT_ID_FILE.read_text().strip()
    except OSError:
        return ""


class BrainBatterySensor:
    """
    The battery sensor on the brain board's HAT, which measures the voltage of the
    12V input. Read it with ``robot.brain.battery_sensor.voltage``, as on Student
    Robotics' power board.

    The status service (psc-statusd) owns the sensor: it reads it every second, for the
    web interface and the low battery alarm, and shares each reading. This reads those,
    so robot code never talks to the sensor itself and can't upset the alarm.

    :param mqtt_client: The MQTT client used to reach the status service, or None if
        there is no status service (e.g. in the simulator).
    :param clock: The monotonic clock the status service stamps readings with;
        replaced in tests.
    :param boot_id: This boot's ID; replaced in tests.
    """

    __slots__ = ('_mqtt', '_clock', '_boot_id', '_lock', '_message')

    def __init__(
        self,
        mqtt_client: Optional[MQTTClient],
        clock: Callable[[], float] = time.monotonic,
        boot_id: Optional[str] = None,
    ) -> None:
        self._mqtt = mqtt_client
        self._clock = clock
        self._boot_id = _boot_id() if boot_id is None else boot_id
        self._lock = threading.Condition()
        self._message: Optional[dict] = None
        if mqtt_client is not None:
            mqtt_client.subscribe(BATTERY_TOPIC, self._on_message, abs_path=True)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            data = json.loads(message.payload)
            voltage = data["voltage"]
            parsed = {
                "voltage": None if voltage is None else float(voltage),
                "low": bool(data["low"]),
                "time": float(data["time"]),
                "boot_id": str(data["boot_id"]),
            }
        except (ValueError, TypeError, KeyError, AttributeError):
            LOGGER.debug("Ignoring a battery message in an unexpected format")
            return
        with self._lock:
            self._message = parsed
            self._lock.notify_all()

    def _is_current(self, message: Optional[dict]) -> bool:
        if message is None or message["boot_id"] != self._boot_id:
            return False
        age = self._clock() - float(message["time"])
        return -1.0 <= age <= STALE_AFTER_S

    def _reading(self) -> dict:
        """The latest reading, waiting briefly for one if the code has just started."""
        if self._mqtt is None:
            raise BatteryMonitorError("The battery voltage is only available on the robot")
        with self._lock:
            # A retained reading arrives as soon as the code subscribes, but it may be
            # out of date; a current one arrives within a second.
            self._lock.wait_for(lambda: self._is_current(self._message),
                                timeout=FIRST_READING_TIMEOUT_S)
            message = self._message
        if message is None or not self._is_current(message):
            raise BatteryMonitorError(
                "No up-to-date battery reading from the status service. "
                "Check it's running: systemctl status psc-statusd")
        return message

    @property
    def voltage(self) -> float:
        """
        The battery voltage in volts, read within the last second.

        A reading below 6V means no battery is connected: the Pi is powered another
        way, such as over USB.

        :raises BatteryMonitorError: If there's no reading, for example because the
            HAT's battery sensor isn't responding or the status service isn't running.
        """
        voltage: Optional[float] = self._reading()["voltage"]
        if voltage is None:
            raise BatteryMonitorError(
                "The HAT's battery sensor isn't responding (an ADS1115 at I2C address 0x48)")
        return voltage

    @property
    def low(self) -> bool:
        """
        Whether the low battery alarm is on. The robot stops your code when it comes
        on, so this is mostly useful just after the code starts.

        :raises BatteryMonitorError: If there's no reading.
        """
        return bool(self._reading()["low"])

    def __repr__(self) -> str:
        with self._lock:
            message = self._message
        if message is None or message["voltage"] is None or not self._is_current(message):
            return f"<{self.__class__.__qualname__} no reading>"
        return f"<{self.__class__.__qualname__} voltage={message['voltage']:.2f}V>"
