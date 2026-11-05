"""The WS2812 LED chain on the PSC robot, driven by the robot's status service."""
from __future__ import annotations

import json
import logging
import sys
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union, overload

from .kch import LED
from .mqtt import MQTTClient

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

LOGGER = logging.getLogger(__name__)

LEDS_TOPIC = 'psc/leds'
STATUS_CONFIG = Path('/etc/psc-status.toml')
# The image's default: a status LED followed by one LED for robot code.
DEFAULT_CHAIN_LENGTH = 2

Colour = Sequence[Union[int, bool]]
RGB = tuple[int, int, int]
OFF: RGB = (0, 0, 0)


def user_led_count(config_file: Path = STATUS_CONFIG) -> int:
    """The number of LEDs in the chain that robot code controls."""
    try:
        strip = tomllib.loads(config_file.read_text()).get('led_strip', {})
    except (OSError, ValueError):
        return DEFAULT_CHAIN_LENGTH - 1
    if not strip.get('enabled', True):
        return 0
    count = strip.get('count', DEFAULT_CHAIN_LENGTH)
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        return 0
    # The first LED shows the robot's status, unless the status service is told not to.
    return max(0, count - 1) if strip.get('status_led', True) else count


def to_rgb(colour: Colour) -> RGB:
    """
    Check a colour: (red, green, blue) from 0 to 255, or a ``Colour`` such as
    ``Colour.RED``, whose True parts are full brightness.
    """
    try:
        parts = tuple(colour)
    except TypeError:
        raise ValueError("A colour is (red, green, blue), each from 0 to 255") from None
    if len(parts) != 3:
        raise ValueError("A colour is (red, green, blue), each from 0 to 255")
    rgb = []
    for part in parts:
        if isinstance(part, bool):
            rgb.append(255 if part else 0)
        elif isinstance(part, int) and 0 <= part <= 255:
            rgb.append(part)
        else:
            raise ValueError("A colour is (red, green, blue), each from 0 to 255")
    return (rgb[0], rgb[1], rgb[2])


class LEDStrip:
    """
    The WS2812 ("NeoPixel") LEDs chained after the robot's status LED.

    ``robot.leds[0]`` is the first LED after the status LED. Set an LED to a
    colour of (red, green, blue), each from 0 to 255, or to a ``Colour`` such as
    ``Colour.RED``. The LEDs turn off when your code ends.

    The robot's status service drives the chain, as it also shows the robot's
    status on the first LED.

    :param mqtt_client: The MQTT client used to reach the status service, or
        None if there is no status service (e.g. in the simulator).
    :param count: The number of LEDs robot code controls.
    """
    __slots__ = ('_mqtt', '_colours', '_warned')

    def __init__(self, mqtt_client: Optional[MQTTClient], count: int) -> None:
        self._mqtt = mqtt_client
        self._colours: list[RGB] = [OFF] * count
        self._warned = False

    def __len__(self) -> int:
        return len(self._colours)

    @overload
    def __getitem__(self, index: int) -> RGB:
        ...

    @overload
    def __getitem__(self, index: slice) -> list[RGB]:
        ...

    def __getitem__(self, index: Union[int, slice]) -> Union[RGB, list[RGB]]:
        if isinstance(index, slice):
            return self._colours[index]
        return self._colours[self._index(index)]

    def __setitem__(self, index: Union[int, slice], colour) -> None:
        if isinstance(index, slice):
            indices = range(*index.indices(len(self._colours)))
            colours = [to_rgb(c) for c in colour]
            if len(colours) != len(indices):
                raise ValueError(f"Give {len(indices)} colours, one for each LED")
            for i, rgb in zip(indices, colours):
                self._colours[i] = rgb
        else:
            self._colours[self._index(index)] = to_rgb(colour)
        self._send()

    def __iter__(self):
        return iter(list(self._colours))

    def fill(self, colour: Colour) -> None:
        """Set every LED to one colour."""
        rgb = to_rgb(colour)
        self._colours[:] = [rgb] * len(self._colours)
        self._send()

    def set_all(self, colours: Iterable[Colour]) -> None:
        """Set every LED at once, from a list with one colour for each LED."""
        self[:] = list(colours)

    def off(self) -> None:
        """Turn every LED off."""
        self.fill(OFF)

    def _index(self, index: int) -> int:
        if isinstance(index, bool) or not isinstance(index, int):
            raise TypeError("An LED's index must be a whole number")
        if not self._colours:
            raise IndexError(
                "There are no LEDs for robot code: set the chain's count in "
                "/etc/psc-status.toml on the robot")
        if not -len(self._colours) <= index < len(self._colours):
            raise IndexError(f"LED {index} doesn't exist: there are {len(self._colours)} "
                             f"(0 to {len(self._colours) - 1})")
        return index % len(self._colours)

    def _send(self) -> None:
        if self._mqtt is None:
            if not self._warned:
                LOGGER.warning("The LED strip is not available, so the LEDs won't change")
                self._warned = True
            return
        self._mqtt.publish(
            LEDS_TOPIC,
            json.dumps({"colours": [list(c) for c in self._colours]}),
            abs_topic=True,
        )

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} count={len(self._colours)}>"


