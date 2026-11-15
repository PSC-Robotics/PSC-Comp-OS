import json
import warnings
from types import SimpleNamespace

import pytest

from sr.robot3 import start_button as start_button_module
from sr.robot3.brain import Brain
from sr.robot3.buzzer import Buzzer
from sr.robot3.start_button import BUTTON_TOPIC, BrainStartButton

BOOT = "boot-1"


class FakeMQTT:
    def __init__(self):
        self.callbacks = {}

    def subscribe(self, topic, callback, abs_path=False):
        assert abs_path
        self.callbacks[topic] = callback

    def deliver(self, pressed, presses, at, boot_id=BOOT):
        payload = json.dumps({"pressed": pressed, "presses": presses, "time": at,
                              "boot_id": boot_id}).encode()
        self.callbacks[BUTTON_TOPIC](None, None, SimpleNamespace(payload=payload))


class Clock:
    now = 100.0

    def __call__(self):
        return self.now


@pytest.fixture
def button():
    mqtt, clock = FakeMQTT(), Clock()
    return BrainStartButton(mqtt, clock=clock, boot_id=BOOT), mqtt, clock


def test_is_pressed_follows_the_button(button):
    start, mqtt, clock = button
    mqtt.deliver(False, 3, clock.now)
    assert start.is_pressed is False
    mqtt.deliver(True, 4, clock.now)
    assert start.is_pressed is True


def test_was_pressed_counts_presses_since_last_asked(button):
    start, mqtt, clock = button
    mqtt.deliver(False, 3, clock.now)  # presses before the code was ready
    assert start.was_pressed() is False
    mqtt.deliver(True, 4, clock.now)
    mqtt.deliver(False, 4, clock.now)  # a quick tap, released before the code asked
    assert start.was_pressed() is True
    assert start.was_pressed() is False
    mqtt.deliver(False, 6, clock.now)  # two presses: True once
    assert start.was_pressed() is True
    assert start.was_pressed() is False


def test_the_press_that_started_the_code_doesnt_count(button):
    start, mqtt, clock = button
    mqtt.deliver(False, 0, clock.now)
    mqtt.deliver(False, 1, clock.now)  # the start press
    start._forget_presses()  # as robot.wait_start() does
    assert start.was_pressed() is False


def test_a_restarted_status_service_starts_counting_again(button):
    start, mqtt, clock = button
    mqtt.deliver(False, 5, clock.now)
    mqtt.deliver(False, 0, clock.now)  # psc-statusd restarted
    mqtt.deliver(False, 1, clock.now)
    assert start.was_pressed() is True


def test_an_out_of_date_state_reads_as_not_pressed(button, caplog):
    start, mqtt, clock = button
    mqtt.deliver(True, 1, clock.now - 60)  # left retained by a stopped psc-statusd
    assert start.is_pressed is False
    assert start.was_pressed() is False
    mqtt.deliver(True, 1, clock.now, boot_id="boot-0")  # from an earlier boot
    assert start.is_pressed is False
    assert caplog.text.count("systemctl status psc-statusd") == 1


def test_no_state_at_all(button):
    start, _, _ = button
    assert start.is_pressed is False
    assert start.was_pressed() is False


def test_malformed_messages_are_ignored(button):
    start, mqtt, clock = button
    mqtt.deliver(True, 2, clock.now)
    mqtt.callbacks[BUTTON_TOPIC](None, None, SimpleNamespace(payload=b"nonsense"))
    mqtt.callbacks[BUTTON_TOPIC](None, None, SimpleNamespace(payload=b'{"pressed": true}'))
    assert start.is_pressed is True


def test_off_the_robot(caplog):
    start = BrainStartButton(None)
    assert start.is_pressed is False
    assert "only available on the robot" in caplog.text


def test_brain_has_the_buzzer_and_start_button(button):
    start, _, _ = button
    buzzer = Buzzer(None, lambda _: None)
    brain = Brain(gpio_loader=lambda: None, reserved={}, buzzer=buzzer, start_button=start)
    assert brain.buzzer is buzzer and brain.start_button is start


def test_stepper_move_is_deprecated_but_works(monkeypatch):
    brain = Brain(gpio_loader=lambda: None, reserved={})
    calls = []
    monkeypatch.setattr(Brain, "_stepper_move", lambda self, *a, **k: calls.append((a, k)))
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        brain.stepper_move(23, 24, 200, direction=False)
    assert calls == [((23, 24, 200), {"direction": False, "step_delay_us": 1000})]
    assert "robot.brain.stepper(23, 24).move(-200)" in str(caught[0].message)
    assert caught[0].category is DeprecationWarning


def test_state_older_than_five_seconds_is_out_of_date():
    assert start_button_module.STALE_AFTER_S == 5.0
