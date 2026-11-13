import json
from pathlib import Path

import pytest

from sr.robot3 import INPUT, INPUT_PULLUP, OUTPUT
from sr.robot3 import pin_motors
from sr.robot3.brain import Brain, reserved_pins

DEFAULT_RESERVED = reserved_pins(Path('/nonexistent/psc-status.toml'))


class FakeGPIO:
    """RPi.GPIO: pins' modes, pulls and levels, with inputs read from ``levels``."""
    BCM, IN, OUT, LOW, HIGH = "BCM", "IN", "OUT", 0, 1
    PUD_OFF, PUD_UP, PUD_DOWN = "PUD_OFF", "PUD_UP", "PUD_DOWN"

    def __init__(self):
        self.modes, self.pulls, self.levels = {}, {}, {}
        self.cleaned = None

    def setwarnings(self, flag):
        pass

    def setmode(self, mode):
        assert mode == self.BCM

    def setup(self, pin, mode, initial=None, pull_up_down=None):
        self.modes[pin] = mode
        if mode == self.OUT:
            self.levels[pin] = self.LOW if initial is None else initial
        else:
            self.pulls[pin] = pull_up_down

    def output(self, pin, value):
        assert self.modes[pin] == self.OUT, "output() on an input"
        self.levels[pin] = value

    def input(self, pin):
        assert pin in self.modes, "input() on a pin that isn't set up"
        if self.modes[pin] == self.IN and pin not in self.levels:
            return self.HIGH if self.pulls.get(pin) == self.PUD_UP else self.LOW
        return self.levels.get(pin, self.LOW)

    def PWM(self, pin, frequency):  # noqa: N802
        gpio = self

        class PWM:
            def start(self, duty):
                pass

            def ChangeDutyCycle(self, duty):  # noqa: N802
                gpio.levels[pin] = duty

            def stop(self):
                pass
        return PWM()

    def cleanup(self, pins):
        self.cleaned = pins


@pytest.fixture
def brain():
    gpio = FakeGPIO()
    return Brain(gpio_loader=lambda: gpio, reserved=DEFAULT_RESERVED), gpio


@pytest.fixture
def announced(monkeypatch):
    messages = []
    monkeypatch.setattr(pin_motors, "_driven_pins", [])
    monkeypatch.setattr(pin_motors, "_announcer", messages.append)
    return messages


def test_output_starts_low_and_sets_high_and_low(brain, announced):
    pi, gpio = brain
    pin = pi.pins[17]
    pin.mode = OUTPUT
    assert gpio.modes[17] == gpio.OUT and gpio.levels[17] == gpio.LOW
    pin.digital_write(True)
    assert gpio.levels[17] == gpio.HIGH
    assert pin.digital_read() is True
    pin.digital_write(False)
    assert gpio.levels[17] == gpio.LOW
    # The status service makes it an input again if the code is killed
    assert json.loads(announced[-1]) == {"pi": [17]}


def test_inputs_and_pull_up(brain):
    pi, gpio = brain
    pi.pins[22].mode = INPUT_PULLUP
    assert gpio.pulls[22] == gpio.PUD_UP
    assert pi.pins[22].digital_read() is True  # nothing pulling it down
    gpio.levels[22] = gpio.LOW  # a button pressed to GND
    assert pi.pins[22].digital_read() is False
    pi.pins[6].mode = INPUT
    assert gpio.pulls[6] == gpio.PUD_OFF


def test_pins_start_as_inputs_and_can_be_read(brain):
    pi, gpio = brain
    assert pi.pins[23].mode == INPUT
    gpio.levels[23] = gpio.HIGH
    assert pi.pins[23].digital_read() is True
    assert gpio.modes[23] == gpio.IN


def test_writing_needs_an_output(brain):
    pi, _ = brain
    with pytest.raises(IOError, match=r"set robot.brain.pins\[24\].mode = OUTPUT"):
        pi.pins[24].digital_write(True)
    with pytest.raises(IOError, match="GPIOPinMode"):
        pi.pins[24].mode = "OUTPUT"


@pytest.mark.parametrize(("pin", "hint"), [
    (13, r"the buzzer: use robot\.buzzer\.buzz\(\)"),
    (5, r"the start button: use robot\.wait_start\(\)"),
    (18, r"the LEDs \(WS2812 data\): use robot\.brain\.led"),
    (2, r"I2C \(SDA\), with the battery sensor"),
    (0, "the HAT's EEPROM"),
    (14, "the serial port"),
])
def test_the_robots_own_pins_cant_be_used(brain, pin, hint):
    pi, gpio = brain
    with pytest.raises(ValueError, match=f"GPIO {pin} is used by {hint}"):
        pi.pins[pin]
    assert not gpio.modes


@pytest.mark.parametrize("pin", [-1, 28, 40, 4.0, True, "17"])
def test_only_gpio_numbers(brain, pin):
    pi, _ = brain
    with pytest.raises(ValueError, match="BCM GPIO number"):
        pi.pins[pin]


def test_every_other_pin_can_be_used(brain):
    """Not overly restrictive: every GPIO the robot doesn't use, including the motor pads."""
    pi, gpio = brain
    usable = [pin for pin in range(28) if pin not in DEFAULT_RESERVED]
    assert usable == [4, 6, 7, 8, 9, 10, 11, 12, 16, 17, 19, 20, 21, 22, 23, 24, 25, 26, 27]
    for pin in usable:
        pi.pins[pin].mode = OUTPUT
        pi.pins[pin].digital_write(True)
    assert all(gpio.levels[pin] == gpio.HIGH for pin in usable)


def test_a_pin_belongs_to_a_motor_or_the_pins_not_both(brain):
    pi, _ = brain
    pi.dc_motor(20, 21)
    with pytest.raises(ValueError, match="GPIO 20 is already used by another motor"):
        pi.pins[20].mode = OUTPUT
    with pytest.raises(ValueError, match="already used by another motor"):
        pi.pins[21].digital_read()
    pi.pins[19].mode = OUTPUT
    expected = r"GPIO 19 is already used by robot\.brain\.pins\[19\]"
    with pytest.raises(ValueError, match=expected):
        pi.dc_motor(19, 4)


def test_changing_mode_on_the_same_pin(brain):
    pi, gpio = brain
    pin = pi.pins[16]
    pin.mode = OUTPUT
    pin.digital_write(True)
    pin.mode = INPUT
    assert gpio.modes[16] == gpio.IN
    pin.mode = OUTPUT
    assert gpio.levels[16] == gpio.LOW  # starts low again
    assert pi.pins[16] is pin


def test_pins_are_released_at_exit(brain):
    pi, gpio = brain
    pi.pins[12].mode = OUTPUT
    pi.pins[25].mode = INPUT_PULLUP
    pi._cleanup()
    assert gpio.cleaned == [12, 25]


def test_repr(brain):
    pi, _ = brain
    pi.pins[26].mode = OUTPUT
    assert repr(pi.pins[26]) == "<BrainPin GPIO 26 mode=OUTPUT>"
