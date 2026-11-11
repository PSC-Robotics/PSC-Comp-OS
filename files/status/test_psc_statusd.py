"""Tests of the status LED's patterns. Run with pytest; this file isn't installed."""
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location(
    "psc_statusd", Path(__file__).with_name("psc-statusd.py"))
statusd = importlib.util.module_from_spec(SPEC)
import sys  # noqa: E402
sys.modules["psc_statusd"] = statusd
SPEC.loader.exec_module(statusd)


def message(**fields):
    return json.dumps(fields).encode()


@pytest.fixture
def status():
    robot = statusd.RobotStatus(is_comp_usb=lambda path: "comp" in path)
    robot.set_connected(True)
    robot.handle(statusd.ASTPROCD_TOPIC, message(status="RUNNING"))
    return robot


def plug_in_competition_usb(status):
    status.handle(statusd.ASTDISKD_TOPIC, message(disks={"a": "/media/comp"}))
    status.handle(statusd.ASTMETAD_TOPIC, message(metadata={"mode": "COMP"}))


def code_waiting(status):
    status.handle(statusd.ASTPROCD_TOPIC, message(
        status="RUNNING", code_status="code_running", disk_info={"uuid": "b"}))
    status.handle(statusd.ROBOT_STATE_TOPIC, message(data="waiting_start"))


def after_flash():
    return float("inf")


def test_no_usb_at_all_is_pink(status):
    assert status.pattern() == statusd.NO_USB


def test_competition_usb_flashes_yellow_until_code_arrives(status):
    plug_in_competition_usb(status)
    assert status.pattern() == statusd.COMP_USB
    assert status.pattern(after_flash()) == statusd.COMP_USB


def test_waiting_for_start_is_green_in_development(status):
    code_waiting(status)
    assert status.pattern() == statusd.WAITING_FOR_START


def test_waiting_for_start_is_yellow_in_competition_mode(status):
    plug_in_competition_usb(status)
    code_waiting(status)
    assert status.pattern(after_flash()) == statusd.COMP_WAITING_FOR_START


def test_web_interface_competition_mode_is_yellow_without_a_flash(status):
    status.handle(statusd.ASTMETAD_TOPIC, message(metadata={"mode": "COMP"}))
    code_waiting(status)
    assert status.pattern() == statusd.COMP_WAITING_FOR_START


def test_plugging_in_while_waiting_flashes_first(status):
    code_waiting(status)
    plug_in_competition_usb(status)
    assert status.pattern() == statusd.COMP_USB
    assert status.pattern(after_flash()) == statusd.COMP_WAITING_FOR_START


def test_flash_only_happens_on_plugging_in(status):
    plug_in_competition_usb(status)
    until = status.comp_usb_flash_until
    status.handle(statusd.ASTDISKD_TOPIC, message(disks={"a": "/media/comp"}))
    assert status.comp_usb_flash_until == until


def test_removing_the_stick_returns_to_normal(status):
    plug_in_competition_usb(status)
    status.handle(statusd.ASTDISKD_TOPIC, message(disks={}))
    status.handle(statusd.ASTMETAD_TOPIC, message(metadata={"mode": "DEV"}))
    assert status.pattern(after_flash()) == statusd.NO_USB


def test_code_usb_isnt_a_competition_usb(status):
    status.handle(statusd.ASTDISKD_TOPIC, message(disks={"b": "/media/code"}))
    assert status.pattern() == statusd.NO_USB


def test_running_is_green_after_the_flash(status):
    plug_in_competition_usb(status)
    code_waiting(status)
    status.handle(statusd.ROBOT_STATE_TOPIC, message(data="started"))
    assert status.pattern(after_flash()) == statusd.RUNNING


def test_is_competition_usb_uses_astorias_rule(tmp_path):
    assert not statusd.is_competition_usb(str(tmp_path))
    (tmp_path / "astoria.json").write_text("{}")
    assert statusd.is_competition_usb(str(tmp_path))
    (tmp_path / "robot.py").write_text("")
    assert not statusd.is_competition_usb(str(tmp_path))


def test_rapid_flash_turns_fully_on_and_off():
    assert statusd.COMP_USB.chain(0.05)[0] > 0
    assert statusd.COMP_USB.chain(0.15) == (0, 0, 0)


# ---- web UI settings, battery alarm

def test_scaled_duty_keeps_full_volume_and_silences_zero():
    assert statusd.scaled_duty(50, 1.0) == 50
    assert statusd.scaled_duty(15, 1.0) == 15
    assert statusd.scaled_duty(50, 0) == 0
    # Half volume halves the fundamental's amplitude: sin(pi * d) = 0.5 at 50%
    assert statusd.scaled_duty(50, 0.5) == pytest.approx(100 / 6)


def test_settings_save_and_reload(tmp_path):
    path = tmp_path / "psc" / "settings.json"
    settings = statusd.Settings(path)
    assert settings.saved == statusd.DEFAULT_SETTINGS
    assert settings.update(message(buzzer_volume=30)) == {"buzzer_volume": 30}
    assert statusd.Settings(path).saved == {"buzzer_volume": 30, "led_brightness": 100}


