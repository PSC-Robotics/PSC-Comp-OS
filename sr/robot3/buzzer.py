"""The buzzer on the PSC robot, played by the robot's status service."""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional

from .mqtt import MQTTClient
from .utils import float_bounds_check

LOGGER = logging.getLogger(__name__)

BUZZER_TOPIC = 'psc/buzzer'


class Buzzer:
    """
    The buzzer connected to the brain board.

    Tones are played by the robot's status service, which also uses the
    buzzer for system sounds such as the start beep, so the two never drive
    the buzzer pin at the same time.

    :param mqtt_client: The MQTT client used to reach the status service, or
        None if there is no status service (e.g. in the simulator).
    :param sleep_fn: The function used to wait for blocking tones.
    """
    __slots__ = ('_mqtt', '_sleep_fn')

    def __init__(
        self,
        mqtt_client: Optional[MQTTClient],
        sleep_fn: Callable[[float], None],
    ) -> None:
        self._mqtt = mqtt_client
        self._sleep_fn = sleep_fn

    def buzz(
        self,
        frequency: float,
        duration: float,
        *,
        blocking: bool = False,
        volume: Optional[float] = None,
    ) -> None:
        """
        Produce a tone on the buzzer.

        This method is non-blocking, and sending another tone while one is
        playing will cancel the first.

        :param frequency: The frequency of the tone, in Hz. A `Note` can be used.
        :param duration: The duration of the tone, in seconds.
        :param blocking: Whether to block until the tone has finished playing.
        :param volume: The volume, from 0 (silent) to 50 (loudest). Defaults to
            the robot's code volume setting.
        :raises ValueError: If the frequency, duration or volume is out of range.
        """
        frequency_int = int(float_bounds_check(
            frequency, 8, 10_000, "Frequency must be between 8 and 10000Hz"))
        duration_s = float_bounds_check(
            duration, 0, 60, "Duration is a float in seconds between 0 and 60")
        request: dict[str, float] = {"frequency": frequency_int, "duration": duration_s}
        if volume is not None:
            request["volume"] = float_bounds_check(
                volume, 0, 50, "Volume must be between 0 and 50")

        if self._mqtt is None:
            LOGGER.warning("The buzzer is not available, so the tone was not played")
        else:
            self._mqtt.publish(
                BUZZER_TOPIC,
                json.dumps(request),
                abs_topic=True,
            )

        if blocking:
            self._sleep_fn(duration_s)

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}>"
