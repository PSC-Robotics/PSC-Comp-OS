"""Test I2C and the PCA9685 servo driver against a fake kernel interface."""
import atexit
import ctypes
import errno
import struct

import pytest

from sr.robot3 import I2CError
from sr.robot3.i2c import (
    EREMOTEIO, I2C_M_RD, I2C_RDWR, I2CBus, ServoDriver, _Message, _Transfer,
)
from sr.robot3.raspberry_pi import RaspberryPi

FD = 7


class FakeKernel:
    """Decodes transfers exactly as Linux's i2c-dev does, from the raw structs."""

    def __init__(self, addresses=(0x40,)):
        self.registers = {address: {} for address in addresses}
        self.pointers = {}
        self.writes = []
        self.error = None
        self.ignore_prescale = False

    def open(self, path, flags):
        assert path == "/dev/i2c-1"
        return FD

    def ioctl(self, fd, request, transfer):
        assert (fd, request) == (FD, I2C_RDWR)
        if self.error is not None:
            raise OSError(self.error, "fake error")
        # What the kernel sees is bytes: decode from them, not the Python fields.
        pointer, count = struct.unpack_from("=QI", bytes(transfer))
        size = ctypes.sizeof(_Message)
        for index in range(count):
            raw = ctypes.string_at(pointer + index * size, size)
            address, flags, length = struct.unpack_from("=HHH", raw)
            data_pointer = struct.unpack_from("=Q", raw, 8)[0]
            if address not in self.registers:
                raise OSError(EREMOTEIO, "Remote I/O error")
            if flags & I2C_M_RD:
                values = self._read(address, length)
                ctypes.memmove(data_pointer, bytes(values), length)
            else:
                self._write(address, ctypes.string_at(data_pointer, length))
        return count

    def _write(self, address, data):
        self.writes.append((address, data))
        registers = self.registers[address]
        self.pointers[address] = data[0]
        for offset, value in enumerate(data[1:]):
            if not (self.ignore_prescale and data[0] == 0xFE):
                registers[data[0] + offset] = value

    def _read(self, address, length):
        start = self.pointers.get(address, 0)
        return [self.registers[address].get(start + i, 0) for i in range(length)]


@pytest.fixture
def kernel():
    return FakeKernel()


@pytest.fixture
def bus(kernel):
    return I2CBus(opener=kernel.open, ioctl=kernel.ioctl)


def test_structs_match_the_kernel_on_64_bit():
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        pytest.skip("64-bit layout")
    assert ctypes.sizeof(_Message) == 16
    assert _Message.buf.offset == 8
    assert ctypes.sizeof(_Transfer) == 16
    assert _Transfer.nmsgs.offset == 8


def test_write_and_read_a_register(kernel, bus):
    device = bus.device(0x40)
    device.write(0x10, [1, 2])
    device.write(0x20, 3)
    device.write(0x21, b"\x04")
    assert kernel.writes[0] == (0x40, b"\x10\x01\x02")
    assert device.read(0x10, 2) == b"\x01\x02"
    assert device.read(0x20)[0] == 3
    assert device.read(0x21) == b"\x04"


def test_send_and_receive(kernel, bus):
    device = bus.device(0x40)
    device.send([9, 8])
    assert kernel.writes == [(0x40, b"\x09\x08")]
    kernel.registers[0x40][9] = 0xAB
    assert device.receive() == b"\xab"


def test_read_keeps_the_bus_between_write_and_read(kernel, bus):
    calls = []
    real_ioctl = kernel.ioctl

    def counting(fd, request, transfer):
        calls.append(struct.unpack_from("=QI", bytes(transfer))[1])
        return real_ioctl(fd, request, transfer)

    bus._ioctl = counting
    bus.device(0x40).read(0)
    assert calls == [2]


def test_scan():
    kernel = FakeKernel((0x40, 0x48))
    bus = I2CBus(opener=kernel.open, ioctl=kernel.ioctl)
    assert bus.scan() == [0x40, 0x48]


def test_same_device_returned(bus):
    assert bus.device(0x40) is bus.device(0x40)


@pytest.mark.parametrize("address", [0x02, 0x78, 256, -1, "0x40", True, 1.5])
def test_bad_address(bus, address):
    with pytest.raises(ValueError, match="I2C address must be from 0x03 to 0x77"):
        bus.device(address)


