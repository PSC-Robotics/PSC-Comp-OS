"""Test I2C and the PCA9685 servo driver against a fake kernel interface."""
import atexit
import ctypes
import errno
import struct
import types

import pytest

from sr.robot3 import I2CError
from sr.robot3.i2c import (
    EREMOTEIO, I2C_M_RD, I2C_RDWR, I2CBus, ServoDriver, _Message, _Transfer,
)
from sr.robot3.brain import Brain

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
    return Brain(
        gpio_loader=lambda: None, reserved={},
        i2c=I2CBus(opener=kernel.open, ioctl=kernel.ioctl),
    )


def test_brain_servo_driver_on_the_hardware_pins(kernel, monkeypatch):
    monkeypatch.setattr(atexit, 'register', lambda fn: None)
    pi = make_pi(kernel)
    driver = pi.servo_driver(sda=2, scl=3)
    assert len(driver.servos) == 16
    driver.servos[0].position = 0
    assert channel_off(kernel, 0) > 0


def test_brain_second_driver():
    kernel = FakeKernel((0x40, 0x41))
    pi = make_pi(kernel)
    second = pi.servo_driver(sda=2, scl=3, address=0x41)
    assert second is pi.servo_driver(sda=2, scl=3, address=0x41)
    assert second is not pi.servo_driver(sda=2, scl=3)
    with pytest.raises(ValueError, match="I2C address"):
        pi.servo_driver(sda=2, scl=3, address=0x99)


def test_servo_driver_needs_its_pins(kernel):
    with pytest.raises(TypeError, match="sda"):
        make_pi(kernel).servo_driver()


def test_i2c_pins_are_reserved():
    pi = Brain(gpio_loader=lambda: None)
    with pytest.raises(ValueError, match="GPIO 2 is used by I2C"):
        pi.dc_motor(2, 4)
    with pytest.raises(ValueError, match="GPIO 3 is used by I2C"):
        pi.stepper(4, 3)


class FakeChip:
    """An I2C chip with registers, seen only through its effect on the two wires."""

    def __init__(self, address, stretch=0):
        self.address = address
        self.registers = {}
        self.pointer = 0
        self.stretch = stretch
        self.holding_sda = False
        self.state = 'idle'
        self.byte = 0
        self.bits = 0
        self.first = True
        self.received_stops = 0

    def start(self):
        self.state, self.byte, self.bits = 'address', 0, 0
        self.holding_sda = False

    def stop(self):
        self.state = 'idle'
        self.holding_sda = False
        self.received_stops += 1

    def clock_rising(self, sda):
        if self.state in ('address', 'write'):
            self.byte = self.byte << 1 | sda
            self.bits += 1
        elif self.state == 'master_ack':
            self.state = 'send_next' if not sda else 'idle'

    def clock_falling(self):
        if self.state in ('address', 'write') and self.bits == 8:
            self._byte_received()
        elif self.state == 'ack_write':
            self.holding_sda = False
            self.state, self.byte, self.bits = 'write', 0, 0
        elif self.state == 'ack_read':
            self._send_bit(new_byte=True)
        elif self.state == 'send':
            self._send_bit()
        elif self.state == 'send_next':
            self._send_bit(new_byte=True)
        elif self.state == 'idle':
            self.holding_sda = False

    def _byte_received(self):
        if self.state == 'address':
            if self.byte >> 1 != self.address:
                self.state = 'idle'
                return
            self.state = 'ack_read' if self.byte & 1 else 'ack_write'
            self.first = True
        else:
            if self.first:
                self.pointer = self.byte
                self.first = False
            else:
                self.registers[self.pointer] = self.byte
                self.pointer += 1
            self.state = 'ack_write'
        self.holding_sda = True

    def _send_bit(self, new_byte=False):
        if new_byte:
            self.byte = self.registers.get(self.pointer, 0)
            self.pointer += 1
            self.bits = 0
            self.state = 'send'
        if self.bits == 8:
            self.holding_sda = False
            self.state = 'master_ack'
            return
        self.holding_sda = not self.byte >> (7 - self.bits) & 1
        self.bits += 1


