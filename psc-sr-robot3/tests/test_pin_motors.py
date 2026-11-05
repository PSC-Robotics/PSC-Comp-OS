import atexit
import json
import types
from pathlib import Path

import pytest

from sr.robot3 import BRAKE, COAST, pin_motors
from sr.robot3.arduino import Arduino
from sr.robot3.brain import Brain, reserved_pins
from sr.robot3.utils import BoardIdentity

from .conftest import MockSerialWrapper

# The pins the robot uses with the status service's default settings.
DEFAULT_RESERVED = reserved_pins(Path('/nonexistent/psc-status.toml'))


class FakePWM:
    def __init__(self, gpio, pin, frequency):
        self.gpio, self.pin, self.frequency = gpio, pin, frequency
        self.stopped = False

    def start(self, duty):
        self.gpio.duties[self.pin] = duty

    def ChangeDutyCycle(self, duty):
        self.gpio.duties[self.pin] = duty

    def stop(self):
        self.stopped = True


class FakeGPIO(types.ModuleType):
    BCM, OUT, HIGH, LOW = "BCM", "OUT", 1, 0

    def __init__(self):
        super().__init__("RPi.GPIO")
        self.duties = {}
        self.pwms = {}
        self.outputs = []
        self.cleaned = None

    def setwarnings(self, flag):
        pass

    def setmode(self, mode):
        pass

    def setup(self, pin, mode, initial):
        pass

    def output(self, pin, value):
        self.outputs.append((pin, value))

    def PWM(self, pin, frequency):
        assert pin not in self.pwms, "RPi.GPIO allows one PWM object per pin"
        self.pwms[pin] = FakePWM(self, pin, frequency)
        return self.pwms[pin]

    def cleanup(self, pins):
        self.cleaned = pins


class Clock:
    def __init__(self):
        self.now = 0

    def __call__(self):
        self.now += 1_000
        return self.now


@pytest.fixture
def pi(monkeypatch):
    monkeypatch.setattr("sr.robot3.brain.time.sleep", lambda secs: None)
    gpio = FakeGPIO()
    pi = Brain(gpio_loader=lambda: gpio, clock_ns=Clock(), reserved=DEFAULT_RESERVED)
    return pi, gpio


@pytest.fixture
def announced():
    messages = []
    pin_motors.set_announcer(messages.append)
    yield messages
    pin_motors.set_announcer(None)


@pytest.mark.parametrize(
    ("power", "duties"),
    [
        (1, (100, 0)),
        (0.25, (100, 75)),
        (-0.25, (75, 100)),
        (-1, (0, 100)),
        (BRAKE, (100, 100)),
        (COAST, (0, 0)),
    ],
)
def test_pi_dc_motor_drives_drv8871_inputs(pi, power, duties) -> None:
    brain, gpio = pi
    motor = brain.dc_motor(23, 24)
    assert (gpio.duties[23], gpio.duties[24]) == (0, 0)  # starts coasting

    motor.power = power

    assert (gpio.duties[23], gpio.duties[24]) == pytest.approx(duties)
    assert motor.power == power


def test_pi_supports_several_dc_motors(pi) -> None:
    brain, gpio = pi
    left, right = brain.dc_motor(23, 24), brain.dc_motor(25, 26)
    left.power, right.power = 0.5, -0.5
    assert gpio.duties == {23: 100, 24: 50, 25: 50, 26: 100}
    assert brain.dc_motor(23, 24) is left  # the same pins give the same motor


@pytest.mark.parametrize(
    ("pins", "message"),
    [((23, 23), "different"), ((5, 24), "start button"), ((14, 24), "serial port"),
     ((23, 28), "BCM GPIO number")],
)
def test_pi_dc_motor_rejects_invalid_pins(pi, pins, message) -> None:
    with pytest.raises(ValueError, match=message):
        pi[0].dc_motor(*pins)


@pytest.mark.parametrize("power", [1.5, -1.01, float("nan"), "fast", True])
def test_dc_motor_rejects_invalid_power(pi, power) -> None:
    motor = pi[0].dc_motor(23, 24)
    with pytest.raises(ValueError, match="power must be"):
        motor.power = power


def test_pi_motors_cannot_share_pins(pi) -> None:
    brain, _ = pi
    brain.dc_motor(23, 24)
    with pytest.raises(ValueError, match="already used"):
        brain.dc_motor(24, 25)
    with pytest.raises(ValueError, match="already used"):
        brain.stepper(23, 26)
    with pytest.raises(ValueError, match="already used"):
        brain.stepper_move(23, 26, 1)


