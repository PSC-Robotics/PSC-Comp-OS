import json

import pytest

from sr.robot3 import Colour
from sr.robot3.brain import Brain, reserved_pins
from sr.robot3.leds import LEDS_TOPIC, BrainLED


class FakeMQTT:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False, *, abs_topic=False):
        self.published.append((topic, json.loads(payload)["colours"], abs_topic))


def test_setting_the_colour_sends_it_to_the_status_service() -> None:
    mqtt = FakeMQTT()
    led = BrainLED(mqtt)

    led.colour = (255, 128, 0)
    led.colour = Colour.BLUE

    assert mqtt.published == [
        (LEDS_TOPIC, [[255, 128, 0]], True),
        (LEDS_TOPIC, [[0, 0, 255]], True),
    ]


def test_reading_the_colour_back() -> None:
    led = BrainLED(FakeMQTT())
    assert led.colour == Colour.OFF
    led.colour = Colour.RED
    assert led.colour == Colour.RED
    led.colour = (255, 80, 0)
    assert led.colour == (255, 80, 0)


@pytest.mark.parametrize("colour", [
    (256, 0, 0), (-1, 0, 0), (1, 2), (1, 2, 3, 4), (0.5, 0, 0), "red", 5, None,
])
def test_invalid_colours(colour) -> None:
    led = BrainLED(FakeMQTT())
    with pytest.raises(ValueError, match="red, green, blue"):
        led.colour = colour
    assert led.colour == Colour.OFF


def test_without_status_service_does_not_fail(caplog) -> None:
    led = BrainLED(None)
    led.colour = Colour.RED
    led.colour = Colour.BLUE
    assert led.colour == Colour.BLUE
    assert caplog.text.count("only available on the robot") == 1


def test_brain_has_the_led() -> None:
    led = BrainLED(FakeMQTT())
    assert Brain(gpio_loader=lambda: None, reserved={}, led=led).led is led


def test_robot_only_has_one_led_api() -> None:
    from sr.robot3 import Robot
    assert isinstance(Robot.brain, property)
    assert not hasattr(Robot, 'leds')
    assert not hasattr(Robot, 'raspberry_pi')


def test_reserved_pins_follow_the_status_config(tmp_path) -> None:
    path = tmp_path / "psc-status.toml"

    # Defaults (the Brain Board HAT): the LED chain on GPIO 18 only.
    assert sorted(reserved_pins(path)) == [0, 1, 2, 3, 5, 13, 14, 15, 18]

    path.write_text(
        "[led_strip]\npins = [18, 10]\n[start_button]\npin = 6\n[buzzer]\npin = 12\n")
    pins = reserved_pins(path)
    assert sorted(pins) == [0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 14, 15, 18]
    assert pins[8] == "the LEDs' SPI port"

    path.write_text("[led_strip]\nenabled = false\n")
    assert sorted(reserved_pins(path)) == [0, 1, 2, 3, 5, 13, 14, 15]
    path.write_text("not toml [")
    assert sorted(reserved_pins(path)) == [0, 1, 2, 3, 5, 13, 14, 15, 18]


@pytest.mark.parametrize("pin", [7, 8, 9, 10, 11, 18])
def test_spi_pins_are_reserved_when_the_chain_uses_gpio_10(tmp_path, pin) -> None:
    path = tmp_path / "psc-status.toml"
    path.write_text("[led_strip]\npins = [18, 10]\n")
    with pytest.raises(ValueError, match="the LEDs"):
        Brain(gpio_loader=lambda: None, reserved=reserved_pins(path)).dc_motor(pin, 20)


def test_channels_are_set_separately_as_on_student_robotics() -> None:
    mqtt = FakeMQTT()
    led = BrainLED(mqtt)
    led.r = True
    led.g = True
    assert led.colour == Colour.YELLOW
    assert (led.r, led.g, led.b) == (True, True, False)
    led.r = False
    assert led.colour == Colour.GREEN
    assert [colours for _, colours, _ in mqtt.published] == [
        [[255, 0, 0]], [[255, 255, 0]], [[0, 255, 0]]]


def test_channels_take_a_brightness_too() -> None:
    led = BrainLED(FakeMQTT())
    led.colour = Colour.WHITE
    led.b = 40
    assert led.colour == (255, 255, 40)
    assert led.b == 40
    led.b = 0
    assert led.b is False
    led.g = 255
    assert led.g is True


@pytest.mark.parametrize("value", [256, -1, 0.5, "on", None])
def test_invalid_channels(value) -> None:
    led = BrainLED(FakeMQTT())
    with pytest.raises(ValueError, match="True .on., False .off. or a number from 0 to 255"):
        led.r = value
    assert led.colour == Colour.OFF


def test_kch_leds_point_to_the_brain_led_on_a_psc_robot(monkeypatch) -> None:
    from sr.robot3 import LED_A, kch
    monkeypatch.setattr(kch, "NO_KCH_HAT", True)
    with pytest.raises(RuntimeError, match=r"Use the brain board's LED.*robot\.brain\.led"):
        kch.KCH().leds[LED_A]


def test_student_robotics_led_names_still_import() -> None:
    from sr.robot3 import LED_A, LED_B, LED_C
    assert (LED_A, LED_B, LED_C) == (0, 1, 2)