def test_settings_reject_invalid_values(tmp_path):
    settings = statusd.Settings(tmp_path / "settings.json")
    assert settings.update(message(buzzer_volume=101)) is None
    assert settings.update(message(led_brightness=True)) is None
    assert settings.update(b"not json") is None
    assert settings.saved == statusd.DEFAULT_SETTINGS


def test_settings_ignore_a_corrupt_file(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text('{"buzzer_volume": "loud", "led_brightness": 40}')
    assert statusd.Settings(path).saved == {"buzzer_volume": 100, "led_brightness": 40}


def test_competition_mode_uses_the_defaults_and_keeps_the_saved_values(tmp_path):
    settings = statusd.Settings(tmp_path / "settings.json")
    settings.update(message(buzzer_volume=10, led_brightness=20))
    assert settings.effective(locked=True) == statusd.DEFAULT_SETTINGS
    assert settings.effective(locked=False) == {"buzzer_volume": 10, "led_brightness": 20}


def test_battery_alarm_needs_a_sustained_low_reading():
    alarm = statusd.BatteryAlarm(10.5, 11.0, 5)
    assert not alarm.update(10.2, 0)
    assert not alarm.update(10.8, 3)       # back up: the timer restarts
    assert not alarm.update(10.2, 4)
    assert not alarm.update(10.2, 8)
    assert alarm.update(10.2, 9)
    assert alarm.update(10.8, 20)          # above low but below recover: still low
    assert alarm.update(11.2, 21)
    assert not alarm.update(11.2, 26)


def test_battery_alarm_ignores_no_battery():
    alarm = statusd.BatteryAlarm(10.5, 11.0, 0)
    assert not alarm.update(0.02, 0)
    assert not alarm.update(None, 1)
    assert not alarm.update(5.9, 2)       # below 6V: no battery, so no alarm
    alarm.update(6.1, 3)
    assert alarm.update(6.1, 4)           # a flat battery above 6V still alarms


def test_competition_usb_can_turn_the_battery_alarm_off(tmp_path):
    comp = tmp_path / "comp"
    comp.mkdir()
    (comp / "astoria.json").write_text('{"mode": "COMP", "battery_alarm_enabled": false}')
    robot = statusd.RobotStatus()
    robot.handle(statusd.ASTDISKD_TOPIC, json.dumps({"disks": {"a": str(comp)}}).encode())
    assert robot.battery_alarm_bypassed
    (comp / "astoria.json").write_text('{"mode": "COMP"}')
    robot.handle(statusd.ASTDISKD_TOPIC, json.dumps({"disks": {"a": str(comp)}}).encode())
    assert not robot.battery_alarm_bypassed
    (comp / "astoria.json").write_text('{"battery_alarm_enabled": "off"}')
    robot.handle(statusd.ASTDISKD_TOPIC, json.dumps({"disks": {"a": str(comp)}}).encode())
    assert robot.battery_alarm_bypassed
    robot.handle(statusd.ASTDISKD_TOPIC, json.dumps({"disks": {}}).encode())
    assert not robot.battery_alarm_bypassed


class FakeStrip:
    pin = 18
    count = 2

    def __init__(self):
        self.shown = None

    def show(self, colours):
        self.shown = colours


def test_led_brightness_scales_status_and_user_leds():
    strip = FakeStrip()
    chain = statusd.LedChain([strip], True, 0.2, 1.0)
    chain.set_user(json.dumps({"colours": [[200, 100, 0]]}).encode())
    chain.set_status((255, 0, 0))
    assert strip.shown == [(51, 0, 0), (200, 100, 0)]
    chain.set_scale(0.25)
    assert strip.shown == [(13, 0, 0), (50, 25, 0)]


def test_low_battery_alarm_shows_on_every_led_whatever_the_brightness():
    strip = FakeStrip()
    chain = statusd.LedChain([strip], True, 0.2, 1.0)
    chain.set_scale(0)
    chain.set_alarm((255, 0, 0))
    assert strip.shown == [(51, 0, 0), (51, 0, 0)]
    chain.set_alarm(None)
    assert strip.shown == [(0, 0, 0), (0, 0, 0)]


def test_kill_request_matches_astoria():
    request = json.loads(statusd.kill_request())
    assert request["sender_name"] == "psc-statusd"
    assert len(request["uuid"]) == 36


def test_config_battery_defaults(tmp_path):
    path = tmp_path / "psc-status.toml"
    path.write_text("[start_button]\npin = 5\n")
    battery = statusd.load_config(path)["battery"]
    assert battery["low_voltage"] == 10.5 and battery["address"] == 0x48


def test_boot_progress_follows_systemds_job_queue():
    jobs = iter([None, 40, 120, 90, 100, 30, 0])
    progress = statusd.BootProgress(njobs=lambda: next(jobs))
    assert progress.update() == 0              # systemd not answering yet
    assert progress.update() == 0              # 40 queued: the most so far
    assert progress.update() == 0              # 120 queued
    assert progress.update() == pytest.approx(0.25)
    assert progress.update() == pytest.approx(0.25)  # more queued: never goes back
    assert progress.update() == pytest.approx(0.75)
    assert progress.update() == 1
    assert statusd.boot_level(0) == statusd.BOOT_FLOOR
    assert statusd.boot_level(1) == 1