class FakeWireGPIO:
    """RPi.GPIO on open-drain wires, where a line is low if anything pulls it low."""
    BCM, IN, OUT, LOW, HIGH, PUD_UP = "BCM", "IN", "OUT", 0, 1, "PUD_UP"

    def __init__(self):
        self.buses = []
        self.low = set()
        self.setups = []
        self.cleaned = []
        self.drove_high = False

    def add_bus(self, sda, scl, chips):
        self.buses.append({'sda': sda, 'scl': scl, 'chips': chips, 'levels': (1, 1)})

    def setwarnings(self, flag):
        pass

    def setmode(self, mode):
        pass

    def cleanup(self, pins):
        self.cleaned.extend(pins)

    def PWM(self, pin, frequency):
        return types.SimpleNamespace(start=lambda duty: None, stop=lambda: None,
                                     ChangeDutyCycle=lambda duty: None)

    def setup(self, pin, mode, initial=None, pull_up_down=None):
        self.setups.append(pin)
        if mode == self.OUT:
            if initial != self.LOW:
                self.drove_high = True
            self.low.add(pin)
        else:
            assert pull_up_down == self.PUD_UP
            self.low.discard(pin)
        self._update()

    def _level(self, bus, pin):
        if pin in self.low:
            return 0
        if pin == bus['sda'] and any(chip.holding_sda for chip in bus['chips']):
            return 0
        if pin == bus['scl'] and any(chip.stretch > 0 for chip in bus['chips']):
            return 0
        return 1

    def _update(self):
        for bus in self.buses:
            old_sda, old_scl = bus['levels']
            sda, scl = self._level(bus, bus['sda']), self._level(bus, bus['scl'])
            bus['levels'] = (sda, scl)
            for chip in bus['chips']:
                if old_scl and scl and old_sda and not sda:
                    chip.start()
                elif old_scl and scl and not old_sda and sda:
                    chip.stop()
                elif not old_scl and scl:
                    chip.clock_rising(sda)
                elif old_scl and not scl:
                    chip.clock_falling()
            bus['levels'] = (self._level(bus, bus['sda']), scl)

    def input(self, pin):
        for bus in self.buses:
            if pin in (bus['sda'], bus['scl']):
                if pin == bus['scl']:
                    for chip in bus['chips']:
                        if chip.stretch > 0 and pin not in self.low:
                            chip.stretch -= 1
                    self._update()
                return self._level(bus, pin)
        return 1


@pytest.fixture
def wires():
    return FakeWireGPIO()


def wire_pi(gpio):
    return Brain(gpio_loader=lambda: gpio, reserved=reserved_for_tests())


def reserved_for_tests():
    return {2: "I2C (SDA)", 3: "I2C (SCL)", 5: "the start button"}


def test_software_bus_writes_and_reads_registers(wires):
    chip = FakeChip(0x48)
    wires.add_bus(23, 24, [chip])
    device = wire_pi(wires).i2c_bus(23, 24).device(0x48)
    device.write(0x10, [0xA5, 0x3C])
    assert chip.registers == {0x10: 0xA5, 0x11: 0x3C}
    assert device.read(0x10, 2) == b"\xa5\x3c"
    assert device.read(0x11)[0] == 0x3C


def test_software_bus_never_drives_a_line_high(wires):
    chip = FakeChip(0x48)
    wires.add_bus(23, 24, [chip])
    device = wire_pi(wires).i2c_bus(23, 24).device(0x48)
    device.write(0x00, [0xFF, 0x00])
    device.read(0x00, 2)
    assert not wires.drove_high


def test_software_bus_scan(wires):
    wires.add_bus(23, 24, [FakeChip(0x40), FakeChip(0x68)])
    assert wire_pi(wires).i2c_bus(23, 24).scan() == [0x40, 0x68]


def test_software_bus_names_its_own_pins_in_errors(wires):
    wires.add_bus(23, 24, [])
    with pytest.raises(I2CError, match="none were found.*GPIO 23.*SCL is on GPIO 24"):
        wire_pi(wires).i2c_bus(23, 24).device(0x40).read(0)


def test_software_bus_wrong_address_names_devices_found(wires):
    wires.add_bus(23, 24, [FakeChip(0x40)])
    with pytest.raises(I2CError, match="0x41, but these did: 0x40"):
        wire_pi(wires).i2c_bus(23, 24).device(0x41).write(0, 0)


