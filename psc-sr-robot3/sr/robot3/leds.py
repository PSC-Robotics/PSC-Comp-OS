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


CHANNEL_HELP = "A colour channel is True (on), False (off) or a number from 0 to 255"


def to_channel(value: Union[int, bool]) -> int:
    """Check one colour channel: True or False as on Student Robotics' LEDs, or 0 to 255."""
    if isinstance(value, bool):
        return 255 if value else 0
    if isinstance(value, int) and 0 <= value <= 255:
        return value
    raise ValueError(CHANNEL_HELP)


def from_channel(value: int) -> Union[int, bool]:
    """A channel as Student Robotics gives it (True or False) when fully on or off."""
    return value == 255 if value in (0, 255) else value


class BrainLED:
    """
    The LED on the brain board that robot code controls, which can be any colour.

    Set it with ``robot.brain.led.colour = Colour.RED``, or to (red, green, blue), each
    from 0 to 255. Or set its red, green and blue channels separately, as on Student
    Robotics' LEDs: ``robot.brain.led.r = True``, or a number from 0 to 255 for
    brightness. It turns off when your code ends. The robot's status service drives
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

    def _set_channel(self, index: int, value: Union[int, bool]) -> None:
        rgb = list(self._rgb)
        rgb[index] = to_channel(value)
        self.colour = (rgb[0], rgb[1], rgb[2])

    @property
    @log_to_debug
    def r(self) -> Union[int, bool]:
        """
        The red channel: True (fully on) or False (off), or a number from 1 to 254 if
        it's set to part brightness.
        """
        return from_channel(self._rgb[0])

    @r.setter
    @log_to_debug(setter=True)
    def r(self, value: Union[int, bool]) -> None:
        """
        Set the red channel, leaving green and blue as they are.

        :param value: True (on), False (off) or a brightness from 0 to 255.
        :raises ValueError: If the value isn't one of those.
        """
        self._set_channel(0, value)

    @property
    @log_to_debug
    def g(self) -> Union[int, bool]:
        """
        The green channel: True (fully on) or False (off), or a number from 1 to 254 if
        it's set to part brightness.
        """
        return from_channel(self._rgb[1])

    @g.setter
    @log_to_debug(setter=True)
    def g(self, value: Union[int, bool]) -> None:
        """
        Set the green channel, leaving red and blue as they are.

        :param value: True (on), False (off) or a brightness from 0 to 255.
        :raises ValueError: If the value isn't one of those.
        """
        self._set_channel(1, value)

    @property
    @log_to_debug
    def b(self) -> Union[int, bool]:
        """
        The blue channel: True (fully on) or False (off), or a number from 1 to 254 if
        it's set to part brightness.
        """
        return from_channel(self._rgb[2])

    @b.setter
    @log_to_debug(setter=True)
    def b(self, value: Union[int, bool]) -> None:
        """
        Set the blue channel, leaving red and green as they are.

        :param value: True (on), False (off) or a brightness from 0 to 255.
        :raises ValueError: If the value isn't one of those.
        """
        self._set_channel(2, value)

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} colour={self._rgb}>"
