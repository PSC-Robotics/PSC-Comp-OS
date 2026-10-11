"""Control of devices wired directly to the Raspberry Pi's GPIO header."""
from __future__ import annotations

import atexit
import logging
import time
from types import ModuleType
from typing import Callable, Optional

from .logging import log_to_debug

LOGGER = logging.getLogger(__name__)

# Pins the robot itself uses: the status LED (17, 27, 22), the start button (5)
# and the buzzer (13). Pins 0 and 1 are reserved for the HAT EEPROM.
RESERVED_PINS = {0: "HAT EEPROM", 1: "HAT EEPROM", 5: "the start button", 13: "the buzzer",
                 17: "the status LED", 22: "the status LED", 27: "the status LED"}
MIN_STEP_DELAY_US = 50
MAX_STEP_DELAY_US = 1_000_000
MAX_STEPS = 1_000_000
STEP_PULSE_US = 5  # comfortably above the 1-2us minimum of common drivers
DIRECTION_SETUP_US = 5
ULTRASOUND_TRIGGER_US = 10  # the HC-SR04 needs a trigger pulse of at least 10us
ULTRASOUND_TIMEOUT_US = 60_000  # matches the Arduino firmware; beyond ~4m


def _load_gpio() -> ModuleType:
    try:
        import RPi.GPIO as GPIO  # isort: ignore
    except (ImportError, RuntimeError) as error:
        raise RuntimeError(
            "Raspberry Pi GPIO is only available when running on the robot"
        ) from error
    return GPIO


