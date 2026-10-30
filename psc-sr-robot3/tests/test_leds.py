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


@pytest.mark.parametrize("pin", [7, 8, 9, 10, 11, 18])
def test_spi_pins_are_reserved(pin) -> None:
    with pytest.raises(ValueError, match="LED strip"):
        RaspberryPi(gpio_loader=lambda: None).dc_motor(pin, 20)
