"""The Arduino module provides an interface to the Arduino firmware."""
from __future__ import annotations

import atexit
import logging
from enum import Enum, IntEnum
from types import MappingProxyType
from typing import Optional

from serial.tools.list_ports import comports

from .exceptions import IncorrectBoardError
from .logging import log_to_debug
from .motor_board import MotorPower
from .pin_motors import DCMotor, Stepper
from .serial_wrapper import SerialWrapper
from .utils import (
    IN_SIMULATOR, Board, BoardIdentity,
    get_simulator_boards, get_USB_identity, map_to_float,
)

logger = logging.getLogger(__name__)
BAUDRATE = 115200
MAX_STEPPER_COMMAND_US = 350_000
MAX_STEPPER_DELAY_US = 10_000
MIN_STEPPER_DELAY_US = 2
MAX_STEPPER_STEPS = 99999
DEFAULT_STEP_DELAY_US = 1000
STEPPER_PIN_COMMAND = 's'
PWM_COMMAND = 'w'
if IN_SIMULATOR:
    # Place each command on a new line in the simulator to simplify the implementation
    ENDLINE = '\n'
else:
    ENDLINE = ''

# USB IDs of boards the PSC firmware runs on, from the Arduino cores' boards.txt.
# The board is only used if it then answers with SR-compatible firmware.
SUPPORTED_VID_PIDS = {
    # Uno R3, Uno Mini
    (0x2341, 0x0043), (0x2341, 0x0001), (0x2341, 0x0243), (0x2341, 0x006A),
    (0x2341, 0x0062), (0x2A03, 0x0043),
    # Uno R4 Minima and WiFi
    (0x2341, 0x0069), (0x2341, 0x0369), (0x2341, 0x1002), (0x2341, 0x006D),
    # Mega 2560
    (0x2341, 0x0010), (0x2341, 0x0042), (0x2341, 0x0210), (0x2341, 0x0242),
    (0x2A03, 0x0010), (0x2A03, 0x0042),
    # Leonardo and Micro (running a sketch, not the bootloader)
    (0x2341, 0x8036), (0x2A03, 0x8036), (0x2341, 0x8037), (0x2A03, 0x8037),
    (0x2341, 0x8237),
    # Nano Every, Uno WiFi Rev2
    (0x2341, 0x0058), (0x03EB, 0x2145),
    # Due (Programming Port)
    (0x2341, 0x003D), (0x2A03, 0x003D),
    # Zero (Programming Port), Nano 33 IoT, MKR WiFi 1010, MKR Zero
    (0x03EB, 0x2157), (0x2341, 0x8057), (0x2341, 0x8054), (0x2341, 0x804F),
    # Clones with CH340 / CH9102 USB chips
    (0x1A86, 0x7523), (0x1A86, 0x55D4),
    # Ruggeduino
    (0x10C4, 0xEA60), (0x16D0, 0x0613),
}

# Firmware from this version reports its analog reference voltage ('m' command).
ANALOG_REFERENCE_FW_VERSION = 5
# Firmware from this version can set PWM duty cycles ('w' command), for DC motors.
PWM_FW_VERSION = 6
DEFAULT_ANALOG_REFERENCE_V = 5.0


class GPIOPinMode(str, Enum):
    """The possible modes for a GPIO pin."""
    INPUT = 'INPUT'
    INPUT_PULLUP = 'INPUT_PULLUP'
    OUTPUT = 'OUTPUT'


class AnalogPins(IntEnum):
    """The analog pins on the Arduino."""
    A0 = 14
    A1 = 15
    A2 = 16
    A3 = 17
    A4 = 18
    A5 = 19


MODE_CHAR_MAP = {
    GPIOPinMode.INPUT: 'i',
    GPIOPinMode.INPUT_PULLUP: 'p',
    GPIOPinMode.OUTPUT: 'o',
}


DIGITAL_READ_MODES = {GPIOPinMode.INPUT, GPIOPinMode.INPUT_PULLUP, GPIOPinMode.OUTPUT}
DIGITAL_WRITE_MODES = {GPIOPinMode.OUTPUT}
ANALOG_READ_MODES = {GPIOPinMode.INPUT}


