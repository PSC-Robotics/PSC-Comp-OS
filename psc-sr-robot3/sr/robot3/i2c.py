"""Devices on I2C buses on the brain board's GPIO pins, including PCA9685 servo drivers."""
from __future__ import annotations

import atexit
import ctypes
import errno
import logging
import os
import sys
import threading
import time
from collections.abc import Iterable, Sequence
from typing import Any, Callable, Optional, Union

from .exceptions import I2CError
from .logging import log_to_debug
from .servo_board import DUTY_MAX, DUTY_MIN, START_DUTY_MAX, START_DUTY_MIN
from .utils import IN_SIMULATOR, float_bounds_check, map_to_int

LOGGER = logging.getLogger(__name__)

I2C_DEVICE = "/dev/i2c-1"
MIN_ADDRESS = 0x03  # Lower and higher addresses are reserved by the I2C standard.
MAX_ADDRESS = 0x77
MAX_TRANSFER = 4096

# From Linux's i2c-dev.h and i2c.h
I2C_RDWR = 0x0707
I2C_M_RD = 0x0001

# The errors Linux gives when nothing acknowledges an address. EREMOTEIO is given as
# a number because the errno module only has it on Linux, and this file also has to
# load in the simulator on other systems.
EREMOTEIO = 121
NO_REPLY_ERRORS = (errno.ENXIO, EREMOTEIO, errno.EIO)
WIRING_HELP = (
    "Check SDA is on GPIO 2 (header pin 3), SCL is on GPIO 3 (header pin 5), "
    "and the device has power and shares a ground with the Pi."
)

# Buses on other GPIO pins are driven in software, at up to 100kHz.
SOFTWARE_HALF_PERIOD_NS = 5_000
# How long a device may hold the clock low to make the Pi wait.
CLOCK_STRETCH_TIMEOUT_NS = 25_000_000

DEFAULT_SERVO_DRIVER_ADDRESS = 0x40
I2C_DEVICE_USER = "an I2C device"
SERVO_DRIVER_USER = "a servo driver"
NUM_DRIVER_SERVOS = 16
PCA9685_MODE1 = 0x00
PCA9685_LED0_ON_L = 0x06  # Each channel then has 4 registers: ON_L, ON_H, OFF_L and OFF_H.
PCA9685_ALL_LED_OFF_H = 0xFD
PCA9685_PRESCALE = 0xFE
PCA9685_SLEEP = 0x10
PCA9685_AUTO_INCREMENT = 0x20
PCA9685_FULL_OFF = 0x10  # In a channel's OFF_H register
PCA9685_OSCILLATOR_HZ = 25_000_000
PCA9685_STEPS = 4096
SERVO_HZ = 50
PCA9685_PRESCALE_VALUE = round(PCA9685_OSCILLATOR_HZ / (PCA9685_STEPS * SERVO_HZ)) - 1


class _Message(ctypes.Structure):
    """Linux's struct i2c_msg."""
    _fields_ = [
        ('addr', ctypes.c_uint16),
        ('flags', ctypes.c_uint16),
        ('len', ctypes.c_uint16),
        ('buf', ctypes.POINTER(ctypes.c_ubyte)),
    ]


class _Transfer(ctypes.Structure):
    """Linux's struct i2c_rdwr_ioctl_data."""
    _fields_ = [
        ('msgs', ctypes.POINTER(_Message)),
        ('nmsgs', ctypes.c_uint32),
    ]


def _load_ioctl() -> Callable[..., Any]:
    if sys.platform == "win32":
        raise OSError(errno.ENOSYS, "Hardware I2C needs Linux")
    import fcntl
    return fcntl.ioctl


