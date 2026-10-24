from types import SimpleNamespace

import pytest

from sr.robot3 import bus_servo
from sr.robot3.bus_servo import BusServoAdapter, BusServoError, _packet


class FakeBus:
    """A serial port with simulated Feetech servos behind it."""

    def __init__(self, servos, echo=False, corrupt=False):
        self.servos = servos  # id -> bytearray memory
        self.echo = echo
        self.corrupt = corrupt
        self.out = b""
        self.written = []

    def __call__(self, port, baud, timeout):
        self.port, self.baud = port, baud
        return self

    def reset_input_buffer(self):
        self.out = b""

    def flush(self):
        pass

    def close(self):
        pass

    def write(self, data):
        self.written.append(bytes(data))
        if self.echo:
            self.out += data
        servo_id, instruction, params = data[2], data[4], data[5:-1]
        memory = self.servos.get(servo_id)
        if memory is None or servo_id == 0xFE:
            return
        reply_params = b""
        if instruction == 0x02:
            address, length = params
            reply_params = bytes(memory[address:address + length])
        elif instruction == 0x03:
            address = params[0]
            memory[address:address + len(params) - 1] = params[1:]
            if address == 5:  # the servo's ID changed
                self.servos[params[1]] = self.servos.pop(servo_id)
        body = bytes([servo_id, len(reply_params) + 2, 0]) + reply_params
        checksum = (~sum(body)) & 0xFF
        if self.corrupt:
            checksum ^= 0xFF
        self.out += b"\xff\xff" + body + bytes([checksum])

    def read(self, size):
        data, self.out = self.out[:size], self.out[size:]
        return data


def servo_memory(position=0, little_endian=True, voltage=74, temperature=31):
    memory = bytearray(80)
    memory[56:58] = position.to_bytes(2, "little" if little_endian else "big")
    memory[62], memory[63] = voltage, temperature
    return memory


def make_adapter(bus):
    return BusServoAdapter("test", serial_factory=bus, timeout=0.01)


def test_packets_match_the_feetech_protocol() -> None:
    # Ping servo 1: FF FF ID LEN INSTR CHECKSUM
    assert _packet(1, 0x01) == bytes.fromhex("ff ff 01 02 01 fb")


def test_ping_and_scan() -> None:
    bus = FakeBus({1: servo_memory(), 3: servo_memory()})
    adapter = make_adapter(bus)
    assert bus.baud == 1_000_000
    assert adapter.ping(1) and not adapter.ping(2)
    assert adapter.scan(range(1, 6)) == [1, 3]


@pytest.mark.parametrize("echo", [False, True])
def test_sts_move_writes_like_the_sdk(echo) -> None:
    bus = FakeBus({1: servo_memory()}, echo=echo)
    servo = make_adapter(bus).servos[1]

    servo.move(2048, speed=1000, acceleration=50)

    # WritePosEx: at address 41, [acc, pos L, pos H, time 0, 0, speed L, speed H]
    assert bus.written[-1] == _packet(1, 0x03, bytes([41, 50, 0x00, 0x08, 0, 0, 0xE8, 0x03]))


def test_scs_move_is_big_endian() -> None:
    bus = FakeBus({2: servo_memory(little_endian=False)})
    servo = make_adapter(bus).servo(2, series="scs")

    servo.position = 512

    # WritePos: at address 42, [pos H, pos L, time H, time L, speed H, speed L]
    assert bus.written[-1] == _packet(2, 0x03, bytes([42, 0x02, 0x00, 0, 0, 0, 0]))


def test_angles_convert_to_positions() -> None:
    bus = FakeBus({
        1: servo_memory(position=1024),
        2: servo_memory(position=512, little_endian=False),
    })
    adapter = make_adapter(bus)
    assert adapter.servos[1].angle == pytest.approx(90.0)
    assert adapter.servo(2, series="scs").angle == pytest.approx(150.0)

    adapter.servos[1].angle = 180
    assert bus.written[-1][7:9] == (2048).to_bytes(2, "little")
    adapter.servos[1].angle = 360  # the top of the range is the last step
    assert bus.written[-1][7:9] == (4095).to_bytes(2, "little")


