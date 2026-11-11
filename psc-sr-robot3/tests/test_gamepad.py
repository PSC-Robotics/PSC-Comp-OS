import errno
import os
import threading

import pytest

from sr.robot3 import gamepad as gp
from sr.robot3.gamepad import AxisInfo, DeviceInfo, GamepadManager


def axes(**ranges):
    codes = {
        "x": gp.ABS_X, "y": gp.ABS_Y, "z": gp.ABS_Z, "rx": gp.ABS_RX, "ry": gp.ABS_RY,
        "rz": gp.ABS_RZ, "gas": gp.ABS_GAS, "brake": gp.ABS_BRAKE, "hat0x": gp.ABS_HAT0X,
        "hat0y": gp.ABS_HAT0Y, "mt": gp.ABS_MT_POSITION_X,
    }
    return {
        codes[name]: AxisInfo(value, low, high)
        for name, (low, high, value) in ranges.items()
    }


STICK16 = (0, 65535, 32768)
HAT = (-1, 1, 0)

# What the kernel drivers report for each controller (from their drivers' mappings).
XBOX_BLE = DeviceInfo(  # hid-generic / hid-microsoft, Xbox Wireless Controller over Bluetooth
    "Xbox Wireless Controller", 0x045E, 0x0B13, 5, "aa:bb:cc:dd:ee:01",
    {304, 305, 307, 308, 310, 311, 314, 315, 316, 317, 318, 167},
    axes(x=STICK16, y=STICK16, z=STICK16, rz=STICK16, brake=(0, 1023, 0), gas=(0, 1023, 0),
         hat0x=HAT, hat0y=HAT),
)
DS4 = DeviceInfo(  # hid-sony / hid-playstation
    "Wireless Controller", 0x054C, 0x09CC, 5, "aa:bb:cc:dd:ee:02",
    {304, 305, 307, 308, 310, 311, 312, 313, 314, 315, 316, 317, 318},
    axes(x=(0, 255, 128), y=(0, 255, 128), rx=(0, 255, 128), ry=(0, 255, 128),
         z=(0, 255, 0), rz=(0, 255, 0), hat0x=HAT, hat0y=HAT),
)
DS4_MOTION = DeviceInfo(
    "Wireless Controller Motion Sensors", 0x054C, 0x09CC, 5, "aa:bb:cc:dd:ee:02",
    set(), axes(x=(-32768, 32767, 0), y=(-32768, 32767, 0), z=(-32768, 32767, 0)),
    properties={gp.INPUT_PROP_ACCELEROMETER},
)
DS4_TOUCHPAD = DeviceInfo(
    "Wireless Controller Touchpad", 0x054C, 0x09CC, 5, "aa:bb:cc:dd:ee:02",
    {272, 325, 330}, axes(x=(0, 1919, 0), y=(0, 941, 0), mt=(0, 1919, 0)),
)
STADIA = DeviceInfo(
    "Google Stadia Controller", 0x18D1, 0x9400, 5, "aa:bb:cc:dd:ee:03",
    {304, 305, 307, 308, 310, 311, 314, 315, 316, 317, 318},
    axes(x=(1, 255, 128), y=(1, 255, 128), z=(1, 255, 128), rz=(1, 255, 128),
         brake=(0, 255, 0), gas=(0, 255, 0), hat0x=HAT, hat0y=HAT),
)
SNES = DeviceInfo(  # DragonRise USB SNES-style pad
    "USB Gamepad ", 0x0079, 0x0011, 3, "",
    set(range(288, 298)), axes(x=(0, 255, 127), y=(0, 255, 127)),
)
KEYBOARD = DeviceInfo("Keyboard", 0x046D, 0xC31C, 3, "", set(range(1, 120)), {})