def _check_byte(value: object, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255:
        raise ValueError(f"{name} must be a whole number from 0 to 255, got {value!r}")
    return value


def _to_bytes(data: Union[int, bytes, bytearray, Iterable[int]]) -> bytes:
    if isinstance(data, (bytes, bytearray)):
        return bytes(data)
    if isinstance(data, Iterable) and not isinstance(data, str):
        return bytes(_check_byte(value, "Each I2C data byte") for value in data)
    if isinstance(data, int) and not isinstance(data, bool):
        return bytes([_check_byte(data, "I2C data")])
    raise ValueError(
        f"I2C data must be bytes or a list of numbers from 0 to 255, got {data!r}")


def _check_length(length: object) -> int:
    if (
        not isinstance(length, int) or isinstance(length, bool)
        or not 1 <= length <= MAX_TRANSFER
    ):
        raise ValueError(f"I2C read length must be from 1 to {MAX_TRANSFER}, got {length!r}")
    return length


class I2CBus:
    """
    The Pi's hardware I2C bus, on GPIO 2 (SDA, header pin 3) and GPIO 3 (SCL, pin 5).

    :param path: The bus's device file.
    :param opener: Opens the device file; replaced in tests.
    :param ioctl: Sends a transfer to the device file; replaced in tests.
    """
    __slots__ = ('_path', '_opener', '_ioctl', '_fd', '_devices', '_users')

    def __init__(
        self,
        path: str = I2C_DEVICE,
        opener: Callable[[str, int], int] = os.open,
        ioctl: Optional[Callable[..., Any]] = None,
    ) -> None:
        self._path = path
        self._opener = opener
        self._ioctl = ioctl
        self._fd: Optional[int] = None
        self._devices: dict[int, I2CDevice] = {}
        self._users: dict[int, str] = {}

    @property
    def _wiring_help(self) -> str:
        return WIRING_HELP

    @property
    def _name(self) -> str:
        return "GPIO 2 and 3"

    def _open(self) -> int:
        if self._fd is not None:
            return self._fd
        if IN_SIMULATOR:
            raise I2CError("I2C is only available on the robot, not in the simulator.")
        if self._ioctl is None:
            try:
                self._ioctl = _load_ioctl()
            except ImportError as error:
                raise I2CError("I2C is only available on the robot.") from error
        try:
            self._fd = self._opener(self._path, os.O_RDWR)
        except FileNotFoundError as error:
            raise I2CError(
                f"I2C is turned off on this robot ({self._path} doesn't exist). Add "
                "dtparam=i2c_arm=on to /boot/config.txt and i2c-dev to /etc/modules, "
                "then reboot."
            ) from error
        except PermissionError as error:
            raise I2CError(
                f"Your code isn't allowed to use {self._path}. Run "
                "'sudo usermod -aG i2c robot' on the robot, then reboot."
            ) from error
        return self._fd

    def _raw_transfer(
        self, address: int, *messages: Union[bytes, int],
    ) -> list[bytes]:
        """
        Run writes (given as bytes) and reads (given as a length) without releasing
        the bus between them.
        """
        fd = self._open()
        buffers = []
        array = (_Message * len(messages))()
        for message, part in zip(array, messages):
            if isinstance(part, bytes):
                buffer = (ctypes.c_ubyte * len(part)).from_buffer_copy(part)
                message.flags, message.len = 0, len(part)
            else:
                buffer = (ctypes.c_ubyte * part)()
                message.flags, message.len = I2C_M_RD, part
            message.addr = address
            message.buf = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))
            buffers.append(buffer)
        transfer = _Transfer(ctypes.cast(array, ctypes.POINTER(_Message)), len(messages))
        assert self._ioctl is not None
        self._ioctl(fd, I2C_RDWR, transfer)
        return [
            bytes(buffer) for buffer, part in zip(buffers, messages)
            if not isinstance(part, bytes)
        ]

    def _transfer(self, address: int, *messages: Union[bytes, int]) -> list[bytes]:
        try:
            return self._raw_transfer(address, *messages)
        except I2CError:
            raise
        except OSError as error:
            raise self._explain(address, error) from error

    def _explain(self, address: int, error: OSError) -> I2CError:
        if error.errno not in NO_REPLY_ERRORS:
            return I2CError(
                f"The I2C bus failed while talking to {address:#04x}: {error.strerror}. "
                f"SDA or SCL may be shorted or loose. {self._wiring_help}"
            )
        try:
            found = self._find_devices()
        except OSError:
            found = []
        if found:
            others = ", ".join(f"{device:#04x}" for device in found)
            return I2CError(
                f"No I2C device replied at address {address:#04x}, but these did: {others}. "
                "Check the address you're using."
            )
        return I2CError(
            f"No I2C device replied at address {address:#04x}, and none were found on "
            f"the bus. {self._wiring_help}"
        )

    def _find_devices(self) -> list[int]:
        found = []
        for address in range(MIN_ADDRESS, MAX_ADDRESS + 1):
            try:
                self._raw_transfer(address, 1)
            except I2CError:
                raise
            except OSError as error:
                if error.errno not in NO_REPLY_ERRORS:
                    raise
            else:
                found.append(address)
        return found

    @log_to_debug
    def scan(self) -> list[int]:
        """
        Find the devices on the bus, by reading one byte from every address.

        :returns: The address of each device that replied.
        :raises I2CError: If I2C is unavailable or the bus has failed.
        """
        try:
            return self._find_devices()
        except I2CError:
            raise
        except OSError as error:
            raise I2CError(
                f"The I2C bus failed during a scan: {error.strerror}. {self._wiring_help}"
            ) from error

    @log_to_debug
    def device(self, address: int) -> I2CDevice:
        """
        Get the device at an address, found on its datasheet or with ``scan``.

        The bus isn't used until you read or write, so this doesn't check that the
        device is connected. Asking for the same address again returns the same device.

        :param address: The device's 7-bit address, from 0x03 to 0x77.
        :raises ValueError: If the address is invalid, or a servo driver on this bus
            is already using it.
        """
        return self._reserve(address, I2C_DEVICE_USER)

    def _reserve(self, address: int, user: str) -> I2CDevice:
        """Get the device at an address, for one kind of user of the bus."""
        if (
            not isinstance(address, int) or isinstance(address, bool)
            or not MIN_ADDRESS <= address <= MAX_ADDRESS
        ):
            raise ValueError(
                f"I2C address must be from {MIN_ADDRESS:#04x} to {MAX_ADDRESS:#04x}, "
                f"got {address!r}"
            )
        current = self._users.setdefault(address, user)
        if current != user:
            raise ValueError(
                f"I2C address {address:#04x} on {self._name} is already used by {current}. "
                "Devices on the same pins need different addresses: change this device's "
                "address, or connect it to two other pins and use "
                "robot.brain.i2c_bus(sda=..., scl=...)."
            )
        if address not in self._devices:
            self._devices[address] = I2CDevice(self, address)
        return self._devices[address]

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} {self._path}>"