@pytest.mark.parametrize("register", [-1, 256, "1", True])
def test_bad_register(bus, register):
    with pytest.raises(ValueError, match="register"):
        bus.device(0x40).write(register, 0)
    with pytest.raises(ValueError, match="register"):
        bus.device(0x40).read(register)


@pytest.mark.parametrize("data", [256, -1, [256], ["a"], "abc", None, 1.5, True])
def test_bad_data(bus, data):
    with pytest.raises(ValueError, match="I2C data"):
        bus.device(0x40).write(0, data)


@pytest.mark.parametrize("length", [0, -1, 4097, "1", True])
def test_bad_length(bus, length):
    with pytest.raises(ValueError, match="length"):
        bus.device(0x40).read(0, length)
    with pytest.raises(ValueError, match="length"):
        bus.device(0x40).receive(length)


def test_send_nothing(bus):
    with pytest.raises(ValueError, match="at least one byte"):
        bus.device(0x40).send([])


def test_i2c_turned_off_says_how_to_fix_it():
    def missing(path, flags):
        raise FileNotFoundError(path)

    bus = I2CBus(opener=missing, ioctl=lambda *args: None)
    with pytest.raises(I2CError, match="turned off.*i2c_arm=on.*reboot"):
        bus.device(0x40).write(0, 0)
    with pytest.raises(I2CError, match="turned off"):
        bus.scan()


def test_no_permission_says_how_to_fix_it():
    def denied(path, flags):
        raise PermissionError(path)

    bus = I2CBus(opener=denied, ioctl=lambda *args: None)
    with pytest.raises(I2CError, match="usermod -aG i2c robot"):
        bus.device(0x40).read(0)


def test_wrong_address_names_the_devices_found(bus):
    with pytest.raises(I2CError, match="0x41, but these did: 0x40"):
        bus.device(0x41).read(0)


def test_nothing_on_the_bus_points_at_the_wiring():
    kernel = FakeKernel(())
    bus = I2CBus(opener=kernel.open, ioctl=kernel.ioctl)
    with pytest.raises(I2CError, match="none were found.*SDA is on GPIO 2.*SCL is on GPIO 3"):
        bus.device(0x40).read(0)


def test_stuck_bus(kernel, bus):
    kernel.error = errno.ETIMEDOUT
    with pytest.raises(I2CError, match="bus failed.*shorted"):
        bus.device(0x40).read(0)
    with pytest.raises(I2CError, match="bus failed during a scan"):
        bus.scan()


def test_i2c_error_is_an_oserror():
    assert issubclass(I2CError, OSError)


@pytest.fixture
def servo_driver(bus, monkeypatch):
    registered = []
    monkeypatch.setattr(atexit, 'register', registered.append)
    driver = ServoDriver(bus.device(0x40), sleep=lambda secs: None)
    return driver, registered


def channel_off(kernel, port):
    registers = kernel.registers[0x40]
    base = 0x06 + 4 * port
    return registers[base + 2] | registers[base + 3] << 8


def test_nothing_on_the_bus_until_a_servo_moves(kernel, servo_driver):
    assert kernel.writes == []
    assert servo_driver[0].servos[0].position is None
    assert kernel.writes == []


def test_driver_setup_order(kernel, servo_driver):
    driver, _ = servo_driver
    driver.servos[0].position = 0
    assert kernel.writes[:3] == [
        (0x40, bytes([0x00, 0x10])),  # asleep first: the frequency needs it
        (0x40, bytes([0xFD, 0x10])),  # every channel off
        (0x40, bytes([0xFE, 121])),  # 50Hz
    ]
    assert (0x40, bytes([0x00, 0x20])) in kernel.writes[3:5]  # awake, auto-increment


def test_setup_happens_once(kernel, servo_driver):
    driver, registered = servo_driver
    driver.servos[0].position = 0
    driver.servos[1].position = 0
    assert sum(1 for _, data in kernel.writes if data == bytes([0xFE, 121])) == 1
    assert len(registered) == 1


@pytest.mark.parametrize("position, pulse_us", [(-1, 1000), (0, 1500), (1, 2000)])
def test_positions_give_sr_pulses(kernel, servo_driver, position, pulse_us):
    driver, _ = servo_driver
    driver.servos[3].position = position
    period_us = 1_000_000 * 122 * 4096 / 25_000_000
    assert channel_off(kernel, 3) * period_us / 4096 == pytest.approx(pulse_us, abs=3)
    assert driver.servos[3].position == position