def test_readings() -> None:
    bus = FakeBus({1: servo_memory(position=4000, voltage=74, temperature=31)})
    servo = make_adapter(bus).servos[1]
    assert servo.position == 4000
    assert servo.voltage == pytest.approx(7.4)
    assert servo.temperature == 31
    bus.servos[1][56:58] = (0x8000 | 5).to_bytes(2, "little")  # sign bit, as the SDK
    assert servo.position == -5


def test_torque() -> None:
    bus = FakeBus({1: servo_memory()})
    servo = make_adapter(bus).servos[1]
    servo.torque = True
    assert servo.torque is True
    servo.torque = False
    assert bus.written[-1] == _packet(1, 0x03, bytes([40, 0]))


def test_change_id_unlocks_writes_and_relocks() -> None:
    bus = FakeBus({1: servo_memory()})
    adapter = make_adapter(bus)
    servo = adapter.servos[1]

    servo.change_id(7)

    assert bus.written[-3:] == [
        _packet(1, 0x03, bytes([55, 0])),
        _packet(1, 0x03, bytes([5, 7])),
        _packet(7, 0x03, bytes([55, 1])),
    ]
    assert servo.id == 7 and adapter.servos[7] is servo and adapter.ping(7)


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda a: a.servos[1].position, "No reply from bus servo 1"),
        (lambda a: a.servos[1].move(5000), "position must be"),
        (lambda a: a.servos[1].move(angle=361), "angle must be"),
        (lambda a: a.servos[1].move(), "either a position or an angle"),
        (lambda a: a.servo(1, series="pwm"), "series"),
        (lambda a: a.servos[1].change_id(0), "new_id"),
    ],
)
def test_errors_are_explained(call, message) -> None:
    with pytest.raises((BusServoError, ValueError), match=message):
        call(make_adapter(FakeBus({})))


def test_corrupted_reply_is_detected() -> None:
    with pytest.raises(BusServoError, match="Corrupted"):
        make_adapter(FakeBus({1: servo_memory()}, corrupt=True)).servos[1].position


def test_discovery_usb_and_gpio(monkeypatch, tmp_path) -> None:
    usb_bus, gpio_bus = FakeBus({1: servo_memory()}), FakeBus({4: servo_memory()})
    ports = [
        SimpleNamespace(vid=0x1A86, pid=0x55D3, device="/dev/ttyACM0",
                        serial_number="5B90147558", manufacturer="WCH",
                        product="USB Single Serial"),
        SimpleNamespace(vid=0x2341, pid=0x0043, device="/dev/ttyACM1", serial_number="ARDUINO",
                        manufacturer="Arduino", product="Uno"),
    ]
    monkeypatch.setattr(bus_servo, "comports", lambda: ports)
    gpio = tmp_path / "serial0"
    gpio.touch()
    monkeypatch.setattr(bus_servo, "GPIO_UART", gpio)

    def open_port(port, baud, timeout):
        return (gpio_bus if port == str(gpio) else usb_bus)(port, baud, timeout)

    monkeypatch.setattr(BusServoAdapter.__init__, "__defaults__", (None, open_port, 0.01))

    adapters = BusServoAdapter._get_supported_boards()

    assert sorted(adapters) == ["5B90147558", "gpio"]
    assert adapters["gpio"].scan(range(1, 6)) == [4]


def test_gpio_uart_ignored_without_servos(monkeypatch, tmp_path) -> None:
    gpio = tmp_path / "serial0"
    gpio.touch()
    monkeypatch.setattr(bus_servo, "comports", lambda: [])
    monkeypatch.setattr(bus_servo, "GPIO_UART", gpio)
    empty = FakeBus({})
    monkeypatch.setattr(BusServoAdapter.__init__, "__defaults__", (None, empty, 0.01))
    assert dict(BusServoAdapter._get_supported_boards()) == {}