class SoftwareI2CBus(I2CBus):
    """
    An I2C bus on any two free GPIO pins, driven in software.

    Get one with ``robot.brain.i2c_bus(sda, scl)``. It is slower than the Pi's hardware
    I2C, which doesn't matter to most devices. Addresses only have to differ within one
    bus.

    The lines are never driven high: a pin is released and pulled up to send a 1, which
    is how I2C lets several devices share a wire.

    :param gpio: The RPi.GPIO module, already in BCM mode.
    :param sda: The BCM GPIO number of the data pin.
    :param scl: The BCM GPIO number of the clock pin.
    :param clock_ns: A monotonic clock in nanoseconds; replaced in tests.
    """
    __slots__ = ('_gpio', '_sda', '_scl', '_clock_ns', '_lock')

    def __init__(
        self, gpio: Any, sda: int, scl: int,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        super().__init__()
        self._gpio = gpio
        self._sda = sda
        self._scl = scl
        self._clock_ns = clock_ns
        self._lock = threading.Lock()
        self._release(sda)
        self._release(scl)

    @property
    def _wiring_help(self) -> str:
        return (
            f"Check SDA is on GPIO {self._sda}, SCL is on GPIO {self._scl}, "
            "and the device has power and shares a ground with the Pi."
        )

    @property
    def _name(self) -> str:
        return f"the bus on GPIO {self._sda} and {self._scl}"

    def _release(self, pin: int) -> None:
        self._gpio.setup(pin, self._gpio.IN, pull_up_down=self._gpio.PUD_UP)

    def _drive_low(self, pin: int) -> None:
        self._gpio.setup(pin, self._gpio.OUT, initial=self._gpio.LOW)

    def _wait(self) -> None:
        deadline = self._clock_ns() + SOFTWARE_HALF_PERIOD_NS
        while self._clock_ns() < deadline:
            pass

    def _release_scl(self) -> None:
        self._release(self._scl)
        deadline = self._clock_ns() + CLOCK_STRETCH_TIMEOUT_NS
        while not self._gpio.input(self._scl):
            if self._clock_ns() > deadline:
                raise OSError(errno.ETIMEDOUT, "SCL is being held low")
        self._wait()

    def _clock_low(self) -> None:
        self._drive_low(self._scl)
        self._wait()

    def _set_sda(self, high: bool) -> None:
        if high:
            self._release(self._sda)
        else:
            self._drive_low(self._sda)
        self._wait()

    def _start(self) -> None:
        # Also works as a repeated start, between a write and a read.
        self._set_sda(True)
        self._release_scl()
        self._set_sda(False)
        self._clock_low()

    def _stop(self) -> None:
        self._set_sda(False)
        self._release_scl()
        self._set_sda(True)

    def _make_ready(self) -> None:
        self._set_sda(True)
        self._release_scl()
        if self._gpio.input(self._sda):
            return
        # A device stopped part way through a byte, such as when code was killed during
        # a transfer, keeps holding SDA low. Clocking it out of the byte frees it.
        for _ in range(9):
            self._clock_low()
            self._release_scl()
            if self._gpio.input(self._sda):
                break
        self._clock_low()
        self._stop()
        if not self._gpio.input(self._sda):
            raise OSError(errno.ETIMEDOUT, "SDA is being held low")

    def _write_byte(self, value: int) -> bool:
        for bit in range(7, -1, -1):
            self._set_sda(bool(value >> bit & 1))
            self._release_scl()
            self._clock_low()
        self._set_sda(True)
        self._release_scl()
        acknowledged = not self._gpio.input(self._sda)
        self._clock_low()
        return acknowledged

    def _read_byte(self, acknowledge: bool) -> int:
        self._set_sda(True)
        value = 0
        for _ in range(8):
            self._release_scl()
            value = value << 1 | (1 if self._gpio.input(self._sda) else 0)
            self._clock_low()
        self._set_sda(not acknowledge)
        self._release_scl()
        self._clock_low()
        return value

    def _raw_transfer(self, address: int, *messages: Union[bytes, int]) -> list[bytes]:
        no_reply = OSError(EREMOTEIO, "Remote I/O error")
        results = []
        with self._lock:
            self._make_ready()
            try:
                for part in messages:
                    self._start()
                    if isinstance(part, bytes):
                        if not self._write_byte(address << 1):
                            raise no_reply
                        for value in part:
                            if not self._write_byte(value):
                                raise no_reply
                    else:
                        if not self._write_byte(address << 1 | 1):
                            raise no_reply
                        results.append(bytes(
                            self._read_byte(index < part - 1) for index in range(part)))
            finally:
                self._stop()
        return results

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} SDA=GPIO {self._sda} SCL=GPIO {self._scl}>"


