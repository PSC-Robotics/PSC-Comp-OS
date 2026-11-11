"""
Serial bus servos (Waveshare ST/SC series, Feetech STS/SCS) on a bus servo adapter.

Bus servos share one serial line, and each has an ID. They are driven through
a bus servo adapter, such as the Waveshare Bus Servo Adapter (A), connected to
the brain board by USB, or to the Raspberry Pi's serial port (GPIO 14 and 15).

These are not PWM servos (such as the SG90 or MG996R), which can't be used on
a bus servo adapter.
"""
from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Iterable, Optional

import serial
from serial.tools.list_ports import comports

from .utils import BoardIdentity, get_USB_identity

LOGGER = logging.getLogger(__name__)

# USB IDs of bus servo adapters. (The Waveshare Bus Servo Adapter (A) uses a CH343.)
SUPPORTED_VID_PIDS = {
    (0x1A86, 0x55D3),  # Waveshare Bus Servo Adapter (A) (WCH CH343)
}
BAUDRATE = 1_000_000  # the servos' default
GPIO_UART = Path("/dev/serial0")
SCAN_IDS = range(1, 21)

INST_PING = 0x01
INST_READ = 0x02
INST_WRITE = 0x03
BROADCAST_ID = 0xFE

# Register addresses, from Feetech's SDK (SMS_STS and SCSCL).
ADDR_MODEL = 3
ADDR_ID = 5
ADDR_TORQUE_ENABLE = 40
ADDR_ACCELERATION = 41  # STS only
ADDR_GOAL_POSITION = 42
ADDR_GOAL_SPEED = 46
ADDR_PRESENT_POSITION = 56
ADDR_PRESENT_SPEED = 58
ADDR_PRESENT_LOAD = 60
ADDR_PRESENT_VOLTAGE = 62
ADDR_PRESENT_TEMPERATURE = 63
ADDR_MOVING = 66
ADDR_LOCK = {"sts": 55, "scs": 48}

# Positions per revolution and the angle they cover, per series.
SERIES = {
    "sts": {"max_position": 4095, "degrees": 360.0, "little_endian": True},
    "scs": {"max_position": 1023, "degrees": 300.0, "little_endian": False},
}


class BusServoError(IOError):
    """A bus servo didn't reply, or replied with an error."""


def _checksum(data: bytes) -> int:
    return (~sum(data)) & 0xFF


def _packet(servo_id: int, instruction: int, params: bytes = b"") -> bytes:
    body = bytes([servo_id, len(params) + 2, instruction]) + params
    return b"\xff\xff" + body + bytes([_checksum(body)])


