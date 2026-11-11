import json
import threading
from types import SimpleNamespace

import pytest

from sr.robot3 import BatteryMonitorError
from sr.robot3 import battery as battery_module
from sr.robot3.battery import BATTERY_TOPIC, BrainBatterySensor
from sr.robot3.brain import Brain

BOOT = "boot-1"


class FakeMQTT:
    def __init__(self):
        self.callbacks = {}

    def subscribe(self, topic, callback, abs_path=False):
        assert abs_path
        self.callbacks[topic] = callback

    def deliver(self, payload):
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.callbacks[BATTERY_TOPIC](None, None, SimpleNamespace(payload=raw))


class Clock:
    def __init__(self):
        self.now = 100.0

    def __call__(self):
        return self.now


def reading(voltage, at, low=False, boot_id=BOOT):
    return {"voltage": voltage, "low": low, "low_voltage": 10.5, "alarm_bypassed": False,
            "time": at, "boot_id": boot_id}


@pytest.fixture(autouse=True)
def short_wait(monkeypatch):
    monkeypatch.setattr(battery_module, "FIRST_READING_TIMEOUT_S", 0.05)


@pytest.fixture
def sensor():
    mqtt, clock = FakeMQTT(), Clock()
    return BrainBatterySensor(mqtt, clock=clock, boot_id=BOOT), mqtt, clock


def test_voltage_comes_from_the_status_service(sensor):
    battery, mqtt, clock = sensor
    mqtt.deliver(reading(12.345, clock.now))
    assert battery.voltage == pytest.approx(12.345)
    assert not battery.low
    clock.now += 1
    mqtt.deliver(reading(10.2, clock.now, low=True))
    assert battery.voltage == pytest.approx(10.2)
    assert battery.low


def test_a_reading_left_by_a_stopped_status_service_is_an_error(sensor):
    battery, mqtt, clock = sensor
    mqtt.deliver(reading(12.0, clock.now))
    clock.now += battery_module.STALE_AFTER_S + 1
    with pytest.raises(BatteryMonitorError, match="psc-statusd"):
        battery.voltage


def test_a_reading_from_an_earlier_boot_is_an_error(sensor):
    battery, mqtt, clock = sensor
    mqtt.deliver(reading(12.0, clock.now, boot_id="boot-0"))
    with pytest.raises(BatteryMonitorError, match="up-to-date"):
        battery.voltage


def test_waits_for_a_current_reading_after_an_old_retained_one(sensor, monkeypatch):
    battery, mqtt, clock = sensor
    monkeypatch.setattr(battery_module, "FIRST_READING_TIMEOUT_S", 2)
    mqtt.deliver(reading(11.0, clock.now - 60))  # retained, from before a restart
    threading.Timer(0.05, lambda: mqtt.deliver(reading(12.5, clock.now))).start()
    assert battery.voltage == pytest.approx(12.5)


def test_no_reading_at_all_is_an_error(sensor):
    battery, _, _ = sensor
    with pytest.raises(BatteryMonitorError, match="systemctl status psc-statusd"):
        battery.voltage


def test_sensor_not_responding_is_an_error(sensor):
    battery, mqtt, clock = sensor
    mqtt.deliver(reading(None, clock.now))
    with pytest.raises(BatteryMonitorError, match="ADS1115"):
        battery.voltage


def test_malformed_messages_are_ignored(sensor):
    battery, mqtt, clock = sensor
    mqtt.deliver(reading(11.8, clock.now))
    mqtt.deliver(b"not json")
    mqtt.deliver(b"[1, 2]")
    mqtt.deliver({"voltage": "lots", "low": False, "time": clock.now, "boot_id": BOOT})
    mqtt.deliver({"voltage": 9.0})  # an older format, without a time
    assert battery.voltage == pytest.approx(11.8)


def test_repr(sensor):
    battery, mqtt, clock = sensor
    assert "no reading" in repr(battery)
    mqtt.deliver(reading(12.0, clock.now))
    assert "12.00V" in repr(battery)


def test_off_the_robot():
    brain = Brain(gpio_loader=lambda: None, reserved={})
    with pytest.raises(BatteryMonitorError, match="only available on the robot"):
        brain.battery_sensor.voltage


def test_brain_has_the_battery_sensor(sensor):
    battery, _, _ = sensor
    brain = Brain(gpio_loader=lambda: None, reserved={}, battery_sensor=battery)
    assert brain.battery_sensor is battery