class I2CDevice:
    """
    A device on an I2C bus. Get one with ``bus.device(address)``.

    :param bus: The bus the device is on.
    :param address: The device's 7-bit address.
    """
    __slots__ = ('_bus', '_address')

    def __init__(self, bus: I2CBus, address: int) -> None:
        self._bus = bus
        self._address = address

    @property
    def address(self) -> int:
        """The device's 7-bit address."""
        return self._address

    @log_to_debug
    def write(self, register: int, data: Union[int, bytes, Iterable[int]]) -> None:
        """
        Write to a register, and to the ones after it if there are several bytes.

        :param register: The register number, from 0 to 255.
        :param data: A number from 0 to 255, or a list of them, or bytes.
        :raises ValueError: If the register or data is invalid.
        :raises I2CError: If the device doesn't reply.
        """
        _check_byte(register, "I2C register")
        self._bus._transfer(self._address, bytes([register]) + _to_bytes(data))

    @log_to_debug
    def read(self, register: int, length: int = 1) -> bytes:
        """
        Read from a register, and from the ones after it if ``length`` is more than 1.

        :param register: The register number, from 0 to 255.
        :param length: How many bytes to read.
        :returns: The bytes read. ``data[0]`` is the first, as a number from 0 to 255.
        :raises ValueError: If the register or length is invalid.
        :raises I2CError: If the device doesn't reply.
        """
        _check_byte(register, "I2C register")
        _check_length(length)
        return self._bus._transfer(self._address, bytes([register]), length)[0]

    @log_to_debug
    def send(self, data: Union[int, bytes, Iterable[int]]) -> None:
        """
        Write bytes directly, for devices that don't use registers.

        :param data: A number from 0 to 255, or a list of them, or bytes.
        :raises ValueError: If the data is invalid or empty.
        :raises I2CError: If the device doesn't reply.
        """
        message = _to_bytes(data)
        if not message:
            raise ValueError("I2C data must have at least one byte")
        self._bus._transfer(self._address, message)

    @log_to_debug
    def receive(self, length: int = 1) -> bytes:
        """
        Read bytes directly, for devices that don't use registers.

        :param length: How many bytes to read.
        :returns: The bytes read.
        :raises ValueError: If the length is invalid.
        :raises I2CError: If the device doesn't reply.
        """
        _check_length(length)
        return self._bus._transfer(self._address, length)[0]

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} address={self._address:#04x}>"


class DriverServo:
    """
    A servo on a port of a PCA9685 servo driver, with the same controls as a servo
    on Student Robotics' servo board.

    :param driver: The driver the servo is plugged into.
    :param port: The port number, from 0 to 15.
    """
    __slots__ = ('_driver', '_port', '_duty_min', '_duty_max', '_position')

    def __init__(self, driver: ServoDriver, port: int) -> None:
        self._driver = driver
        self._port = port
        self._duty_min = START_DUTY_MIN
        self._duty_max = START_DUTY_MAX
        self._position: Optional[float] = None

    @log_to_debug
    def set_duty_limits(self, lower: int, upper: int) -> None:
        """
        Set the pulse lengths of positions -1 and 1. Used from the next move.

        :param lower: The pulse at position -1, in µs.
        :param upper: The pulse at position 1, in µs.
        :raises TypeError: If the limits are not ints.
        :raises ValueError: If the limits are not in the range 500 to 4000.
        """
        message = f'Servo pulse limits are ints in µs, in the range {DUTY_MIN} to {DUTY_MAX}'
        if not (isinstance(lower, int) and isinstance(upper, int)):
            raise TypeError(message)
        if not (DUTY_MIN <= lower <= DUTY_MAX and DUTY_MIN <= upper <= DUTY_MAX):
            raise ValueError(message)
        self._duty_min = lower
        self._duty_max = upper

    @log_to_debug
    def get_duty_limits(self) -> tuple[int, int]:
        """
        Get the pulse lengths of positions -1 and 1.

        :returns: The lower and upper limits, in µs.
        """
        return self._duty_min, self._duty_max

    @property
    @log_to_debug
    def position(self) -> Optional[float]:
        """
        The last position set, from -1.0 to 1.0, or None if the servo is disabled.

        Set this to move the servo, or to None to disable it. The driver can't sense
        where a servo really is, so this is where it was told to go.
        """
        return self._position

    @position.setter
    @log_to_debug(setter=True)
    def position(self, value: Optional[float]) -> None:
        if value is None:
            self.disable()
            return
        value = float_bounds_check(
            value, -1.0, 1.0, 'Servo position is a float between -1.0 and 1.0')
        pulse_us = map_to_int(value, -1.0, 1.0, self._duty_min, self._duty_max)
        self._driver._set_pulse(self._port, pulse_us)
        self._position = value

    @log_to_debug
    def disable(self) -> None:
        """Stop sending pulses, so the servo goes limp or a continuous one stops."""
        self._driver._disable(self._port)
        self._position = None

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} port={self._port} {self._driver}>"