class BusServoAdapter:
    """
    A bus servo adapter, and the servos connected to it.

    :param port: The serial port the adapter is connected to.
    :param identity: How the adapter is identified in logs and ``robot.bus_servo_adapters``.
    :param serial_factory: Opens the serial port; replaced in tests.
    """
    __slots__ = ('_serial', '_lock', '_servos', '_identity', '_timeout')

    def __init__(
        self,
        port: str,
        identity: Optional[BoardIdentity] = None,
        serial_factory: Callable[..., serial.Serial] = serial.Serial,
        timeout: float = 0.05,
    ) -> None:
        self._serial = serial_factory(port, BAUDRATE, timeout=timeout)
        self._lock = threading.Lock()
        self._servos: dict[int, BusServo] = {}
        self._timeout = timeout
        self._identity = identity or BoardIdentity(
            manufacturer="Waveshare", board_type="BusServoAdapter", asset_tag=port)

    @staticmethod
    def get_board_type() -> str:
        """
        Return the type of the board.

        :return: The literal string 'BusServoAdapter'.
        """
        return 'BusServoAdapter'

    @classmethod
    def _get_supported_boards(
        cls,
        manual_ports: Optional[list[str]] = None,
        include_gpio_uart: bool = True,
    ) -> MappingProxyType[str, BusServoAdapter]:
        """
        Find bus servo adapters connected by USB, or on the Pi's serial port.

        The Pi's serial port is only used if a servo answers on it, as an
        adapter there can't be detected otherwise.
        """
        adapters: dict[str, BusServoAdapter] = {}
        for port in comports():
            if (port.vid, port.pid) not in SUPPORTED_VID_PIDS:
                continue
            identity = get_USB_identity(port)
            name = identity.asset_tag or port.device
            try:
                adapters[name] = cls(port.device, BoardIdentity(
                    manufacturer="Waveshare", board_type="BusServoAdapter", asset_tag=name))
            except (OSError, serial.SerialException) as error:
                LOGGER.warning(f"Couldn't open bus servo adapter at {port.device}: {error}")
        for port_name in manual_ports or []:
            try:
                adapters[port_name] = cls(port_name)
            except (OSError, serial.SerialException) as error:
                LOGGER.warning(f"Couldn't open bus servo adapter at {port_name}: {error}")
        if include_gpio_uart and GPIO_UART.exists():
            try:
                adapter = cls(str(GPIO_UART), BoardIdentity(
                    manufacturer="Waveshare", board_type="BusServoAdapter", asset_tag="gpio"),
                    timeout=0.01)
            except (OSError, serial.SerialException):
                adapter = None
            if adapter is not None:
                if adapter.scan():
                    adapters["gpio"] = adapter
                else:
                    adapter.close()
        return MappingProxyType(adapters)

    def identify(self) -> BoardIdentity:
        """
        Get the identity of the adapter.

        :return: The identity of the adapter.
        """
        return self._identity

    def close(self) -> None:
        """Close the serial port."""
        self._serial.close()

    def _transact(self, servo_id: int, instruction: int, params: bytes = b"") -> bytes:
        """Send an instruction and return the servo's reply parameters."""
        request = _packet(servo_id, instruction, params)
        with self._lock:
            self._serial.reset_input_buffer()
            self._serial.write(request)
            self._serial.flush()
            if servo_id == BROADCAST_ID:
                return b""
            reply = self._read_reply(request)
        if reply[2] != servo_id:
            raise BusServoError(f"Reply from servo {reply[2]}, expected servo {servo_id}")
        error, data = reply[4], reply[5:-1]
        if error:
            raise BusServoError(f"Servo {servo_id} reported an error (status {error:#04x})")
        return data

    def _read_reply(self, request: bytes) -> bytes:
        """Read the next packet from the bus that isn't an echo of the request."""
        deadline = time.monotonic() + self._timeout * 4
        while True:
            packet = self._read_packet(deadline)
            if packet is None:
                raise BusServoError(
                    f"No reply from bus servo {request[2]}. "
                    "Check its ID, wiring and power supply"
                )
            if packet != request:  # some adapters echo what they send
                return packet

    def _read_packet(self, deadline: float) -> Optional[bytes]:
        buffer = b""
        while time.monotonic() < deadline:
            buffer += self._serial.read(1 if len(buffer) < 4 else 4 + buffer[3] - len(buffer))
            start = buffer.find(b"\xff\xff")
            if start < 0:
                buffer = buffer[-1:]
                continue
            buffer = buffer[start:]
            if len(buffer) >= 4 and len(buffer) >= 4 + buffer[3]:
                packet = buffer[:4 + buffer[3]]
                if _checksum(packet[2:-1]) != packet[-1]:
                    raise BusServoError("Corrupted reply from a bus servo")
                return packet
        return None

    def ping(self, servo_id: int) -> bool:
        """
        Check whether a servo with this ID answers.

        :param servo_id: The servo's ID, from 0 to 253.
        :return: True if the servo answered.
        """
        try:
            self._transact(_check_id(servo_id), INST_PING)
            return True
        except BusServoError:
            return False

    def scan(self, ids: Iterable[int] = SCAN_IDS) -> list[int]:
        """
        Find the servos that answer.

        :param ids: The IDs to try, defaults to 1 to 20.
        :return: The IDs that answered.
        """
        return [servo_id for servo_id in ids if self.ping(servo_id)]

    def read(self, servo_id: int, address: int, length: int) -> bytes:
        """Read bytes from a servo's memory."""
        return self._transact(_check_id(servo_id), INST_READ, bytes([address, length]))

    def write(self, servo_id: int, address: int, data: bytes) -> None:
        """Write bytes to a servo's memory."""
        self._transact(_check_id(servo_id), INST_WRITE, bytes([address]) + data)

    @property
    def servos(self) -> _ServoLookup:
        """
        The servos on this adapter, by ID.

        ``adapter.servos[1]`` is the ST/STS-series servo with ID 1. For an
        SC/SCS-series servo, use ``adapter.servo(1, series="scs")`` once first.
        """
        return _ServoLookup(self)

    def servo(self, servo_id: int, series: str = "sts") -> BusServo:
        """
        Get a servo by ID, saying which series it is.

        :param servo_id: The servo's ID.
        :param series: "sts" for ST/STS servos (e.g. ST3215), or "scs" for
            SC/SCS servos (e.g. SC09).
        """
        if series not in SERIES:
            raise ValueError('series must be "sts" or "scs"')
        servo = self._servos.get(servo_id)
        if servo is None or servo.series != series:
            servo = self._servos[servo_id] = BusServo(self, _check_id(servo_id), series)
        return servo

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: {self._identity.asset_tag}>"


