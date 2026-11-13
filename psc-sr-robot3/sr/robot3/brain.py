"""The brain board: its LED, and devices wired directly to its GPIO pins."""
from __future__ import annotations

import atexit
import logging
import sys
import time
from pathlib import Path
from types import ModuleType
from typing import Callable, Optional

from .arduino import GPIOPinMode
from .battery import BrainBatterySensor
from .i2c import (
    DEFAULT_SERVO_DRIVER_ADDRESS, SERVO_DRIVER_USER,
    I2CBus, ServoDriver, SoftwareI2CBus,
)
from .leds import STATUS_CONFIG, BrainLED
from .logging import log_to_debug
from .pin_motors import DCMotor, Stepper, register_driven_pins

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

LOGGER = logging.getLogger(__name__)

# Pins the robot always uses: the HAT EEPROM (0, 1), I2C (2, 3) and the serial port for
# bus servos (14, 15). The rest come from the status service's config: see reserved_pins.
# Each says what uses the pin, and what to use instead where robot code can use it.
I2C_HINT = "use robot.brain.i2c_bus(sda=2, scl=3)"
FIXED_RESERVED_PINS = {0: "the HAT's EEPROM", 1: "the HAT's EEPROM",
                       2: f"I2C (SDA), with the battery sensor: {I2C_HINT}",
                       3: f"I2C (SCL), with the battery sensor: {I2C_HINT}",
                       14: "the serial port (bus servos)", 15: "the serial port (bus servos)"}
# Besides MOSI (GPIO 10), the SPI port also takes these pins.
SPI_PINS = (7, 8, 9, 11)
MIN_STEP_DELAY_US = 50
DEFAULT_STEP_DELAY_US = 1000
MAX_STEP_DELAY_US = 1_000_000
MAX_STEPS = 1_000_000
STEP_PULSE_US = 5  # comfortably above the 1-2us minimum of common drivers
DIRECTION_SETUP_US = 5
# Software PWM frequency for DC motor drivers. The DRV8871 accepts up to 200kHz.
DC_MOTOR_PWM_HZ = 1000


def reserved_pins(config_file: Path = STATUS_CONFIG) -> dict[int, str]:
    """
    The pins the robot itself uses, so robot code can't, and what uses each, from
    the status service's config: the start button, the buzzer and the LEDs' data
    pins (with the rest of the SPI port if GPIO 10 is one). Missing
    settings take the service's defaults.
    """
    try:
        config = tomllib.loads(config_file.read_text())
    except (OSError, ValueError):
        config = {}

    def section(name: str) -> dict:
        value = config.get(name, {})
        return value if isinstance(value, dict) else {}

    pins = dict(FIXED_RESERVED_PINS)

    def reserve(pin: object, user: str) -> None:
        if isinstance(pin, int) and not isinstance(pin, bool) and 0 <= pin <= 27:
            pins[pin] = user

    reserve(section('start_button').get('pin', 5), "the start button: use robot.wait_start()")
    reserve(section('buzzer').get('pin', 13), "the buzzer: use robot.buzzer.buzz()")
    strip = section('led_strip')
    if strip.get('enabled', True) is True and strip.get('count', 2) != 0:
        strip_pins = strip.get('pins', [18])
        for pin in strip_pins if isinstance(strip_pins, list) else []:
            reserve(pin, "the LEDs (WS2812 data): use robot.brain.led")
            if pin == 10:
                for spi_pin in SPI_PINS:
                    reserve(spi_pin, "the LEDs' SPI port")
    return pins


def _load_gpio() -> ModuleType:
    try:
        import RPi.GPIO as GPIO  # isort: ignore
    except (ImportError, RuntimeError) as error:
        raise RuntimeError(
            "The brain board's GPIO pins are only available when running on the robot"
        ) from error
    return GPIO


DIGITAL_READ_MODES = (GPIOPinMode.INPUT, GPIOPinMode.INPUT_PULLUP, GPIOPinMode.OUTPUT)