class ChainLED(LED):
    """
    A user LED, as on Student Robotics' KCH board, that is an LED in the WS2812
    chain: ``robot.kch.leds[LED_A]`` (or ``robot.raspberry_pi.leds[LED_A]``) is
    the first LED after the status LED, ``LED_B`` the next, and so on.

    Set it with ``led.colour = Colour.RED``, or one part at a time with
    ``led.r = True``. A colour can also be (red, green, blue), each from 0 to 255.
    """
    __slots__ = ('_strip', '_index', '_number')

    def __init__(self, strip: LEDStrip, number: int) -> None:
        self._strip = strip
        self._number = number
        self._index: Optional[int] = number if number < len(strip) else None

    def _get(self) -> RGB:
        return OFF if self._index is None else self._strip[self._index]

    def _set(self, colour: Colour) -> None:
        rgb = to_rgb(colour)
        if self._index is None:
            LOGGER.warning(
                f"LED_{'ABC'[self._number]} "
                f"isn't connected: the robot's LED chain has {len(self._strip)} LED(s) "
                "for your code; set the chain's count in /etc/psc-status.toml on the robot")
            return
        self._strip[self._index] = rgb

    def _set_part(self, part: int, value: Union[bool, int]) -> None:
        colour = list(self._get())
        colour[part] = value if not isinstance(value, bool) else (255 if value else 0)
        self._set(colour)

    @property
    def r(self) -> bool:
        """Get the state of the Red LED segment."""
        return self._get()[0] > 0

    @r.setter
    def r(self, value: bool) -> None:
        """Set the state of the Red LED segment."""
        self._set_part(0, value)

    @property
    def g(self) -> bool:
        """Get the state of the Green LED segment."""
        return self._get()[1] > 0

    @g.setter
    def g(self, value: bool) -> None:
        """Set the state of the Green LED segment."""
        self._set_part(1, value)

    @property
    def b(self) -> bool:
        """Get the state of the Blue LED segment."""
        return self._get()[2] > 0

    @b.setter
    def b(self, value: bool) -> None:
        """Set the state of the Blue LED segment."""
        self._set_part(2, value)

    @property
    def colour(self):  # type: ignore[override]
        """
        Get the colour of the user LED: a ``Colour`` such as ``Colour.RED`` if
        it is one, otherwise (red, green, blue), each from 0 to 255.
        """
        rgb = self._get()
        if all(part in (0, 255) for part in rgb):
            return tuple(part == 255 for part in rgb)
        return rgb

    @colour.setter
    def colour(self, value: Colour) -> None:
        """Set the colour of the user LED."""
        self._set(value)

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} {'ABC'[self._number]} colour={self.colour}>"


def chain_leds(strip: LEDStrip) -> tuple[ChainLED, ...]:
    """The user LEDs, LED_A to LED_C, on the WS2812 chain."""
    return tuple(ChainLED(strip, number) for number in range(3))
