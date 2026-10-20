"""
DC motors and stepper motors driven directly from the Raspberry Pi's or an Arduino's pins.

A DC motor uses a driver with two inputs, such as a DRV8871 (IN1 and IN2).
A stepper motor uses a driver with step and direction inputs, such as an A4988.
The boards create these objects: see ``RaspberryPi.dc_motor`` and ``Arduino.dc_motor``.
"""
from __future__ import annotations

import json
import logging
import math
import threading
from typing import Callable, Optional, Union

from .logging import log_to_debug
from .motor_board import MotorPower

logger = logging.getLogger(__name__)

# The Raspberry Pi pins DC motors are driving are published here (retained), so
# psc-statusd can stop the motors if the code is killed before it can stop them itself.
MOTOR_PINS_TOPIC = 'psc/motors'
_announcer: Optional[Callable[[str], None]] = None
_driven_pins: list[int] = []
_driven_lock = threading.Lock()


def set_announcer(announcer: Optional[Callable[[str], None]]) -> None:
    """Set the function that publishes the Pi pins DC motors drive, and announce none."""
    global _announcer
    with _driven_lock:
        _announcer = announcer
        _driven_pins.clear()
    _announce()


def register_driven_pins(pins: tuple[int, ...]) -> None:
    """Record Raspberry Pi pins that a DC motor drives."""
    with _driven_lock:
        _driven_pins.extend(pin for pin in pins if pin not in _driven_pins)
    _announce()


def _announce() -> None:
    with _driven_lock:
        announcer = _announcer
        message = json.dumps({"pi": sorted(_driven_pins)})
    if announcer is not None:
        try:
            announcer(message)
        except Exception:
            logger.debug("Failed to announce the pins driving DC motors", exc_info=True)


class DCMotor:
    """
    A DC motor connected through a two-input driver, such as a DRV8871.

    Power is set like the motor board's motors: from -1 (full speed reverse) to 1
    (full speed forwards), with ``BRAKE`` (0) to stop quickly and ``COAST`` to let the
    motor spin freely.

    :param set_inputs: Sets the driver's IN1 and IN2 to duty cycles from 0 to 1.
    :param description: Describes the motor's pins, for messages.
    """
    __slots__ = ('_set_inputs', '_description', '_power')

    def __init__(self, set_inputs: Callable[[float, float], None], description: str) -> None:
        self._set_inputs = set_inputs
        self._description = description
        self._power: float = MotorPower.COAST
        self._set_inputs(0.0, 0.0)

    @property
    @log_to_debug
    def power(self) -> float:
        """
        The power the motor was last set to.

        :return: The power, from -1 to 1, or ``COAST``.
        """
        return self._power

    @power.setter
    @log_to_debug(setter=True)
    def power(self, value: Union[float, MotorPower]) -> None:
        """
        Set the motor's power.

        :param value: From -1 (full reverse) to 1 (full forwards), ``BRAKE`` or ``COAST``.
        :raises ValueError: If the power is not a number from -1 to 1 or ``COAST``.
        """
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("power must be a number from -1 to 1, BRAKE or COAST")
        if value == MotorPower.COAST:
            self._set_inputs(0.0, 0.0)  # Both inputs low: the outputs are off
            self._power = MotorPower.COAST
            return
        if math.isnan(value) or not -1 <= value <= 1:
            raise ValueError("power must be a number from -1 to 1, BRAKE or COAST")
        # Slow decay: the off part of each PWM cycle brakes rather than coasts, so the
        # speed follows the power closely. Both inputs high (power 0) is brake.
        if value >= 0:
            self._set_inputs(1.0, 1.0 - value)
        else:
            self._set_inputs(1.0 + value, 1.0)
        self._power = float(value)

    def __repr__(self) -> str:
        power = "COAST" if self._power == MotorPower.COAST else self._power
        return f"<{self.__class__.__qualname__} {self._description} power={power}>"