def test_software_bus_waits_for_clock_stretching(wires):
    chip = FakeChip(0x48, stretch=5)
    wires.add_bus(23, 24, [chip])
    device = wire_pi(wires).i2c_bus(23, 24).device(0x48)
    device.write(0x01, 0x42)
    assert chip.registers == {0x01: 0x42}


def test_software_bus_clock_held_low_is_an_error(wires, monkeypatch):
    monkeypatch.setattr('sr.robot3.i2c.CLOCK_STRETCH_TIMEOUT_NS', 1000)
    chip = FakeChip(0x48, stretch=10 ** 9)
    wires.add_bus(23, 24, [chip])
    with pytest.raises(I2CError, match="bus failed.*GPIO 23"):
        wire_pi(wires).i2c_bus(23, 24).device(0x48).read(0)


def test_software_bus_frees_a_chip_left_mid_byte(wires):
    """Code killed during a read leaves the chip holding SDA low."""
    chip = FakeChip(0x48)
    chip.registers[0] = 0x00
    wires.add_bus(23, 24, [chip])
    device = wire_pi(wires).i2c_bus(23, 24).device(0x48)
    chip.state, chip.byte, chip.bits, chip.holding_sda = 'send', 0x00, 1, True
    wires.buses[0]['levels'] = (0, 1)  # It took SDA while SCL was low, so it isn't a start.
    assert wires.input(23) == 0
    device.write(0x01, 0x42)
    assert chip.registers[0x01] == 0x42
    assert not chip.holding_sda


def test_software_bus_sda_stuck_low_is_an_error(wires):
    chip = FakeChip(0x48)
    wires.add_bus(23, 24, [chip])
    device = wire_pi(wires).i2c_bus(23, 24).device(0x48)
    chip.clock_falling = lambda: None
    chip.stop = lambda: None
    chip.holding_sda = True
    wires.buses[0]['levels'] = (0, 1)
    with pytest.raises(I2CError, match="SDA is being held low"):
        device.read(0)


def test_servo_driver_with_pins_makes_its_own_bus(wires, monkeypatch):
    monkeypatch.setattr(atexit, 'register', lambda fn: None)
    chip = FakeChip(0x40)
    wires.add_bus(23, 24, [chip])
    driver = wire_pi(wires).servo_driver(sda=23, scl=24)
    driver.servos[3].position = 1
    assert chip.registers[0xFE] == 121
    assert chip.registers[0x00] == 0x20
    off = chip.registers[0x06 + 12 + 2] | chip.registers[0x06 + 12 + 3] << 8
    assert 405 <= off <= 415  # 2000us


def test_same_address_on_two_buses_stays_separate(wires, monkeypatch):
    monkeypatch.setattr(atexit, 'register', lambda fn: None)
    first, second = FakeChip(0x40), FakeChip(0x40)
    wires.add_bus(23, 24, [first])
    wires.add_bus(17, 27, [second])
    pi = wire_pi(wires)
    pi.servo_driver(sda=23, scl=24).servos[0].position = 1
    pi.servo_driver(sda=17, scl=27).servos[0].position = -1
    assert first.registers[0x08] != second.registers[0x08]
    assert pi.servo_driver(sda=23, scl=24) is pi.servo_driver(sda=23, scl=24)
    assert pi.servo_driver(sda=23, scl=24) is not pi.servo_driver(sda=17, scl=27)


def test_same_pins_give_the_same_bus(wires):
    wires.add_bus(23, 24, [])
    pi = wire_pi(wires)
    assert pi.i2c_bus(23, 24) is pi.i2c_bus(23, 24)


def test_gpio_2_and_3_are_the_hardware_bus(wires):
    pi = wire_pi(wires)
    assert type(pi.i2c_bus(2, 3)) is I2CBus


def test_device_shares_the_bus_of_a_servo_driver_on_its_pins(wires, monkeypatch):
    monkeypatch.setattr(atexit, 'register', lambda fn: None)
    driver_chip, sensor = FakeChip(0x40), FakeChip(0x48)
    wires.add_bus(23, 24, [driver_chip, sensor])
    pi = wire_pi(wires)
    pi.servo_driver(sda=23, scl=24).servos[0].position = 1
    bus = pi.i2c_bus(sda=23, scl=24)
    assert sorted(bus.scan()) == [0x40, 0x48]
    bus.device(0x48).write(0x01, [0x10])
    assert sensor.registers[0x01] == 0x10