class BrainPin:
    """
    One of the brain board's GPIO pins, used like an Arduino's pins:
    ``robot.brain.pins[17].mode = OUTPUT``, then ``digital_write(True)`` to set it to
    3.3V. Or set the mode to ``INPUT`` or ``INPUT_PULLUP`` and use ``digital_read()``.

    A pin starts as an input. Pins set as outputs go back to being inputs when your
    code ends, so nothing is left driven.
    """
    __slots__ = ('_brain', '_pin')

    def __init__(self, brain: Brain, pin: int) -> None:
        self._brain = brain
        self._pin = pin

    @property
    @log_to_debug
    def mode(self) -> GPIOPinMode:
        """The pin's mode: ``INPUT`` (the default), ``INPUT_PULLUP`` or ``OUTPUT``."""
        return self._brain._gpio_modes.get(self._pin, GPIOPinMode.INPUT)

    @mode.setter
    @log_to_debug(setter=True)
    def mode(self, value: GPIOPinMode) -> None:
        """
        Set the pin's mode.

        :param value: ``INPUT``, ``INPUT_PULLUP`` (pulled up to 3.3V, for a button
            wired to GND) or ``OUTPUT`` (starts low).
        :raises IOError: If the mode isn't a GPIOPinMode.
        :raises ValueError: If the pin is already used by a motor or I2C bus.
        :raises RuntimeError: If not running on the robot.
        """
        if not isinstance(value, GPIOPinMode):
            raise IOError("Pin mode only supports being set to a GPIOPinMode")
        self._brain._set_pin_mode(self._pin, value)

    @log_to_debug
    def digital_read(self) -> bool:
        """
        Read the pin: True if it's high (3.3V), False if it's low (0V). An output
        reads back the level it's set to.

        :raises ValueError: If the pin is already used by a motor or I2C bus.
        :raises RuntimeError: If not running on the robot.
        """
        if self._pin not in self._brain._gpio_modes:
            self._brain._set_pin_mode(self._pin, GPIOPinMode.INPUT)
        return bool(self._brain._load().input(self._pin))

    @log_to_debug
    def digital_write(self, value: bool) -> None:
        """
        Set the pin high (True, 3.3V) or low (False, 0V).

        :param value: True for high, False for low.
        :raises IOError: If the pin isn't an output.
        """
        if self.mode != GPIOPinMode.OUTPUT:
            raise IOError(
                f"Digital write is not supported in {self.mode.value}: set "
                f"robot.brain.pins[{self._pin}].mode = OUTPUT first")
        gpio = self._brain._load()
        gpio.output(self._pin, gpio.HIGH if value else gpio.LOW)

    def __repr__(self) -> str:
        # Not self.mode: log_to_debug logs this repr, so reading a logged property loops
        mode = self._brain._gpio_modes.get(self._pin, GPIOPinMode.INPUT)
        return f"<{self.__class__.__qualname__} GPIO {self._pin} mode={mode.value}>"


class BrainPins:
    """
    The brain board's GPIO pins, by BCM number: ``robot.brain.pins[17]``.

    Pins the robot uses itself can't be used: the start button, the buzzer and the
    LEDs' data pin (use ``robot.wait_start()``, ``robot.buzzer`` and
    ``robot.brain.led``), the HAT's EEPROM (0, 1), I2C with the battery sensor
    (2, 3) and the bus servos' serial port (14, 15). Asking for one raises a
    ValueError saying what uses it. Every other pin from 4 to 27 can be used.
    """
    __slots__ = ('_brain', '_pins')

    def __init__(self, brain: Brain) -> None:
        self._brain = brain
        self._pins: dict[int, BrainPin] = {}

    def __getitem__(self, pin: int) -> BrainPin:
        """
        :raises ValueError: If the pin isn't a GPIO number from 4 to 27, or the robot
            uses it.
        """
        self._brain._check_pin(pin, "pin")
        if pin not in self._pins:
            self._pins[pin] = BrainPin(self._brain, pin)
        return self._pins[pin]

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}>"


