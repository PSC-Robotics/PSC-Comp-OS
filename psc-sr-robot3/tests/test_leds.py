import json

import pytest

from sr.robot3 import Colour
from sr.robot3.leds import LEDS_TOPIC, LEDStrip, user_led_count
from sr.robot3.raspberry_pi import RaspberryPi


class FakeMQTT:
    def __init__(self):
        self.published = []

    def publish(self, topic, payload, retain=False, *, abs_topic=False):
        self.published.append((topic, json.loads(payload)["colours"], abs_topic))


def test_setting_an_led_sends_every_colour() -> None:
    mqtt = FakeMQTT()
    leds = LEDStrip(mqtt, 3)

    leds[1] = (255, 128, 0)
    leds[-1] = Colour.BLUE

    assert mqtt.published == [
        (LEDS_TOPIC, [[0, 0, 0], [255, 128, 0], [0, 0, 0]], True),
        (LEDS_TOPIC, [[0, 0, 0], [255, 128, 0], [0, 0, 255]], True),
    ]
    assert leds[1] == (255, 128, 0)
    assert list(leds) == [(0, 0, 0), (255, 128, 0), (0, 0, 255)]
    assert len(leds) == 3


def test_fill_set_all_and_off() -> None:
    mqtt = FakeMQTT()
    leds = LEDStrip(mqtt, 2)

    leds.fill(Colour.RED)
    leds.set_all([(1, 2, 3), (4, 5, 6)])
    leds[0:1] = [(7, 8, 9)]
    leds.off()

    assert [colours for _, colours, _ in mqtt.published] == [
        [[255, 0, 0], [255, 0, 0]],
        [[1, 2, 3], [4, 5, 6]],
        [[7, 8, 9], [4, 5, 6]],
        [[0, 0, 0], [0, 0, 0]],
    ]


@pytest.mark.parametrize("colour", [(256, 0, 0), (-1, 0, 0), (1, 2), (1.5, 0, 0), "red", 5])
def test_invalid_colours(colour) -> None:
    leds = LEDStrip(FakeMQTT(), 1)
    with pytest.raises(ValueError, match="red, green, blue"):
        leds[0] = colour


def test_invalid_indices() -> None:
    leds = LEDStrip(FakeMQTT(), 2)
    with pytest.raises(IndexError, match="0 to 1"):
        leds[2] = Colour.RED
    with pytest.raises(TypeError):
        leds[True] = Colour.RED
    with pytest.raises(ValueError, match="Give 2 colours"):
        leds.set_all([Colour.RED])
    with pytest.raises(IndexError, match="count"):
        LEDStrip(FakeMQTT(), 0)[0] = Colour.RED


def test_without_status_service_does_not_fail() -> None:
    leds = LEDStrip(None, 1)
    leds[0] = Colour.GREEN
    assert leds[0] == (0, 255, 0)


@pytest.mark.parametrize(("config", "expected"), [
    (None, 1),
    ("", 1),
    ("[led_strip]\ncount = 8\n", 7),
    ("[led_strip]\ncount = 8\nstatus_led = false\n", 8),
    ("[led_strip]\ncount = 8\nenabled = false\n", 0),
    ("[led_strip]\ncount = 0\n", 0),
    ("[led_strip]\ncount = -3\n", 0),
    ("not toml [", 1),
])
def test_user_led_count(tmp_path, config, expected) -> None:
    path = tmp_path / "psc-status.toml"
    if config is not None:
        path.write_text(config)
    assert user_led_count(path) == expected


def test_reserved_pins_follow_the_status_config(tmp_path) -> None:
    from sr.robot3.raspberry_pi import reserved_pins
    path = tmp_path / "psc-status.toml"

    # Defaults (the Brain Board HAT): the LED chain on GPIO 18 only.
    assert sorted(reserved_pins(path)) == [0, 1, 2, 3, 5, 13, 14, 15, 18]

    path.write_text(
        "[led_strip]\npins = [18, 10]\n[start_button]\npin = 6\n[buzzer]\npin = 12\n")
    pins = reserved_pins(path)
    assert sorted(pins) == [0, 1, 2, 3, 6, 7, 8, 9, 10, 11, 12, 14, 15, 18]
    assert pins[8] == "the LED strip's SPI port"

    path.write_text("[led_strip]\nenabled = false\n")
    assert sorted(reserved_pins(path)) == [0, 1, 2, 3, 5, 13, 14, 15]
    path.write_text("not toml [")
    assert sorted(reserved_pins(path)) == [0, 1, 2, 3, 5, 13, 14, 15, 18]


@pytest.mark.parametrize("pin", [7, 8, 9, 10, 11, 18])
def test_spi_pins_are_reserved_when_the_chain_uses_gpio_10(tmp_path, pin) -> None:
    from sr.robot3.raspberry_pi import reserved_pins
    path = tmp_path / "psc-status.toml"
    path.write_text("[led_strip]\npins = [18, 10]\n")
    with pytest.raises(ValueError, match="LED strip"):
        RaspberryPi(gpio_loader=lambda: None, reserved=reserved_pins(path)).dc_motor(pin, 20)


def test_sr_style_user_leds(caplog) -> None:
    from sr.robot3 import LED_A, LED_B
    from sr.robot3.leds import chain_leds
    mqtt = FakeMQTT()
    leds = chain_leds(LEDStrip(mqtt, 1))

    leds[LED_A].colour = Colour.BLUE
    assert leds[LED_A].colour == Colour.BLUE
    leds[LED_A].r = True
    assert leds[LED_A].colour == Colour.MAGENTA and leds[LED_A].r and not leds[LED_A].g
    leds[LED_A].b = False
    assert leds[LED_A].colour == Colour.RED
    leds[LED_A].colour = (255, 80, 0)
    assert leds[LED_A].colour == (255, 80, 0)
    assert [colours for _, colours, _ in mqtt.published] == [
        [[0, 0, 255]], [[255, 0, 255]], [[255, 0, 0]], [[255, 80, 0]]]

    # LED_B isn't in a 2-LED chain: a warning, not a crash.
    leds[LED_B].colour = Colour.RED
    assert leds[LED_B].colour == Colour.OFF
    assert "LED_B isn't connected" in caplog.text
    assert len(mqtt.published) == 4

    with pytest.raises(ValueError):
        leds[LED_A].colour = "blue"


def test_robot_exposes_user_leds_on_kch_and_raspberry_pi() -> None:
    from sr.robot3.kch import KCH
    from sr.robot3.leds import chain_leds
    user_leds = chain_leds(LEDStrip(FakeMQTT(), 1))
    assert KCH(user_leds).leds is user_leds
    assert RaspberryPi(gpio_loader=lambda: None, leds=user_leds).leds is user_leds