class FakeIO(gp.DeviceIO):
    """Pretends to be /dev/input."""

    def __init__(self, devices=None):
        self.devices = dict(devices or {})  # path -> DeviceInfo
        self.fds = {}  # fd -> path
        self.queues = {}  # fd -> list of events, or an OSError to raise
        self.next_fd = 10
        self.denied = set()

    def paths(self):
        return sorted(self.devices)

    def open(self, path):
        if path in self.denied:
            raise OSError(errno.EACCES, "Permission denied")
        if path not in self.devices:
            raise OSError(errno.ENOENT, "No such device")
        fd, self.next_fd = self.next_fd, self.next_fd + 1
        self.fds[fd] = path
        self.queues[fd] = []
        return fd

    def close(self, fd):
        self.fds.pop(fd, None)

    def info(self, fd):
        if self.fds[fd] not in self.devices:
            raise OSError(errno.ENODEV, "gone")
        return self.devices[self.fds[fd]]

    def read(self, fd):
        queue = self.queues[fd]
        if isinstance(queue, OSError):
            raise queue
        events, self.queues[fd] = queue, []
        return events

    def wait(self, fds, timeout):
        return [fd for fd in fds if self.queues.get(fd)]

    # test helpers
    def send(self, path, *events):
        fd = next(fd for fd, p in self.fds.items() if p == path)
        self.queues[fd].extend([*events, (gp.EV_SYN, gp.SYN_REPORT, 0)])

    def unplug(self, path):
        del self.devices[path]
        for fd, p in self.fds.items():
            if p == path:
                self.queues[fd] = OSError(errno.ENODEV, "No such device")


@pytest.fixture(autouse=True)
def no_stat(monkeypatch):
    stat = type("Stat", (), {"st_rdev": 1, "st_ctime": 0, "st_ctime_ns": 0})
    real_stat = os.stat
    # Only the gamepad scan's stat of a device path is faked; pytest needs the real one.

    def fake_stat(path, **kwargs):
        return stat() if str(path).startswith("/dev/input") else real_stat(path, **kwargs)

    monkeypatch.setattr(gp.os, "stat", fake_stat)


def manager_with(devices):
    io = FakeIO(devices)
    manager = GamepadManager(io)
    manager._thread = threading.current_thread()  # don't start the background thread
    manager.poll(0)
    return manager, io


def poll(manager):
    manager._last_scan = 0
    manager.poll(0)


@pytest.mark.parametrize("info", [XBOX_BLE, DS4, STADIA, SNES])
def test_gamepads_are_recognised(info) -> None:
    assert gp.is_gamepad(info)


@pytest.mark.parametrize("info", [DS4_MOTION, DS4_TOUCHPAD, KEYBOARD])
def test_other_input_devices_are_ignored(info) -> None:
    assert not gp.is_gamepad(info)


def test_playstation_controller_uses_one_slot_for_its_three_devices() -> None:
    manager, _ = manager_with({
        "/dev/input/event3": DS4, "/dev/input/event4": DS4_MOTION,
        "/dev/input/event5": DS4_TOUCHPAD,
    })
    pad = manager.gamepads[0]
    assert pad.connected and pad.name == "Wireless Controller"
    assert not manager.gamepads[1].connected


@pytest.mark.parametrize(
    ("info", "codes"),
    [
        # Xbox and Stadia: BTN_X (307) is the left button and BTN_Y (308) the top.
        (XBOX_BLE, {"a": 304, "b": 305, "x": 307, "y": 308}),
        (STADIA, {"a": 304, "b": 305, "x": 307, "y": 308}),
        # PlayStation: Cross, Circle, Square (BTN_WEST, 308), Triangle (BTN_NORTH, 307).
        (DS4, {"a": 304, "b": 305, "x": 308, "y": 307}),
        # SNES: B bottom (290), A right (289), Y left (291), X top (288).
        (SNES, {"a": 290, "b": 289, "x": 291, "y": 288}),
    ],
)
def test_face_buttons_are_named_by_position(info, codes) -> None:
    manager, io = manager_with({"/dev/input/event0": info})
    pad = manager.gamepads[0]
    for name, code in codes.items():
        io.send("/dev/input/event0", (gp.EV_KEY, code, 1))
        poll(manager)
        assert pad.is_pressed(name), name
        assert getattr(pad, name)
        assert [b for b, down in pad.buttons.items() if down] == [name]
        io.send("/dev/input/event0", (gp.EV_KEY, code, 0))
        poll(manager)
        assert not pad.is_pressed(name)