def test_pi_dc_motors_stop_and_are_announced(pi, announced) -> None:
    brain, gpio = pi
    assert json.loads(announced[-1]) == {"pi": []}
    brain.dc_motor(23, 24).power = 1
    brain.dc_motor(6, 12)
    assert json.loads(announced[-1]) == {"pi": [6, 12, 23, 24]}

    brain._cleanup()

    assert all(pwm.stopped for pwm in gpio.pwms.values())
    assert gpio.cleaned == [6, 12, 23, 24]


def test_pi_stepper_moves_both_ways_and_counts_position(pi) -> None:
    brain, gpio = pi
    stepper = brain.stepper(23, 24, step_delay_us=500)

    stepper.move(3)
    stepper.move(-5)

    direction = [value for pin, value in gpio.outputs if pin == 24]
    steps = [value for pin, value in gpio.outputs if pin == 23]
    assert direction == [1, 0]
    assert steps.count(1) == 8
    assert stepper.position == -2

    stepper.move_to(1)
    assert stepper.position == 1
    stepper.position = 0
    stepper.move(0)  # nothing to do
    assert stepper.position == 0
    assert brain.stepper(23, 24) is stepper
    assert stepper.step_delay_us == 500


@pytest.mark.parametrize(
    ("call", "message"),
    [
        (lambda s: s.move(1.5), "steps must be"),
        (lambda s: s.move(1, step_delay_us=10), "step_delay_us must be"),
        (lambda s: setattr(s, "step_delay_us", 2_000_000), "step_delay_us must be"),
        (lambda s: setattr(s, "position", 1.0), "position must be"),
    ],
)
def test_stepper_rejects_invalid_arguments(pi, call, message) -> None:
    with pytest.raises(ValueError, match=message):
        call(pi[0].stepper(23, 24))


def make_arduino(monkeypatch, version):
    responses = [("v", f"SRcustom:{version}")]
    if version >= 5:
        responses.append(("m", "5000"))
    serial = MockSerialWrapper(responses)
    monkeypatch.setattr("sr.robot3.arduino.SerialWrapper", serial)
    return Arduino("test://", initial_identity=BoardIdentity(asset_tag="TEST")), serial


def test_arduino_dc_motor_sets_pwm(monkeypatch) -> None:
    arduino, serial = make_arduino(monkeypatch, 6)
    serial._add_responses([
        ("wj000", "OK"), ("wk000", "OK"),  # pins 9 and 10 start coasting
        ("wj255", "OK"), ("wk128", "OK"),  # power 0.5
        ("wj191", "OK"), ("wk255", "OK"),  # power -0.25
        ("wj255", "OK"), ("wk255", "OK"),  # BRAKE
        ("wj000", "OK"), ("wk000", "OK"),  # COAST when the code ends
    ])

    motor = arduino.dc_motor(9, 10)
    motor.power = 0.5
    motor.power = -0.25
    motor.power = BRAKE
    assert arduino.dc_motor(9, 10) is motor
    arduino._stop_dc_motors()
    atexit.unregister(arduino._stop_dc_motors)

    assert serial.request_index == len(serial.responses)


def test_arduino_dc_motor_needs_firmware_6(monkeypatch) -> None:
    arduino, _ = make_arduino(monkeypatch, 5)
    with pytest.raises(RuntimeError, match="firmware version 6"):
        arduino.dc_motor(9, 10)
    with pytest.raises(ValueError, match="Invalid in1 pin"):
        arduino.dc_motor(1, 10)


def test_arduino_stepper_splits_long_moves(monkeypatch) -> None:
    arduino, serial = make_arduino(monkeypatch, 6)
    # 1000us steps: 350000 // 1100 = 318 steps per command
    serial._add_responses([
        ("sef10031801000", "OK"), ("sef10031801000", "OK"), ("sef10006401000", "OK"),
        ("sef00000500002", "OK"),
    ])

    stepper = arduino.stepper(4, 5)
    stepper.move(700)
    stepper.move(-5, step_delay_us=2)

    assert stepper.position == 695
    assert serial.request_index == len(serial.responses)
    with pytest.raises(ValueError, match="already used"):
        arduino.dc_motor(5, 6)
