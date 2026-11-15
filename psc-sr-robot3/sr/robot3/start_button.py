"""The start button on the brain board."""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any, Callable, Optional

from .battery import _boot_id
from .logging import log_to_debug
from .mqtt import MQTTClient

LOGGER = logging.getLogger(__name__)

BUTTON_TOPIC = 'psc/start_button'
# psc-statusd shares the button's state every second; an older one means it has stopped.
STALE_AFTER_S = 5.0


class BrainStartButton:
    """
    The start button on the brain board: ``robot.brain.start_button``.

    Once your code has started, you can use the start button for anything:
    ``is_pressed`` says whether it's held down now, and ``was_pressed()`` whether it's
    been pressed since you last asked, so a quick press isn't missed while your code is
    busy. ``robot.wait_start()`` still waits for it, as before.

    The robot's status service reads the button (it also starts your code from it), and
    shares its state, which this reads.

    :param mqtt_client: The MQTT client used to reach the status service, or None if
        there is no status service (e.g. in the simulator).
    :param clock: The monotonic clock the status service stamps its state with;
        replaced in tests.
    :param boot_id: This boot's ID; replaced in tests.
    """

    __slots__ = ('_mqtt', '_clock', '_boot_id', '_lock', '_message', '_seen', '_warned')

    def __init__(
        self,
        mqtt_client: Optional[MQTTClient],
        clock: Callable[[], float] = time.monotonic,
        boot_id: Optional[str] = None,
    ) -> None:
        self._mqtt = mqtt_client
        self._clock = clock
        self._boot_id = _boot_id() if boot_id is None else boot_id
        self._lock = threading.Lock()
        self._message: Optional[dict[str, Any]] = None
        # Presses before this count were before the code was ready for them.
        self._seen: Optional[int] = None
        self._warned = False
        if mqtt_client is not None:
            mqtt_client.subscribe(BUTTON_TOPIC, self._on_message, abs_path=True)

    def _on_message(self, client: Any, userdata: Any, message: Any) -> None:
        try:
            data = json.loads(message.payload)
            parsed: dict[str, Any] = {
                "pressed": bool(data["pressed"]),
                "presses": int(data["presses"]),
                "time": float(data["time"]),
                "boot_id": str(data["boot_id"]),
            }
        except (ValueError, TypeError, KeyError, AttributeError):
            LOGGER.debug("Ignoring a start button message in an unexpected format")
            return
        with self._lock:
            if self._seen is None or parsed["presses"] < self._seen:
                self._seen = parsed["presses"]  # The first message, or psc-statusd restarted
            self._message = parsed

    def _current(self) -> Optional[dict[str, Any]]:
        """The latest state, if it's from this boot and up to date."""
        with self._lock:
            message = self._message
        if (message is not None and message["boot_id"] == self._boot_id
                and -1.0 <= self._clock() - message["time"] <= STALE_AFTER_S):
            return message
        if not self._warned:
            self._warned = True
            if self._mqtt is None:
                LOGGER.warning("The start button is only available on the robot")
            else:
                LOGGER.warning(
                    "No up-to-date start button state from the status service, so it "
                    "reads as not pressed. Check it's running: systemctl status psc-statusd")
        return None

    def _forget_presses(self) -> None:
        """Ignore presses so far, such as the one that started the code."""
        with self._lock:
            if self._message is not None:
                self._seen = self._message["presses"]

    @property
    @log_to_debug
    def is_pressed(self) -> bool:
        """
        Whether the start button is held down now.

        Reads False, with a warning in the log, if the robot can't read the button.
        """
        message = self._current()
        return bool(message and message["pressed"])

    @log_to_debug
    def was_pressed(self) -> bool:
        """
        Whether the start button has been pressed since this was last asked (or since
        the code started). Use this to do something once per press, even if your loop
        is slow: it's True once for each time you ask after one or more presses.

        Returns False, with a warning in the log, if the robot can't read the button.
        """
        message = self._current()
        if message is None:
            return False
        presses: int = message["presses"]
        with self._lock:
            pressed = self._seen is not None and presses > self._seen
            self._seen = presses
        return pressed

    def __repr__(self) -> str:
        with self._lock:
            message = self._message
        state = "unknown" if message is None else (
            "pressed" if message["pressed"] else "released")
        return f"<{self.__class__.__qualname__} {state}>"