def test_device_cant_use_a_servo_drivers_address(wires):
    wires.add_bus(23, 24, [])
    pi = wire_pi(wires)
    pi.servo_driver(sda=23, scl=24)
    with pytest.raises(ValueError, match=(
            "0x40 on the bus on GPIO 23 and 24 is already used by a servo driver")):
        pi.i2c_bus(sda=23, scl=24).device(0x40)


def test_servo_driver_cant_use_a_devices_address(wires):
    wires.add_bus(23, 24, [])
    pi = wire_pi(wires)
    pi.i2c_bus(sda=23, scl=24).device(0x40)
    with pytest.raises(ValueError, match="is already used by an I2C device"):
        pi.servo_driver(sda=23, scl=24)


def test_same_address_is_fine_on_other_pins(wires):
    wires.add_bus(23, 24, [])
    wires.add_bus(17, 27, [])
    pi = wire_pi(wires)
    pi.servo_driver(sda=23, scl=24)
    assert pi.i2c_bus(sda=17, scl=27).device(0x40).address == 0x40


def test_asking_for_the_same_device_again_is_fine(wires):
    wires.add_bus(23, 24, [])
    bus = wire_pi(wires).i2c_bus(sda=23, scl=24)
    assert bus.device(0x48) is bus.device(0x48)


def test_swapped_pins_say_which_bus_uses_them(wires):
    wires.add_bus(23, 24, [])
    pi = wire_pi(wires)
    pi.servo_driver(sda=23, scl=24)
    with pytest.raises(ValueError, match="GPIO 24 is already used by an I2C bus"):
        pi.i2c_bus(sda=24, scl=23)


@pytest.mark.parametrize("sda, scl", [(23, 25), (25, 24), (24, 23)])
def test_buses_sharing_a_pin_collide(wires, sda, scl):
    pi = wire_pi(wires)
    pi.i2c_bus(23, 24)
    expected = r"already used by an I2C bus \(SDA=GPIO 23, SCL=GPIO 24\)"
    with pytest.raises(ValueError, match=expected):
        pi.i2c_bus(sda, scl)


def test_a_failed_bus_claims_no_pins(wires):
    pi = wire_pi(wires)
    pi.i2c_bus(23, 24)
    with pytest.raises(ValueError):
        pi.i2c_bus(25, 24)
    pi.i2c_bus(25, 26)


def test_bus_and_motor_collide_both_ways(wires):
    pi = wire_pi(wires)
    pi.i2c_bus(23, 24)
    with pytest.raises(ValueError, match="already used by an I2C bus"):
        pi.dc_motor(23, 25)
    with pytest.raises(ValueError, match="already used by an I2C bus"):
        pi.stepper_move(24, 25, 1)
    pi.dc_motor(19, 26)
    with pytest.raises(ValueError, match="already used by another motor"):
        pi.i2c_bus(19, 20)


@pytest.mark.parametrize("sda, scl", [(2, 24), (24, 3), (5, 24), (3, 2)])
def test_bus_on_reserved_pins_is_refused(wires, sda, scl):
    with pytest.raises(ValueError, match="is used by"):
        wire_pi(wires).i2c_bus(sda, scl)


@pytest.mark.parametrize("sda, scl", [(23, 23), (99, 24), ("a", 24), ([1], 24), (True, 24)])
def test_bad_bus_pins_are_refused(wires, sda, scl):
    with pytest.raises(ValueError):
        wire_pi(wires).i2c_bus(sda, scl)


@pytest.mark.parametrize("pins", [{'sda': 23}, {'scl': 24}])
def test_servo_driver_needs_both_pins(wires, pins):
    with pytest.raises(TypeError, match="sda|scl"):
        wire_pi(wires).servo_driver(**pins)


def test_bus_pins_are_released_when_code_ends(wires, monkeypatch):
    registered = []
    monkeypatch.setattr(atexit, 'register', registered.append)
    chip = FakeChip(0x40)
    wires.add_bus(23, 24, [chip])
    pi = wire_pi(wires)
    pi.servo_driver(sda=23, scl=24).servos[0].position = 1
    assert len(registered) == 2
    # atexit runs the last one first: the servos stop while the pins still work.
    for function in reversed(registered):
        function()
    assert chip.registers[0xFD] == 0x10
    assert sorted(wires.cleaned) == [23, 24]
