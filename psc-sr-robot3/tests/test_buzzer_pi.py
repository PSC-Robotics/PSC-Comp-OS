import json
import types
from pathlib import Path

import pytest

from sr.robot3 import Note
from sr.robot3.buzzer import BUZZER_TOPIC, Buzzer
from sr.robot3.brain import Brain, reserved_pins

# The pins the robot uses with the status service's default settings.
DEFAULT_RESERVED = reserved_pins(Path('/nonexistent/psc-status.toml'))


class FakeMQTT:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False, *, abs_topic=False):
        self.published.append((topic, json.loads(payload), abs_topic))


def test_buzz_sends_tone_to_status_service() -> None:
    mqtt, sleeps = FakeMQTT(), []
    buzzer = Buzzer(mqtt, sleeps.append)

    buzzer.buzz(Note.A6, 0.5)
    buzzer.buzz(440, 1, blocking=True)

    assert mqtt.published == [
        (BUZZER_TOPIC, {"frequency": 1760, "duration": 0.5}, True),
        (BUZZER_TOPIC, {"frequency": 440, "duration": 1.0}, True),
    ]
    assert sleeps == [1.0]


@pytest.mark.parametrize(
    ("frequency", "duration"), [(7, 1), (10_001, 1), (440, -1), (440, 61)],
)
def test_buzz_rejects_out_of_range(frequency, duration) -> None:
    with pytest.raises(ValueError):
        Buzzer(FakeMQTT(), lambda _: None).buzz(frequency, duration)


def test_buzz_volume() -> None:
    mqtt = FakeMQTT()
    buzzer = Buzzer(mqtt, lambda _: None)

    buzzer.buzz(440, 0.5, volume=10)

    assert mqtt.published == [
        (BUZZER_TOPIC, {"frequency": 440, "duration": 0.5, "volume": 10.0}, True),
    ]
    with pytest.raises(ValueError, match="Volume"):
        buzzer.buzz(440, 0.5, volume=51)


def test_buzz_without_status_service_does_not_fail() -> None:
    Buzzer(None, lambda _: None).buzz(440, 0.1)


class FakeGPIO(types.ModuleType):
    BCM, OUT, HIGH, LOW = "BCM", "OUT", 1, 0

    def __init__(self, clock):
        super().__init__("RPi.GPIO")
        self.clock = clock
        self.events = []
        self.setup_pins = []
        self.cleaned = None

    def setwarnings(self, flag):
        pass

    def setmode(self, mode):
        assert mode == self.BCM

    def setup(self, pin, mode, initial):
        self.setup_pins.append((pin, mode, initial))

    def output(self, pin, value):
        self.events.append((self.clock.now, pin, value))

    def cleanup(self, pins):
        self.cleaned = pins


class FakeClock:
    """A clock that advances 1us each time it is read, like a busy-wait loop."""

    def __init__(self):
        self.now = 0

    def __call__(self):
        self.now += 1_000
        return self.now


def make_pi():
    clock = FakeClock()
    gpio = FakeGPIO(clock)
    pi = Brain(gpio_loader=lambda: gpio, clock_ns=clock, reserved=DEFAULT_RESERVED)
    return pi, gpio


def test_stepper_pulses_step_pin_with_direction_and_spacing(monkeypatch) -> None:
    monkeypatch.setattr("sr.robot3.brain.time.sleep", lambda secs: None)
    pi, gpio = make_pi()

    pi.stepper_move(23, 24, 3, direction=False, step_delay_us=500)

    assert gpio.setup_pins == [(23, "OUT", 0), (24, "OUT", 0)]
    assert gpio.events[0][1:] == (24, 0)  # direction set low first
    step_events = [(t, v) for t, pin, v in gpio.events if pin == 23]
    assert [v for _, v in step_events] == [1, 0, 1, 0, 1, 0]
    rising = [t for t, v in step_events if v == 1]
    gaps = [b - a for a, b in zip(rising, rising[1:])]
    assert gaps == pytest.approx([500_000] * 2, abs=2_000)
    falling = [t for t, v in step_events if v == 0]
    assert all(4_000 <= f - r <= 8_000 for r, f in zip(rising, falling))


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        (dict(step_pin=13, direction_pin=24, steps=1), "buzzer"),
        (dict(step_pin=23, direction_pin=5, steps=1), "start button"),
        (dict(step_pin=13, direction_pin=24, steps=1), "buzzer"),
        (dict(step_pin=28, direction_pin=24, steps=1), "BCM GPIO number"),
        (dict(step_pin=23, direction_pin=23, steps=1), "different"),
        (dict(step_pin=23, direction_pin=24, steps=0), "steps"),
        (dict(step_pin=23, direction_pin=24, steps=1, step_delay_us=10), "step_delay_us"),
        (dict(step_pin=23, direction_pin=24, steps=1, direction=1), "direction"),
    ],
)
def test_stepper_rejects_invalid_arguments(kwargs, message) -> None:
    pi, gpio = make_pi()
    with pytest.raises(ValueError, match=message):
        pi.stepper_move(**kwargs)
    assert gpio.events == []


def test_stepper_off_the_robot_explains_why() -> None:
    def no_gpio():
        raise RuntimeError("Raspberry Pi GPIO is only available when running on the robot")

    with pytest.raises(RuntimeError, match="only available"):
        Brain(gpio_loader=no_gpio).stepper_move(23, 24, 1)