class Arduino(Board):
    """
    The Arduino board interface.

    This is intended to be used with Arduino boards running the SR or PSC
    firmware. Pins are numbered like an Uno (2-13, A0-A5) on every board.

    :param serial_port: The serial port to connect to.
    :param initial_identity: The identity of the board, as reported by the USB descriptor.
    """
    __slots__ = ('_serial_num', '_serial', '_pins', '_identity', '_motors', '_claims')

    @staticmethod
    def get_board_type() -> str:
        """
        Return the type of the board.

        :return: The literal string 'Arduino'.
        """
        return 'Arduino'

    def __init__(
        self,
        serial_port: str,
        initial_identity: Optional[BoardIdentity] = None,
    ) -> None:
        if initial_identity is None:
            initial_identity = BoardIdentity()

        # The arduino firmware cannot access the serial number reported in the USB descriptor
        self._serial_num = initial_identity.asset_tag
        self._serial = SerialWrapper(
            serial_port,
            BAUDRATE,
            identity=initial_identity,
            delay_after_connect=2,  # Wait for the board to reset after connecting
        )

        self._pins = (
            tuple(  # Pins 0 and 1 are reserved for serial comms
                Pin(self._serial, index, supports_analog=False, disabled=True)
                for index in range(2))
            + tuple(Pin(self._serial, index, supports_analog=False) for index in range(2, 14))
            + tuple(Pin(self._serial, index, supports_analog=True) for index in range(14, 20))
        )

        # Motors by kind and pins, and which motor uses each pin.
        self._motors: dict[tuple, object] = {}
        self._claims: dict[int, tuple] = {}

        self._identity = self.identify()
        # Arduino board type is not validated to allow for custom firmwares
        if not self._identity.board_type.startswith('SR'):
            raise IncorrectBoardError(self._identity.board_type, 'SR*')
        self._serial.set_identity(self._identity)

        analog_reference = self._read_analog_reference()
        for pin in self._pins:
            pin._analog_reference = analog_reference

    def _read_analog_reference(self) -> float:
        """
        Get the voltage that an analog reading's full scale represents.

        3.3V boards such as the Due read up to 3.3V. Firmware before version 5
        doesn't report it, so 5V is assumed, as on an Uno.
        """
        if self._firmware_version() < ANALOG_REFERENCE_FW_VERSION:
            return DEFAULT_ANALOG_REFERENCE_V
        try:
            return int(self._serial.query('m', endl=ENDLINE)) / 1000
        except ValueError:
            return DEFAULT_ANALOG_REFERENCE_V

    def _firmware_version(self) -> int:
        """Get the firmware's major version, or 0 if it doesn't report a number."""
        try:
            return int(self._identity.sw_version.split('.')[0])
        except ValueError:
            return 0

    @classmethod
    def _get_valid_board(
        cls,
        serial_port: str,
        initial_identity: Optional[BoardIdentity] = None,
    ) -> Optional[Arduino]:
        """
        Attempt to connect to an Arduino and returning None if it fails identification.

        :param serial_port: The serial port to connect to.
        :param initial_identity: The identity of the board, as reported by the USB descriptor.

        :return: An Arduino object, or None if the board could not be identified.
        """
        try:
            board = cls(serial_port, initial_identity)
        except IncorrectBoardError as err:
            logger.warning(
                f"Board returned type {err.returned_type!r}, "
                f"expected {err.expected_type!r}. Ignoring this device")
            return None
        except Exception:
            if initial_identity is not None:
                if initial_identity.board_type == 'manual':
                    logger.warning(
                        f"Manually specified Arduino at port {serial_port!r} "
                        "could not be identified. Ignoring this device")
                elif initial_identity.manufacturer == 'sbot_simulator':
                    logger.warning(
                        f"Simulator specified arduino at port {serial_port!r} "
                        "could not be identified. Ignoring this device")
                return None

            logger.warning(
                f"Found Arduino-like serial port at {serial_port!r}, "
                "but it could not be identified. Ignoring this device")
            return None
        return board

    @classmethod
    def _get_simulator_boards(cls) -> MappingProxyType[str, Arduino]:
        """
        Get the simulator boards.

        :return: A mapping of board serial numbers to Arduinos
        """
        boards = {}
        # The filter here is the name of the emulated board in the simulator
        for board_info in get_simulator_boards('Arduino'):

            # Create board identity from the info given
            initial_identity = BoardIdentity(
                manufacturer='sbot_simulator',
                board_type=board_info.type_str,
                asset_tag=board_info.serial_number,
            )
            if (board := cls._get_valid_board(board_info.url, initial_identity)) is None:
                continue

            boards[board._identity.asset_tag] = board
        return MappingProxyType(boards)

    @classmethod
    def _get_supported_boards(
        cls,
        manual_boards: Optional[list[str]] = None,
        ignored_serials: Optional[list[str]] = None,
    ) -> MappingProxyType[str, Arduino]:
        """
        Discover the connected Arduinos, by matching the USB descriptor to SUPPORTED_VID_PIDS.

        :param manual_boards: A list of manually specified board port strings,
            defaults to None
        :param ignored_serials: A list of serial number to ignore during board discovery
        :return: A mapping of board serial numbers to Arduinos
        """
        if IN_SIMULATOR:
            return cls._get_simulator_boards()

        boards = {}
        if ignored_serials is None:
            ignored_serials = []
        serial_ports = comports()
        for port in serial_ports:
            if (port.vid, port.pid) in SUPPORTED_VID_PIDS:
                # Create board identity from USB port info
                initial_identity = get_USB_identity(port)
                if initial_identity.asset_tag in ignored_serials:
                    continue

                if (board := cls._get_valid_board(port.device, initial_identity)) is None:
                    continue

                boards[board._identity.asset_tag] = board

        # Add any manually specified boards
        if isinstance(manual_boards, list):
            for manual_port in manual_boards:
                # Create board identity from the info given
                initial_identity = BoardIdentity(
                    board_type='manual',
                    asset_tag=manual_port,
                )

                if (board := cls._get_valid_board(manual_port, initial_identity)) is None:
                    continue

                boards[board._identity.asset_tag] = board
        return MappingProxyType(boards)

    @log_to_debug
    def identify(self) -> BoardIdentity:
        """
        Get the identity of the board.

        The asset tag of the board is the serial number from the USB descriptor.

        :return: The identity of the board.
        """
        response = self._serial.query('v', endl=ENDLINE)
        response_fields = response.split(':')

        # The arduino firmware cannot access the serial number reported in the USB descriptor
        return BoardIdentity(
            manufacturer=(
                "Student Robotics"
                if response_fields[0].startswith('SR')
                else "Arduino"),
            board_type=response_fields[0],
            asset_tag=self._serial_num,
            sw_version=response_fields[1],
        )

    @property
    @log_to_debug
    def pins(self) -> tuple[Pin, ...]:
        """
        The pins on the Arduino.

        :return: A tuple of the pins on the Arduino.
        """
        return self._pins

    @log_to_debug
    def command(self, command: str) -> str:
        """
        Send a command to the board.

        :param command: The command to send to the board.
        :return: The response from the board.
        """
        if IN_SIMULATOR:
            logger.warning("The command method is not fully supported in the simulator")
        return self._serial.query(command, endl=ENDLINE)

    def map_pin_number(self, pin_number: int) -> str:
        """
        Map the pin number to the the serial format.
        Pin numbers are sent as printable ASCII characters, with 0 being 'a'.

        :param pin_number: The pin number to encode.
        :return: The pin number in the serial format.
        :raises ValueError: If the pin number is invalid.
        """
        try:  # bounds check
            self.pins[pin_number]._check_if_disabled()
        except (IndexError, IOError):
            raise ValueError("Invalid pin provided") from None
        return chr(pin_number + ord('a'))

    @log_to_debug
    def ultrasound_measure(
        self,
        pulse_pin: int,
        echo_pin: int,
    ) -> int:
        """
        Measure the distance to an object using an ultrasound sensor.

        The sensor can only measure distances up to 4m.

        :param pulse_pin: The pin to send the ultrasound pulse from.
        :param echo_pin: The pin to read the ultrasound echo from.
        :raises ValueError: If either of the pins are invalid
        :return: The distance measured by the ultrasound sensor in mm.
        """
        try:  # bounds check
            pulse_id = self.map_pin_number(pulse_pin)
        except ValueError:
            raise ValueError("Invalid pulse pin provided") from None
        try:
            echo_id = self.map_pin_number(echo_pin)
        except ValueError:
            raise ValueError("Invalid echo pin provided") from None

        response = self._serial.query(f'u{pulse_id}{echo_id}', endl=ENDLINE)
        return int(response)

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
        """Move a step/direction driver by a finite number of steps.

        The Arduino generates one 2 microsecond STEP pulse per step and holds
        the STEP line low for ``step_delay_us`` between pulses. This is a
        blocking operation; the maximum pulse train is bounded to fit within
        the serial command timeout. Acceleration and position tracking are not
        provided.

        :param step_pin: Arduino pin connected to the driver's STEP input.
        :param direction_pin: Arduino pin connected to the driver's DIR input.
        :param steps: Number of rising STEP edges to generate (1 to 99999).
        :param direction: True drives DIR high; False drives DIR low.
        :param step_delay_us: Low time between STEP pulses in microseconds.
        :raises ValueError: If pins, step count, direction, or timing are invalid.
        :raises RuntimeError: If the attached firmware does not support this command.
        """
        if not isinstance(steps, int) or isinstance(steps, bool) or not 1 <= steps <= 99999:
            raise ValueError("steps must be an integer from 1 to 99999")
        if not isinstance(direction, bool):
            raise ValueError("direction must be a bool")
        if (
            not isinstance(step_delay_us, int)
            or isinstance(step_delay_us, bool)
            or not 2 <= step_delay_us <= MAX_STEPPER_DELAY_US
        ):
            raise ValueError(
                f"step_delay_us must be an integer from 2 to {MAX_STEPPER_DELAY_US}"
            )
        if steps * (step_delay_us + 100) > MAX_STEPPER_COMMAND_US:
            raise ValueError(
                "stepper move is too long for one serial command; use smaller moves"
            )
        if step_pin == direction_pin:
            raise ValueError("step_pin and direction_pin must be different")
        for pin in (step_pin, direction_pin):
            owner = self._claims.get(pin)
            if owner is not None and owner[0] != 'stepper':
                raise ValueError(f"Pin {pin} is already used by another motor")

        try:
            step_pin_id = self.map_pin_number(step_pin)
        except (TypeError, ValueError):
            raise ValueError("Invalid step pin provided") from None
        try:
            direction_pin_id = self.map_pin_number(direction_pin)
        except (TypeError, ValueError):
            raise ValueError("Invalid direction pin provided") from None

        command = (
            f"{STEPPER_PIN_COMMAND}{step_pin_id}{direction_pin_id}"
            f"{int(direction)}{steps:05d}{step_delay_us:05d}"
        )
        response = self._serial.query(command, endl=ENDLINE)
        if response != "OK":
            raise RuntimeError(
                "Arduino firmware does not support stepper_move; update its firmware"
            )

    def _claim(self, key: tuple, pins: tuple[int, ...], names: tuple[str, ...]) -> None:
        for pin, name in zip(pins, names):
            try:
                self.map_pin_number(pin)
            except (TypeError, ValueError):
                raise ValueError(f"Invalid {name.replace('_', ' ')} provided") from None
        if len(set(pins)) != len(pins):
            raise ValueError(f"{' and '.join(names)} must be different")
        for pin in pins:
            owner = self._claims.get(pin)
            if owner is not None and owner != key:
                raise ValueError(f"Pin {pin} is already used by another motor")
        for pin in pins:
            self._claims[pin] = key

    def _set_duty(self, pin: int, duty: float) -> None:
        response = self._serial.query(
            f"{PWM_COMMAND}{self.map_pin_number(pin)}{round(duty * 255):03d}", endl=ENDLINE)
        if response != "OK":
            raise RuntimeError(
                "Arduino firmware does not support DC motors; update its firmware")

    def _stop_dc_motors(self) -> None:
        """Let the DC motors coast while exiting. Registered as an exit handler."""
        for motor in self._motors.values():
            if isinstance(motor, DCMotor):
                try:
                    motor.power = MotorPower.COAST
                except Exception:
                    logger.warning(f"Failed to stop {motor!r}.")

    @log_to_debug
    def dc_motor(self, in1_pin: int, in2_pin: int) -> DCMotor:
        """
        Get a DC motor connected through a two-input driver, such as a DRV8871.

        Set its ``power`` from -1 to 1, or to ``BRAKE`` or ``COAST``. Both pins must
        support PWM (on an Uno: 3, 5, 6, 9, 10 and 11). Asking for the same pins again
        returns the same motor. The motor starts coasting, and is stopped when your
        code ends.

        :param in1_pin: Arduino pin connected to the driver's IN1 input.
        :param in2_pin: Arduino pin connected to the driver's IN2 input.
        :raises ValueError: If a pin is invalid or used by another motor.
        :raises RuntimeError: If the Arduino's firmware is too old for DC motors.
        """
        key = ('dc', in1_pin, in2_pin)
        if key not in self._motors:
            self._claim(key, (in1_pin, in2_pin), ('in1_pin', 'in2_pin'))
            if self._firmware_version() < PWM_FW_VERSION:
                del self._claims[in1_pin], self._claims[in2_pin]
                raise RuntimeError(
                    f"DC motors need Arduino firmware version {PWM_FW_VERSION} or later; "
                    "update its firmware"
                )
            if not any(isinstance(motor, DCMotor) for motor in self._motors.values()):
                atexit.register(self._stop_dc_motors)

            def set_inputs(in1: float, in2: float) -> None:
                self._set_duty(in1_pin, in1)
                self._set_duty(in2_pin, in2)

            self._motors[key] = DCMotor(set_inputs, f"IN1=pin {in1_pin} IN2=pin {in2_pin}")
        return self._motors[key]  # type: ignore[return-value]

    @log_to_debug
    def stepper(
        self, step_pin: int, direction_pin: int, *, step_delay_us: Optional[int] = None,
    ) -> Stepper:
        """
        Get a stepper motor connected through a step/direction driver, such as an A4988.

        Move it with ``move(steps)``; it counts its ``position`` in steps. Long moves
        are split into several commands, so there is no limit on how long a move takes.
        Asking for the same pins again returns the same motor.

        :param step_pin: Arduino pin connected to the driver's STEP input.
        :param direction_pin: Arduino pin connected to the driver's DIR input.
        :param step_delay_us: The time between steps, in microseconds (2 to 10000),
            when a move doesn't give one. Defaults to 1000 for a new motor.
        :raises ValueError: If a pin is invalid or used by another motor.
        """
        key = ('stepper', step_pin, direction_pin)
        if key not in self._motors:
            self._claim(key, (step_pin, direction_pin), ('step_pin', 'direction_pin'))

            def move_steps(steps: int, direction: bool, delay: int) -> None:
                self.stepper_move(
                    step_pin, direction_pin, steps, direction=direction, step_delay_us=delay)

            def max_steps(delay: int) -> int:
                return max(1, min(MAX_STEPPER_STEPS, MAX_STEPPER_COMMAND_US // (delay + 100)))

            self._motors[key] = Stepper(
                move_steps, max_steps, MIN_STEPPER_DELAY_US, MAX_STEPPER_DELAY_US,
                DEFAULT_STEP_DELAY_US, f"STEP=pin {step_pin} DIR=pin {direction_pin}",
            )
        motor = self._motors[key]
        assert isinstance(motor, Stepper)
        if step_delay_us is not None:
            motor.step_delay_us = step_delay_us
        return motor

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: {self._serial}>"


class Pin:
    """
    A pin on the Arduino.

    :param serial: The serial wrapper to use to communicate with the board.
    :param index: The index of the pin.
    :param supports_analog: Whether the pin supports analog reads.
    :param disabled: Whether the pin can be controlled.
    """
    __slots__ = (
        '_serial', '_index', '_supports_analog', '_disabled', '_mode', '_analog_reference',
    )

    def __init__(
        self,
        serial: SerialWrapper,
        index: int,
        supports_analog: bool,
        disabled: bool = False
    ):
        self._serial = serial
        self._index = index
        self._analog_reference = DEFAULT_ANALOG_REFERENCE_V
        self._supports_analog = supports_analog
        self._disabled = disabled
        self._mode = GPIOPinMode.INPUT

    @property
    @log_to_debug
    def mode(self) -> GPIOPinMode:
        """
        Get the mode of the pin.

        This returns the cached value since the board does not report this.

        :raises IOError: If this pin cannot be controlled.
        :return: The mode of the pin.
        """
        self._check_if_disabled()
        return self._mode

    @mode.setter
    @log_to_debug(setter=True)
    def mode(self, value: GPIOPinMode) -> None:
        """
        Set the mode of the pin.

        To do analog or digital reads set the mode to INPUT or INPUT_PULLUP.
        To do digital writes set the mode to OUTPUT.

        :param value: The mode to set the pin to.
        :raises IOError: If the pin mode is not a GPIOPinMode.
        :raises IOError: If this pin cannot be controlled.
        """
        self._check_if_disabled()
        if not isinstance(value, GPIOPinMode):
            raise IOError('Pin mode only supports being set to a GPIOPinMode')

        mode_char = MODE_CHAR_MAP.get(value)
        if mode_char is None:
            raise IOError(f'Pin mode {value} is not supported')
        self._serial.write(self._build_command(mode_char), endl=ENDLINE)
        self._mode = value

    @log_to_debug
    def digital_read(self) -> bool:
        """
        Perform a digital read on the pin.

        :raises IOError: If the pin's current mode does not support digital read
        :raises IOError: If this pin cannot be controlled.
        :return: The digital value of the pin.
        """
        self._check_if_disabled()
        if self.mode not in DIGITAL_READ_MODES:
            raise IOError(f'Digital read is not supported in {self.mode}')
        response = self._serial.query(self._build_command('r'), endl=ENDLINE)
        return response == 'h'

    @log_to_debug
    def digital_write(self, value: bool) -> None:
        """
        Write a digital value to the pin.

        :param value: The value to write to the pin.
        :raises IOError: If the pin's current mode does not support digital write.
        :raises IOError: If this pin cannot be controlled.
        """
        self._check_if_disabled()
        if self.mode not in DIGITAL_WRITE_MODES:
            raise IOError(f'Digital write is not supported in {self.mode}')
        if value:
            self._serial.write(self._build_command('h'), endl=ENDLINE)
        else:
            self._serial.write(self._build_command('l'), endl=ENDLINE)

    @log_to_debug
    def analog_read(self) -> float:
        """
        Get the analog voltage on the pin.

        This is returned in volts. Only pins A0-A5 support analog reads.
        Readings range from 0 to 5V, or 0 to 3.3V on 3.3V boards such as the Due.

        :raises IOError: If the pin or its current mode does not support analog read.
        :raises IOError: If this pin cannot be controlled.
        :return: The analog voltage on the pin, from 0 to the board's reference voltage.
        """
        ADC_MAX = 1023  # 10 bit ADC
        ADC_MIN = 0

        self._check_if_disabled()
        if self.mode not in ANALOG_READ_MODES:
            raise IOError(f'Analog read is not supported in {self.mode}')
        if not self._supports_analog:
            raise IOError('Pin does not support analog read')
        response = self._serial.query(self._build_command('a'), endl=ENDLINE)
        # map the response from the ADC range to the voltage range
        return map_to_float(int(response), ADC_MIN, ADC_MAX, 0.0, self._analog_reference)

    def _check_if_disabled(self) -> None:
        """
        Check if the pin is disabled.

        :raises IOError: If the pin is disabled.
        """
        if self._disabled:
            raise IOError('This pin cannot be controlled.')

    def _map_pin_number(self) -> str:
        """
        Map the pin number to the the serial format.
        Pin numbers are sent as printable ASCII characters, with 0 being 'a'.

        :return: The pin number in the serial format.
        """
        return chr(self._index + ord('a'))

    def _build_command(self, cmd_char: str) -> str:
        """
        Generate the command to send to the board.

        :param cmd_char: The command character to send.
        :return: The command string.
        """
        return f'{cmd_char}{self._map_pin_number()}'

    def __repr__(self) -> str:
        return (
            f"<{self.__class__.__qualname__} "
            f"index={self._index} analog={self._supports_analog} "
            f"disabled={self._disabled} {self._serial}>"
        )


if __name__ == '__main__':  # pragma: no cover
    arduinos = Arduino._get_supported_boards()
    for serial_num, board in arduinos.items():
        print(serial_num)

        board.pins[4].mode = GPIOPinMode.INPUT
        board.pins[4].mode = GPIOPinMode.INPUT_PULLUP

        # Digital write
        board.pins[13].mode = GPIOPinMode.OUTPUT
        board.pins[13].digital_write(True)
        digital_write_value = board.pins[13].digital_read()
        print(f'Set pin 13 to output and set to {digital_write_value}')

        # Digital read
        board.pins[4].mode = GPIOPinMode.INPUT
        digital_read_value = board.pins[4].digital_read()
        print(f'Input 4 = {digital_read_value}')

        board.pins[5].mode = GPIOPinMode.INPUT_PULLUP
        digital_read_value = board.pins[5].digital_read()
        print(f'Input 5 = {digital_read_value}')

        # Analog read
        board.pins[AnalogPins.A0].mode = GPIOPinMode.INPUT
        analog_read_value = board.pins[AnalogPins.A0].analog_read()
        print(f'Analog input A0 = {analog_read_value}')