def test_channel_registers(kernel, servo_driver):
    driver, _ = servo_driver
    driver.servos[15].position = 0
    assert kernel.writes[-1][1][0] == 0x06 + 4 * 15
    assert kernel.writes[-1][1][1:3] == b"\x00\x00"


def test_disable(kernel, servo_driver):
    driver, _ = servo_driver
    driver.servos[2].position = 0.5
    driver.servos[2].position = None
    assert kernel.writes[-1][1] == bytes([0x06 + 8, 0, 0, 0, 0x10])
    assert driver.servos[2].position is None


def test_duty_limits(kernel, servo_driver):
    driver, _ = servo_driver
    servo = driver.servos[0]
    servo.set_duty_limits(500, 2500)
    assert servo.get_duty_limits() == (500, 2500)
    servo.position = 1
    period_us = 1_000_000 * 122 * 4096 / 25_000_000
    assert channel_off(kernel, 0) * period_us / 4096 == pytest.approx(2500, abs=3)
    with pytest.raises(ValueError, match="500 to 4000"):
        servo.set_duty_limits(100, 2000)
    with pytest.raises(TypeError, match="ints"):
        servo.set_duty_limits(1000.0, 2000)


@pytest.mark.parametrize("position", [2, -1.5, "a"])
def test_bad_position(kernel, servo_driver, position):
    driver, _ = servo_driver
    with pytest.raises((ValueError, TypeError), match="between -1.0 and 1.0"):
        driver.servos[0].position = position
    assert kernel.writes == []


@pytest.mark.parametrize("port", [16, -17])
def test_bad_port_lists_the_ports(servo_driver, port):
    driver, _ = servo_driver
    with pytest.raises(IndexError, match=f"port {port} doesn't exist.*0 to 15"):
        driver.servos[port]


def test_ports_are_a_sequence(servo_driver):
    driver, _ = servo_driver
    assert len(driver.servos) == 16
    assert len(list(driver.servos)) == 16
    assert len(driver.servos[:2]) == 2


def test_missing_driver_error(servo_driver):
    kernel = FakeKernel(())
    bus = I2CBus(opener=kernel.open, ioctl=kernel.ioctl)
    driver = ServoDriver(bus.device(0x40), sleep=lambda secs: None)
    with pytest.raises(I2CError, match="Can't find the servo driver.*SDA is on GPIO 2"):
        driver.servos[0].position = 0
    assert driver.servos[0].position is None


def test_a_different_chip_at_the_address_is_caught(kernel, servo_driver):
    driver, registered = servo_driver
    kernel.ignore_prescale = True
    with pytest.raises(I2CError, match="isn't a PCA9685"):
        driver.servos[0].position = 0
    assert registered == []


def test_servos_stop_when_code_ends(kernel, servo_driver):
    driver, registered = servo_driver
    driver.servos[0].position = 1
    registered[0]()
    assert kernel.writes[-1] == (0x40, bytes([0xFD, 0x10]))


def test_stopping_ignores_a_lost_driver(kernel, servo_driver):
    driver, registered = servo_driver
    driver.servos[0].position = 1
    kernel.error = EREMOTEIO
    registered[0]()


def make_pi(kernel):
    return RaspberryPi(
        gpio_loader=lambda: None, reserved={},
        i2c=I2CBus(opener=kernel.open, ioctl=kernel.ioctl),
    )


def test_raspberry_pi_servos(kernel, monkeypatch):
    monkeypatch.setattr(atexit, 'register', lambda fn: None)
    pi = make_pi(kernel)
    assert len(pi.servos) == 16
    assert pi.servos is pi.servo_driver().servos
    pi.servos[0].position = 0
    assert channel_off(kernel, 0) > 0


def test_raspberry_pi_second_driver():
    kernel = FakeKernel((0x40, 0x41))
    pi = make_pi(kernel)
    assert pi.servo_driver(0x41) is pi.servo_driver(0x41)
    assert pi.servo_driver(0x41) is not pi.servo_driver()
    with pytest.raises(ValueError, match="I2C address"):
        pi.servo_driver(0x99)


def test_raspberry_pi_i2c(kernel):
    pi = make_pi(kernel)
    assert pi.i2c.scan() == [0x40]


def test_i2c_pins_are_reserved():
    pi = RaspberryPi(gpio_loader=lambda: None)
    with pytest.raises(ValueError, match="GPIO 2 is used by I2C"):
        pi.dc_motor(2, 4)
    with pytest.raises(ValueError, match="GPIO 3 is used by I2C"):
        pi.stepper(4, 3)