def test_xbox_sticks_and_triggers() -> None:
    manager, io = manager_with({"/dev/input/event0": XBOX_BLE})
    pad = manager.gamepads[0]
    pad.deadzone = 0
    sticks = (pad.left_x, pad.left_y, pad.right_x, pad.right_y)
    assert sticks == pytest.approx((0, 0, 0, 0), abs=1e-4)
    io.send("/dev/input/event0", (gp.EV_ABS, gp.ABS_X, 65535), (gp.EV_ABS, gp.ABS_Y, 0),
            (gp.EV_ABS, gp.ABS_Z, 0), (gp.EV_ABS, gp.ABS_RZ, 65535),
            (gp.EV_ABS, gp.ABS_BRAKE, 1023), (gp.EV_ABS, gp.ABS_GAS, 512))
    poll(manager)
    assert pad.left_x == pytest.approx(1)
    assert pad.left_y == pytest.approx(1)  # up is positive
    assert pad.right_x == pytest.approx(-1)
    assert pad.right_y == pytest.approx(-1)
    assert pad.left_trigger == pytest.approx(1)
    assert pad.right_trigger == pytest.approx(0.5, abs=0.01)


def test_playstation_sticks_triggers_and_dpad() -> None:
    manager, io = manager_with({"/dev/input/event0": DS4})
    pad = manager.gamepads[0]
    io.send("/dev/input/event0", (gp.EV_ABS, gp.ABS_RX, 255), (gp.EV_ABS, gp.ABS_Z, 255),
            (gp.EV_ABS, gp.ABS_HAT0X, -1), (gp.EV_ABS, gp.ABS_HAT0Y, -1))
    poll(manager)
    assert pad.right_x == pytest.approx(1)
    assert pad.left_trigger == pytest.approx(1)
    assert pad.right_trigger == 0
    assert pad.dpad_left and pad.dpad_up and not pad.dpad_right and not pad.dpad_down


def test_deadzone() -> None:
    manager, io = manager_with({"/dev/input/event0": DS4})
    pad = manager.gamepads[0]
    io.send("/dev/input/event0", (gp.EV_ABS, gp.ABS_X, 138))  # about 0.08 right
    poll(manager)
    assert pad.left_x == 0
    pad.deadzone = 0
    assert pad.left_x == pytest.approx(0.08, abs=0.01)
    with pytest.raises(ValueError):
        pad.deadzone = 1


def test_snes_pad_dpad_comes_from_its_axes() -> None:
    manager, io = manager_with({"/dev/input/event0": SNES})
    pad = manager.gamepads[0]
    io.send("/dev/input/event0", (gp.EV_ABS, gp.ABS_X, 0), (gp.EV_ABS, gp.ABS_Y, 255))
    poll(manager)
    assert pad.dpad_left and pad.dpad_down and not pad.dpad_up


def test_disconnect_reads_neutral_and_reconnect_fills_the_slot() -> None:
    manager, io = manager_with({"/dev/input/event0": XBOX_BLE, "/dev/input/event1": DS4})
    first, second = manager.gamepads[0], manager.gamepads[1]
    io.send("/dev/input/event0", (gp.EV_ABS, gp.ABS_X, 65535), (gp.EV_KEY, 304, 1))
    poll(manager)
    assert first.left_x == pytest.approx(1) and first.a

    io.unplug("/dev/input/event0")
    poll(manager)
    assert not first.connected
    assert first.left_x == 0 and not first.a and first.name == ""
    assert second.connected  # the other player keeps their controller

    io.devices["/dev/input/event2"] = STADIA
    poll(manager)
    assert first.connected and first.name == "Google Stadia Controller"


