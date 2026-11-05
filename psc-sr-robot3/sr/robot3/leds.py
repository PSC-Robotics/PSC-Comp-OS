"""The LED on the brain board that robot code controls."""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Optional, Sequence, Union

from .logging import log_to_debug
from .mqtt import MQTTClient

LOGGER = logging.getLogger(__name__)

LEDS_TOPIC = 'psc/leds'
STATUS_CONFIG = Path('/etc/psc-status.toml')

Colour = Sequence[Union[int, bool]]
RGB = tuple[int, int, int]
OFF: RGB = (0, 0, 0)
COLOUR_HELP = "A colour is (red, green, blue), each from 0 to 255"


def to_rgb(colour: Colour) -> RGB:
    """
    Check a colour: (red, green, blue) from 0 to 255, or a ``Colour`` such as
    ``Colour.RED``, whose True parts are full brightness.
    """
    try:
        parts = tuple(colour)
    except TypeError:
        raise ValueError(COLOUR_HELP) from None
    if len(parts) != 3:
        raise ValueError(COLOUR_HELP)
    rgb = []
    for part in parts:
        if isinstance(part, bool):
            rgb.append(255 if part else 0)
        elif isinstance(part, int) and 0 <= part <= 255:
            rgb.append(part)
        else:
            raise ValueError(COLOUR_HELP)
    return (rgb[0], rgb[1], rgb[2])


class BrainLED:
    """
    The LED on the brain board that robot code controls, which can be any colour.

    Set it with ``robot.brain.led.colour = Colour.RED``, or to (red, green, blue), each
    from 0 to 255. It turns off when your code ends. The robot's status service drives
    it, as the same LED chain has the status LED on it.

    :param mqtt_client: The MQTT client used to reach the status service, or None if
        there is no status service (e.g. in the simulator).
    """
    __slots__ = ('_mqtt', '_rgb', '_warned')

    def __init__(self, mqtt_client: Optional[MQTTClient]) -> None:
        self._mqtt = mqtt_client
        self._rgb = OFF
        self._warned = False

    @property
    @log_to_debug
    def colour(self) -> Colour:
        """
        The LED's colour: a ``Colour`` such as ``Colour.RED`` if it is one, otherwise
        (red, green, blue), each from 0 to 255.
        """
        if all(part in (0, 255) for part in self._rgb):
            return tuple(part == 255 for part in self._rgb)
        return self._rgb

    @colour.setter
    @log_to_debug(setter=True)
    def colour(self, value: Colour) -> None:
        self._rgb = to_rgb(value)
        if self._mqtt is None:
            if not self._warned:
                LOGGER.warning("The LED is only available on the robot, so it won't change")
                self._warned = True
            return
        self._mqtt.publish(
            LEDS_TOPIC, json.dumps({"colours": [list(self._rgb)]}), abs_topic=True)

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} colour={self._rgb}>"