class _ServoLookup:
    __slots__ = ('_adapter',)

    def __init__(self, adapter: BusServoAdapter) -> None:
        self._adapter = adapter

    def __getitem__(self, servo_id: int) -> BusServo:
        existing = self._adapter._servos.get(servo_id)
        return existing if existing is not None else self._adapter.servo(servo_id)


def _check_id(servo_id: int) -> int:
    if not isinstance(servo_id, int) or isinstance(servo_id, bool) or not 0 <= servo_id <= 254:
        raise ValueError("Servo IDs are integers from 0 to 253")
    return servo_id


class BusServo:
    """
    A serial bus servo.

    :param adapter: The adapter the servo is connected to.
    :param servo_id: The servo's ID.
    :param series: "sts" (ST/STS) or "scs" (SC/SCS).
    """
    __slots__ = ('_adapter', 'id', 'series')

    def __init__(self, adapter: BusServoAdapter, servo_id: int, series: str = "sts") -> None:
        self._adapter = adapter
        self.id = servo_id
        self.series = series

    @property
    def _spec(self) -> dict:
        return SERIES[self.series]

    def _encode(self, value: int) -> bytes:
        if value < 0:  # sign bit, as the SDK's scs_toscs
            value = -value | 0x8000
        return value.to_bytes(2, "little" if self._spec["little_endian"] else "big")

    def _decode(self, data: bytes, signed: bool = True) -> int:
        value = int.from_bytes(data[:2], "little" if self._spec["little_endian"] else "big")
        if signed and value & 0x8000:
            return -(value & 0x7FFF)
        return value

    @property
    def position(self) -> int:
        """
        The servo's position, in steps.

        ST/STS servos go from 0 to 4095 over 360°; SC/SCS servos from 0 to 1023 over 300°.
        """
        data = self._adapter.read(self.id, ADDR_PRESENT_POSITION, 2)
        return self._decode(data, signed=self.series == "sts")

    @position.setter
    def position(self, value: int) -> None:
        self.move(position=value)

    @property
    def angle(self) -> float:
        """The servo's angle, in degrees (0 to 360 for ST/STS, 0 to 300 for SC/SCS)."""
        return float(self.position * self._spec["degrees"] / (self._spec["max_position"] + 1))

    @angle.setter
    def angle(self, value: float) -> None:
        self.move(angle=value)

    def move(
        self,
        position: Optional[int] = None,
        *,
        angle: Optional[float] = None,
        speed: int = 0,
        acceleration: int = 0,
        wait: bool = False,
    ) -> None:
        """
        Move the servo to a position or an angle.

        :param position: The target position, in steps.
        :param angle: The target angle, in degrees, instead of a position.
        :param speed: Speed in steps per second; 0 is the servo's maximum.
        :param acceleration: ST/STS acceleration (0 to 254); 0 is the maximum.
        :param wait: Wait until the servo stops moving.
        :raises ValueError: If the target, speed or acceleration is out of range.
        """
        spec = self._spec
        if (position is None) == (angle is None):
            raise ValueError("Give either a position or an angle")
        if angle is not None:
            if not 0 <= angle <= spec["degrees"]:
                raise ValueError(f"angle must be from 0 to {spec['degrees']:g} degrees")
            position = round(angle * (spec["max_position"] + 1) / spec["degrees"])
            position = min(position, spec["max_position"])
        assert position is not None
        if not isinstance(position, int) or not 0 <= position <= spec["max_position"]:
            raise ValueError(f"position must be an integer from 0 to {spec['max_position']}")
        if not isinstance(speed, int) or not 0 <= speed <= 32767:
            raise ValueError("speed must be an integer from 0 to 32767")
        if not isinstance(acceleration, int) or not 0 <= acceleration <= 254:
            raise ValueError("acceleration must be an integer from 0 to 254")

        if self.series == "sts":
            # acceleration, position, time (unused), speed, as the SDK's WritePosEx
            data = (bytes([acceleration]) + self._encode(position)
                    + b"\x00\x00" + self._encode(speed))
            self._adapter.write(self.id, ADDR_ACCELERATION, data)
        else:
            # position, time (unused), speed, as the SDK's WritePos
            data = self._encode(position) + b"\x00\x00" + self._encode(speed)
            self._adapter.write(self.id, ADDR_GOAL_POSITION, data)
        if wait:
            self.wait_until_stopped()

    def wait_until_stopped(self, timeout: float = 10) -> None:
        """Wait until the servo has stopped moving."""
        deadline = time.monotonic() + timeout
        time.sleep(0.02)
        while self.is_moving:
            if time.monotonic() > deadline:
                raise BusServoError(f"Servo {self.id} was still moving after {timeout}s")
            time.sleep(0.01)

    @property
    def is_moving(self) -> bool:
        """Whether the servo is moving."""
        return self._adapter.read(self.id, ADDR_MOVING, 1)[0] != 0

    @property
    def torque(self) -> bool:
        """Whether the servo is holding its position. Turn off to move it by hand."""
        return self._adapter.read(self.id, ADDR_TORQUE_ENABLE, 1)[0] != 0

    @torque.setter
    def torque(self, enabled: bool) -> None:
        self._adapter.write(self.id, ADDR_TORQUE_ENABLE, bytes([1 if enabled else 0]))

    @property
    def speed(self) -> int:
        """The servo's current speed, in steps per second."""
        return self._decode(self._adapter.read(self.id, ADDR_PRESENT_SPEED, 2))

    @property
    def load(self) -> int:
        """The servo's current load, from -1000 to 1000 (tenths of a percent)."""
        value = int.from_bytes(self._adapter.read(self.id, ADDR_PRESENT_LOAD, 2),
                               "little" if self._spec["little_endian"] else "big")
        return -(value & 0x3FF) if value & 0x400 else value & 0x3FF

    @property
    def voltage(self) -> float:
        """The servo's supply voltage, in volts."""
        return self._adapter.read(self.id, ADDR_PRESENT_VOLTAGE, 1)[0] / 10

    @property
    def temperature(self) -> int:
        """The servo's temperature, in °C."""
        return self._adapter.read(self.id, ADDR_PRESENT_TEMPERATURE, 1)[0]

    def change_id(self, new_id: int) -> None:
        """
        Give the servo a new ID. Do this with only this servo connected.

        :param new_id: The new ID, from 1 to 253.
        """
        if not isinstance(new_id, int) or not 1 <= new_id <= 253:
            raise ValueError("new_id must be an integer from 1 to 253")
        lock = ADDR_LOCK[self.series]
        self._adapter.write(self.id, lock, b"\x00")  # unlock the EEPROM
        self._adapter.write(self.id, ADDR_ID, bytes([new_id]))
        self._adapter.write(new_id, lock, b"\x01")  # lock it again, at the new ID
        self._adapter._servos.pop(self.id, None)
        self.id = new_id
        self._adapter._servos[new_id] = self

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: {self.id} ({self.series})>"
