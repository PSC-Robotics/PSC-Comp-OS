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