class Stepper:
    """
    A stepper motor connected through a step/direction driver, such as an A4988.

    The motor counts the steps it has moved in ``position``. Moves wait until the
    motor has finished moving. Acceleration is not provided.

    :param move_steps: Moves the motor (steps, direction, step delay in microseconds).
    :param max_steps: The most steps ``move_steps`` can move at once, for a step delay.
    :param min_step_delay_us: The shortest supported step delay, in microseconds.
    :param max_step_delay_us: The longest supported step delay, in microseconds.
    :param step_delay_us: The time between steps used when a move doesn't give one.
    :param description: Describes the motor's pins, for messages.
    """
    __slots__ = (
        '_move_steps', '_max_steps', '_min_delay', '_max_delay',
        '_step_delay_us', '_position', '_description',
    )

    def __init__(
        self,
        move_steps: Callable[[int, bool, int], None],
        max_steps: Callable[[int], int],
        min_step_delay_us: int,
        max_step_delay_us: int,
        step_delay_us: int,
        description: str,
    ) -> None:
        self._move_steps = move_steps
        self._max_steps = max_steps
        self._min_delay = min_step_delay_us
        self._max_delay = max_step_delay_us
        self._description = description
        self._position = 0
        self._step_delay_us = self._check_delay(step_delay_us)

    def _check_delay(self, step_delay_us: int) -> int:
        if (
            not isinstance(step_delay_us, int)
            or isinstance(step_delay_us, bool)
            or not self._min_delay <= step_delay_us <= self._max_delay
        ):
            raise ValueError(
                f"step_delay_us must be an integer from {self._min_delay} to {self._max_delay}"
            )
        return step_delay_us

    @property
    def step_delay_us(self) -> int:
        """
        The time between steps, in microseconds, when a move doesn't give one.

        A longer delay is slower but gives more torque.
        """
        return self._step_delay_us

    @step_delay_us.setter
    def step_delay_us(self, value: int) -> None:
        self._step_delay_us = self._check_delay(value)

    @property
    @log_to_debug
    def position(self) -> int:
        """
        The number of steps moved since the motor was created or the position was set.

        Positive steps are counted up and negative steps down.
        """
        return self._position

    @position.setter
    @log_to_debug(setter=True)
    def position(self, value: int) -> None:
        """
        Set the current position, for example to 0 when the motor reaches a limit switch.

        :param value: The position, in steps.
        """
        if not isinstance(value, int) or isinstance(value, bool):
            raise ValueError("position must be an integer")
        self._position = value

    @log_to_debug
    def move(self, steps: int, *, step_delay_us: Optional[int] = None) -> None:
        """
        Move the motor by a number of steps, waiting until the move has finished.

        Positive steps drive the direction pin high, and negative steps drive it low.

        :param steps: The number of steps to move. Negative steps move the other way.
        :param step_delay_us: The time between steps, in microseconds. Defaults to
            ``step_delay_us``.
        :raises ValueError: If the steps or step delay are invalid.
        """
        if not isinstance(steps, int) or isinstance(steps, bool):
            raise ValueError("steps must be an integer")
        delay = (
            self._step_delay_us if step_delay_us is None
            else self._check_delay(step_delay_us)
        )
        direction = steps > 0
        remaining = abs(steps)
        chunk = self._max_steps(delay)
        while remaining:
            count = min(remaining, chunk)
            self._move_steps(count, direction, delay)
            self._position += count if direction else -count
            remaining -= count

    @log_to_debug
    def move_to(self, position: int, *, step_delay_us: Optional[int] = None) -> None:
        """
        Move the motor to a position, in steps, waiting until the move has finished.

        :param position: The position to move to.
        :param step_delay_us: The time between steps, in microseconds. Defaults to
            ``step_delay_us``.
        """
        if not isinstance(position, int) or isinstance(position, bool):
            raise ValueError("position must be an integer")
        self.move(position - self._position, step_delay_us=step_delay_us)

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} {self._description} position={self._position}>"