class ServoPorts(Sequence):
    """A servo driver's ports, which say which ports exist if you ask for another."""
    __slots__ = ('_servos',)

    def __init__(self, servos: tuple[DriverServo, ...]) -> None:
        self._servos = servos

    def __getitem__(self, port: Any) -> Any:
        try:
            return self._servos[port]
        except IndexError:
            raise IndexError(
                f"Servo port {port} doesn't exist: the ports are 0 to {len(self._servos) - 1}"
            ) from None

    def __len__(self) -> int:
        return len(self._servos)


class ServoDriver:
    """
    A PCA9685 16-channel servo driver on an I2C bus.

    The driver is set up the first time a servo moves, so nothing happens on the bus
    until then. Every servo stops when your code ends.

    :param device: The driver's I2C device.
    :param sleep: Waits a number of seconds; replaced in tests.
    """
    __slots__ = ('_device', '_sleep', '_started', '_servos')

    def __init__(self, device: I2CDevice, sleep: Callable[[float], None] = time.sleep) -> None:
        self._device = device
        self._sleep = sleep
        self._started = False
        self._servos = ServoPorts(
            tuple(DriverServo(self, port) for port in range(NUM_DRIVER_SERVOS)))

    @property
    def servos(self) -> ServoPorts:
        """The servos, by the port number printed on the driver, from 0 to 15."""
        return self._servos

    def _start(self) -> None:
        if self._started:
            return
        device = self._device
        try:
            # The frequency can only be set while asleep.
            device.write(PCA9685_MODE1, PCA9685_SLEEP)
            device.write(PCA9685_ALL_LED_OFF_H, PCA9685_FULL_OFF)
            device.write(PCA9685_PRESCALE, PCA9685_PRESCALE_VALUE)
            prescale = device.read(PCA9685_PRESCALE)[0]
        except I2CError as error:
            raise I2CError(f"Can't find the servo driver: {error}") from error
        if prescale != PCA9685_PRESCALE_VALUE:
            raise I2CError(
                f"The device at I2C address {device.address:#04x} isn't a PCA9685 servo "
                "driver. Check the address, and that no other device is using it."
            )
        device.write(PCA9685_MODE1, PCA9685_AUTO_INCREMENT)
        self._sleep(0.001)  # The oscillator takes up to 500µs to start.
        self._started = True
        atexit.register(self._stop)
        LOGGER.info(f"Servo driver found at I2C address {device.address:#04x}")

    def _stop(self) -> None:
        try:
            self._device.write(PCA9685_ALL_LED_OFF_H, PCA9685_FULL_OFF)
        except OSError:
            LOGGER.debug("Failed to stop the servos", exc_info=True)

    def _set_pulse(self, port: int, pulse_us: int) -> None:
        self._start()
        ticks_per_us = PCA9685_OSCILLATOR_HZ / (PCA9685_PRESCALE_VALUE + 1) / 1_000_000
        off = min(round(pulse_us * ticks_per_us), PCA9685_STEPS - 1)
        self._device.write(
            PCA9685_LED0_ON_L + 4 * port, [0, 0, off & 0xFF, off >> 8])

    def _disable(self, port: int) -> None:
        self._start()
        self._device.write(PCA9685_LED0_ON_L + 4 * port, [0, 0, 0, PCA9685_FULL_OFF])

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__} address={self._device.address:#04x}>"