class RaspberryPi:
    """
    Devices wired directly to the brain board's Raspberry Pi GPIO header.

    Supports stepper motor drivers and ultrasound sensors. Pins are given as
    BCM GPIO numbers (e.g. 23 for GPIO 23, header pin 16), not header pin numbers.

    :param gpio_loader: Returns the RPi.GPIO module; replaced in tests.
    :param clock_ns: A monotonic clock in nanoseconds; replaced in tests.
    """
    __slots__ = ('_gpio', '_gpio_loader', '_clock_ns', '_pin_modes')

    def __init__(
        self,
        gpio_loader: Callable[[], ModuleType] = _load_gpio,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self._gpio: Optional[ModuleType] = None
        self._gpio_loader = gpio_loader
        self._clock_ns = clock_ns
        self._pin_modes: dict[int, str] = {}

    def _load(self) -> ModuleType:
        if self._gpio is None:
            self._gpio = self._gpio_loader()
            self._gpio.setwarnings(False)
            self._gpio.setmode(self._gpio.BCM)
            atexit.register(self._cleanup)
        return self._gpio

    def _setup_output(self, pin: int) -> ModuleType:
        gpio = self._load()
        if self._pin_modes.get(pin) != 'output':
            gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
            self._pin_modes[pin] = 'output'
        return gpio

    def _setup_input(self, pin: int) -> ModuleType:
        gpio = self._load()
        if self._pin_modes.get(pin) != 'input':
            gpio.setup(pin, gpio.IN, pull_up_down=gpio.PUD_OFF)
            self._pin_modes[pin] = 'input'
        return gpio

    def _cleanup(self) -> None:
        if self._gpio is not None and self._pin_modes:
            self._gpio.cleanup(sorted(self._pin_modes))
            self._pin_modes.clear()

    @staticmethod
    def _check_pin(pin: int, name: str) -> int:
        if not isinstance(pin, int) or isinstance(pin, bool) or not 0 <= pin <= 27:
            raise ValueError(f"{name} must be a BCM GPIO number from 2 to 27")
        if pin in RESERVED_PINS:
            raise ValueError(f"GPIO {pin} is used by {RESERVED_PINS[pin]}")
        return pin

    def _wait_until(self, deadline_ns: int) -> None:
        # Sleep for most of a long wait, then spin for accurate timing.
        remaining_ns = deadline_ns - self._clock_ns()
        if remaining_ns > 2_000_000:
            time.sleep((remaining_ns - 1_000_000) / 1e9)
        while self._clock_ns() < deadline_ns:
            pass

    @log_to_debug
    def stepper_move(
        self,
        step_pin: int,
        direction_pin: int,
        steps: int,
        *,
        direction: bool = True,
        step_delay_us: int = 1000,
    ) -> None:
        """
        Move a step/direction stepper driver by a number of steps.

        Generates one short STEP pulse per step, ``step_delay_us`` apart.
        This blocks until the move is finished. Timing is done in software, so
        there may be small variations, especially at high step rates.
        Acceleration and position tracking are not provided.

        :param step_pin: BCM GPIO number connected to the driver's STEP input.
        :param direction_pin: BCM GPIO number connected to the driver's DIR input.
        :param steps: Number of steps to move (1 to 1000000).
        :param direction: True drives DIR high; False drives DIR low.
        :param step_delay_us: Time between the start of each step, in
            microseconds (50 to 1000000).
        :raises ValueError: If the pins, step count, direction or timing are invalid.
        :raises RuntimeError: If not running on the robot.
        """
        self._check_pin(step_pin, "step_pin")
        self._check_pin(direction_pin, "direction_pin")
        if step_pin == direction_pin:
            raise ValueError("step_pin and direction_pin must be different")
        if (
            not isinstance(steps, int) or isinstance(steps, bool)
            or not 1 <= steps <= MAX_STEPS
        ):
            raise ValueError(f"steps must be an integer from 1 to {MAX_STEPS}")
        if not isinstance(direction, bool):
            raise ValueError("direction must be a bool")
        if (
            not isinstance(step_delay_us, int)
            or isinstance(step_delay_us, bool)
            or not MIN_STEP_DELAY_US <= step_delay_us <= MAX_STEP_DELAY_US
        ):
            raise ValueError(
                f"step_delay_us must be an integer from {MIN_STEP_DELAY_US} "
                f"to {MAX_STEP_DELAY_US}"
            )

        gpio = self._setup_output(step_pin)
        self._setup_output(direction_pin)

        gpio.output(direction_pin, gpio.HIGH if direction else gpio.LOW)
        next_step_ns = self._clock_ns() + DIRECTION_SETUP_US * 1000
        period_ns = step_delay_us * 1000
        for _ in range(steps):
            self._wait_until(next_step_ns)
            gpio.output(step_pin, gpio.HIGH)
            self._wait_until(next_step_ns + STEP_PULSE_US * 1000)
            gpio.output(step_pin, gpio.LOW)
            # Schedule from the planned time, not the actual one, so delays don't add up.
            next_step_ns += period_ns
        # Keep the step spacing if another move follows straight away.
        self._wait_until(next_step_ns)

    def _wait_for_level(
        self, gpio: ModuleType, pin: int, level: int, deadline_ns: int,
    ) -> bool:
        """Spin until the pin reads the given level; False if the deadline passes first."""
        while gpio.input(pin) != level:
            if self._clock_ns() > deadline_ns:
                return False
        return True

    @log_to_debug
    def ultrasound_measure(self, trigger_pin: int, echo_pin: int) -> int:
        """
        Measure the distance to an object using an HC-SR04 ultrasound sensor.

        The sensor can only measure distances up to about 4m. If no echo is
        detected, 0 is returned, as with the Arduino.

        The HC-SR04's echo output is 5V, so it must be connected to the Pi
        through a voltage divider (or use a 3.3V sensor such as the HC-SR04P).

        :param trigger_pin: BCM GPIO number connected to the sensor's TRIG input.
        :param echo_pin: BCM GPIO number connected to the sensor's ECHO output.
        :raises ValueError: If either of the pins are invalid.
        :raises RuntimeError: If not running on the robot.
        :return: The distance measured by the ultrasound sensor in mm.
        """
        self._check_pin(trigger_pin, "trigger_pin")
        self._check_pin(echo_pin, "echo_pin")
        if trigger_pin == echo_pin:
            raise ValueError("trigger_pin and echo_pin must be different")

        gpio = self._setup_output(trigger_pin)
        self._setup_input(echo_pin)
        timeout_ns = ULTRASOUND_TIMEOUT_US * 1000

        # Let any echo from a previous measurement finish first.
        if not self._wait_for_level(gpio, echo_pin, gpio.LOW, self._clock_ns() + timeout_ns):
            return 0

        gpio.output(trigger_pin, gpio.HIGH)
        self._wait_until(self._clock_ns() + ULTRASOUND_TRIGGER_US * 1000)
        gpio.output(trigger_pin, gpio.LOW)

        deadline_ns = self._clock_ns() + timeout_ns
        if not self._wait_for_level(gpio, echo_pin, gpio.HIGH, deadline_ns):
            return 0
        echo_start_ns = self._clock_ns()
        if not self._wait_for_level(gpio, echo_pin, gpio.LOW, deadline_ns):
            return 0
        echo_us = (self._clock_ns() - echo_start_ns) // 1000

        # Sound travels out and back at about 340 m/s (29us per cm), as in the
        # Arduino firmware, so both give the same distances.
        return 5 * echo_us // 29

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}>"