def test_was_pressed_counts_each_press_once() -> None:
    manager, io = manager_with({"/dev/input/event0": XBOX_BLE})
    pad = manager.gamepads[0]
    assert not pad.was_pressed("a")
    io.send("/dev/input/event0", (gp.EV_KEY, 304, 1), (gp.EV_KEY, 304, 0))
    poll(manager)
    io.send("/dev/input/event0", (gp.EV_KEY, 304, 1))
    poll(manager)
    assert pad.was_pressed("a")
    assert not pad.was_pressed("a")
    with pytest.raises(ValueError, match="Unknown button"):
        pad.was_pressed("square")


def test_lost_events_resync_from_the_device() -> None:
    held = DeviceInfo(**{**DS4.__dict__, "pressed": {305}})
    manager, io = manager_with({"/dev/input/event0": DS4})
    pad = manager.gamepads[0]
    io.devices["/dev/input/event0"] = held
    io.send("/dev/input/event0", (gp.EV_SYN, gp.SYN_DROPPED, 0))
    poll(manager)
    assert pad.b


def test_buttons_held_when_connecting_are_read() -> None:
    held = DeviceInfo(**{**XBOX_BLE.__dict__, "pressed": {311}})
    manager, _ = manager_with({"/dev/input/event0": held})
    assert manager.gamepads[0].right_bumper


def test_unreadable_devices_are_skipped() -> None:
    io = FakeIO({"/dev/input/event0": XBOX_BLE, "/dev/input/event1": DS4})
    io.denied.add("/dev/input/event0")
    manager = GamepadManager(io)
    manager._thread = threading.current_thread()
    manager.poll(0)
    assert manager.gamepads[0].name == "Wireless Controller"


def test_wait_until_connected_times_out() -> None:
    manager, _ = manager_with({})
    assert manager.gamepads[0].wait_until_connected(timeout=0.05) is False


def test_a_quick_tap_read_in_one_go_counts() -> None:
    manager, io = manager_with({"/dev/input/event0": XBOX_BLE})
    pad = manager.gamepads[0]
    io.send("/dev/input/event0", (gp.EV_KEY, 304, 1), (gp.EV_KEY, 304, 0))
    poll(manager)
    assert not pad.a
    assert pad.was_pressed("a")


def test_axes_at_their_minimum_on_connect_read_centred_until_they_move() -> None:
    # Cheap USB pads report 0 on every axis until their first report: full left and up.
    unreported = DeviceInfo(**{**SNES.__dict__, "axes": axes(x=(0, 255, 0), y=(0, 255, 0))})
    manager, io = manager_with({"/dev/input/event0": unreported})
    pad = manager.gamepads[0]
    pad.deadzone = 0
    assert (pad.left_x, pad.left_y) == (0, 0)
    assert not pad.is_pressed("dpad_left") and not pad.is_pressed("dpad_up")
    io.send("/dev/input/event0", (gp.EV_ABS, gp.ABS_X, 0))
    poll(manager)
    assert pad.left_x == pytest.approx(-1)
    assert pad.left_y == 0


def test_a_device_is_retried_once_udev_fixes_its_permissions(monkeypatch) -> None:
    ctime = {"ns": 1_000_000_000}

    def fake_stat(path, **kwargs):
        return type("Stat", (), {"st_rdev": 1, "st_ctime": 1, "st_ctime_ns": ctime["ns"]})()

    monkeypatch.setattr(gp.os, "stat", fake_stat)
    io = FakeIO({"/dev/input/event0": XBOX_BLE})
    io.denied.add("/dev/input/event0")
    manager = GamepadManager(io)
    manager._thread = threading.current_thread()
    manager.poll(0)
    assert not manager.gamepads[0].connected
    io.denied.clear()
    ctime["ns"] += 200_000_000  # chmod in the same second
    poll(manager)
    assert manager.gamepads[0].connected