class Brain:
    """
    The brain board: its LED, and devices wired directly to its GPIO pins.

    Supports setting and reading GPIO pins directly (``pins``), DC motor drivers (such
    as the DRV8871), stepper motor drivers, I2C devices and PCA9685 servo drivers.
    Pins are given as BCM GPIO numbers (e.g. 23 for GPIO 23, header pin 16),
    not header pin numbers.

    :param gpio_loader: Returns the RPi.GPIO module; replaced in tests.
    :param clock_ns: A monotonic clock in nanoseconds; replaced in tests.
    :param led: The LED your code controls.
    :param battery_sensor: The HAT's battery sensor.
    :param reserved: Pins robot code can't use, and what uses each; read from the
        status service's config if not given.
    :param i2c: The I2C bus; replaced in tests.
    """
    __slots__ = (
        '_gpio', '_gpio_loader', '_clock_ns', '_pin_modes', '_pwms', '_motors', '_claims',
        '_led', '_battery_sensor', '_reserved', '_i2c', '_i2c_buses', '_servo_drivers',
        '_gpio_modes', '_pins',
    )

    def __init__(
        self,
        gpio_loader: Callable[[], ModuleType] = _load_gpio,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
        led: Optional[BrainLED] = None,
        battery_sensor: Optional[BrainBatterySensor] = None,
        reserved: Optional[dict[int, str]] = None,
        i2c: Optional[I2CBus] = None,
    ) -> None:
        self._led = BrainLED(None) if led is None else led
        self._battery_sensor = (
            BrainBatterySensor(None) if battery_sensor is None else battery_sensor)
        self._i2c = I2CBus() if i2c is None else i2c
        self._i2c_buses: dict[tuple, SoftwareI2CBus] = {}
        self._servo_drivers: dict[tuple[I2CBus, int], ServoDriver] = {}
        self._reserved = reserved_pins() if reserved is None else reserved
        self._gpio: Optional[ModuleType] = None
        self._gpio_loader = gpio_loader
        self._clock_ns = clock_ns
        self._pin_modes: dict[int, str] = {}
        self._pwms: dict[int, object] = {}
        # Motors by kind and pins, and which motor uses each pin.
        self._motors: dict[tuple, object] = {}
        self._claims: dict[int, tuple] = {}
        # Modes of pins set through robot.brain.pins
        self._gpio_modes: dict[int, GPIOPinMode] = {}
        self._pins = BrainPins(self)

    @property
    def led(self) -> BrainLED:
        """
        The LED your code controls. Set it with
        ``robot.brain.led.colour = Colour.RED``.
        """
        return self._led

    @property
    def pins(self) -> BrainPins:
        """
        The GPIO pins, to set and read directly: ``robot.brain.pins[17]``. Pins the
        robot uses itself (the start button, buzzer and LEDs) can't be used.
        """
        return self._pins

    def _set_pin_mode(self, pin: int, mode: GPIOPinMode) -> None:
        self._claim(('pin', pin), (pin,), ('pin',))
        gpio = self._load()
        if mode == GPIOPinMode.OUTPUT:
            # Low until the code sets it, and back to an input when the code ends,
            # even if it's killed (the status service does that for these pins).
            gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
            register_driven_pins((pin,))
        else:
            pull = gpio.PUD_UP if mode == GPIOPinMode.INPUT_PULLUP else gpio.PUD_OFF
            gpio.setup(pin, gpio.IN, pull_up_down=pull)
        self._pin_modes[pin] = mode.value.lower()
        self._gpio_modes[pin] = mode

    @property
    def battery_sensor(self) -> BrainBatterySensor:
        """
        The HAT's battery sensor. Read the voltage with
        ``robot.brain.battery_sensor.voltage``.
        """
        return self._battery_sensor

    @log_to_debug
    def servo_driver(
        self,
        *,
        sda: int,
        scl: int,
        address: int = DEFAULT_SERVO_DRIVER_ADDRESS,
    ) -> ServoDriver:
        """
        Get a PCA9685 servo driver on two GPIO pins.

        This uses the I2C bus on those pins, creating it if needed, so other I2C devices
        can share the pins through ``i2c_bus``.

        :param sda: The BCM GPIO number connected to the driver's SDA pin.
        :param scl: The BCM GPIO number connected to the driver's SCL pin.
        :param address: The driver's I2C address.
        :raises ValueError: If the address or a pin is invalid, or the address is
            already used by another device on those pins.
        """
        bus = self.i2c_bus(sda, scl)
        key = (bus, address)
        if key not in self._servo_drivers:
            self._servo_drivers[key] = ServoDriver(bus._reserve(address, SERVO_DRIVER_USER))
        return self._servo_drivers[key]

    @log_to_debug
    def i2c_bus(self, sda: int, scl: int) -> I2CBus:
        """
        Get the I2C bus on two free GPIO pins, creating it if needed. Asking for the
        same pins again, including pins a servo driver uses, returns the same bus.

        :param sda: The BCM GPIO number of the data pin.
        :param scl: The BCM GPIO number of the clock pin.
        :raises ValueError: If a pin is invalid, reserved, or used by another motor or bus.
        :raises RuntimeError: If not running on the robot.
        """
        if (sda, scl) == (2, 3):
            return self._i2c
        self._check_pin(sda, "sda")
        self._check_pin(scl, "scl")
        key = ('i2c', sda, scl)
        if key not in self._i2c_buses:
            self._claim(key, (sda, scl), ('sda', 'scl'))
            gpio = self._load()
            self._i2c_buses[key] = SoftwareI2CBus(gpio, sda, scl)
            self._pin_modes[sda] = self._pin_modes[scl] = 'i2c'
        return self._i2c_buses[key]

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

    def _cleanup(self) -> None:
        for pwm in self._pwms.values():
            try:
                pwm.stop()  # type: ignore[attr-defined]
            except Exception:
                LOGGER.debug("Failed to stop PWM", exc_info=True)
        self._pwms.clear()
        if self._gpio is not None and self._pin_modes:
            self._gpio.cleanup(sorted(self._pin_modes))
            self._pin_modes.clear()

    def _check_pin(self, pin: int, name: str) -> int:
        if not isinstance(pin, int) or isinstance(pin, bool) or not 0 <= pin <= 27:
            raise ValueError(f"{name} must be a BCM GPIO number from 4 to 27")
        if pin in self._reserved:
            raise ValueError(f"GPIO {pin} is used by {self._reserved[pin]}")
        return pin

    def _wait_until(self, deadline_ns: int) -> None:
        # Sleep for most of a long wait, then spin for accurate timing.
        remaining_ns = deadline_ns - self._clock_ns()
        if remaining_ns > 2_000_000:
            time.sleep((remaining_ns - 1_000_000) / 1e9)
        while self._clock_ns() < deadline_ns:
            pass

    @staticmethod
    def _describe(owner: tuple) -> str:
        if owner[0] == 'i2c':
            return f"an I2C bus (SDA=GPIO {owner[1]}, SCL=GPIO {owner[2]})"
        if owner[0] == 'pin':
            return f"robot.brain.pins[{owner[1]}]"
        return "another motor"

    def _claim(self, key: tuple, pins: tuple[int, ...], names: tuple[str, ...]) -> None:
        for pin, name in zip(pins, names):
            self._check_pin(pin, name)
        if len(set(pins)) != len(pins):
            raise ValueError(f"{' and '.join(names)} must be different")
        for pin in pins:
            owner = self._claims.get(pin)
            if owner is not None and owner != key:
                raise ValueError(f"GPIO {pin} is already used by {self._describe(owner)}")
        for pin in pins:
            self._claims[pin] = key

    def _set_duty(self, pin: int, duty: float) -> None:
        gpio = self._setup_output(pin)
        pwm = self._pwms.get(pin)
        if pwm is None:
            pwm = gpio.PWM(pin, DC_MOTOR_PWM_HZ)
            pwm.start(0)
            self._pwms[pin] = pwm
        pwm.ChangeDutyCycle(duty * 100)  # type: ignore[attr-defined]

    @log_to_debug
    def dc_motor(self, in1_pin: int, in2_pin: int) -> DCMotor:
        """
        Get a DC motor connected through a two-input driver, such as a DRV8871.

        Set its ``power`` from -1 to 1, or to ``BRAKE`` or ``COAST``. Asking for the
        same pins again returns the same motor. The motor starts coasting, and is
        stopped when your code ends.

        :param in1_pin: BCM GPIO number connected to the driver's IN1 input.
        :param in2_pin: BCM GPIO number connected to the driver's IN2 input.
        :raises ValueError: If a pin is invalid, reserved or used by another motor.
        :raises RuntimeError: If not running on the robot.
        """
        key = ('dc', in1_pin, in2_pin)
        if key not in self._motors:
            self._claim(key, (in1_pin, in2_pin), ('in1_pin', 'in2_pin'))
            self._load()

            def set_inputs(in1: float, in2: float) -> None:
                self._set_duty(in1_pin, in1)
                self._set_duty(in2_pin, in2)

            self._motors[key] = DCMotor(set_inputs, f"IN1=GPIO {in1_pin} IN2=GPIO {in2_pin}")
            register_driven_pins((in1_pin, in2_pin))
        return self._motors[key]  # type: ignore[return-value]

    @log_to_debug
    def stepper(
        self, step_pin: int, direction_pin: int, *, step_delay_us: Optional[int] = None,
    ) -> Stepper:
        """
        Get a stepper motor connected through a step/direction driver, such as an A4988.

        Move it with ``move(steps)``; it counts its ``position`` in steps. Asking for
        the same pins again returns the same motor.

        :param step_pin: BCM GPIO number connected to the driver's STEP input.
        :param direction_pin: BCM GPIO number connected to the driver's DIR input.
        :param step_delay_us: The time between steps, in microseconds (50 to 1000000),
            when a move doesn't give one. Defaults to 1000 for a new motor.
        :raises ValueError: If a pin is invalid, reserved or used by another motor.
        """
        key = ('stepper', step_pin, direction_pin)
        if key not in self._motors:
            self._claim(key, (step_pin, direction_pin), ('step_pin', 'direction_pin'))

            def move_steps(steps: int, direction: bool, delay: int) -> None:
                self._step(step_pin, direction_pin, steps, direction, delay)

            self._motors[key] = Stepper(
                move_steps, lambda _delay: MAX_STEPS, MIN_STEP_DELAY_US, MAX_STEP_DELAY_US,
                DEFAULT_STEP_DELAY_US, f"STEP=GPIO {step_pin} DIR=GPIO {direction_pin}",
            )
        motor = self._motors[key]
        assert isinstance(motor, Stepper)
        if step_delay_us is not None:
            motor.step_delay_us = step_delay_us
        return motor

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

        for pin in (step_pin, direction_pin):
            owner = self._claims.get(pin)
            if owner is not None and owner[0] != 'stepper':
                raise ValueError(f"GPIO {pin} is already used by {self._describe(owner)}")
        self._step(step_pin, direction_pin, steps, direction, step_delay_us)

    def _step(
        self, step_pin: int, direction_pin: int, steps: int, direction: bool,
        step_delay_us: int,
    ) -> None:
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

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}>"
