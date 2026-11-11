"""Test that the module works."""
import logging
import signal
import socket
from types import MappingProxyType

import pytest

from sr.robot3 import Robot
from sr.robot3.astoria import Metadata
from sr.robot3.exceptions import MetadataNotReadyError
from sr.robot3.utils import BoardIdentity, obtain_lock

from .conftest import MockAtExit, MockSerialWrapper


def test_robot(monkeypatch, caplog) -> None:
    """Test that the Robot object can be created."""
    # monkey patch serial ports so we can test without hardware
    monkeypatch.setattr('sr.robot3.power_board.SerialWrapper', MockSerialWrapper([
        ("*IDN?", "Student Robotics:PBv4B:POW123:4.4.1"),
        ("OUT:0:SET:1", "ACK"),
        ("OUT:1:SET:1", "ACK"),
        ("OUT:2:SET:1", "ACK"),
        ("OUT:3:SET:1", "ACK"),
        ("OUT:5:SET:1", "ACK"),
        ("OUT:6:SET:1", "ACK"),
        ("*IDN?", "Student Robotics:PBv4B:POW123:4.4.1"),
        ("BTN:START:GET?", "0:1"),
        ("NOTE:1760:100", "ACK"),  # Start up sound
        ("LED:RUN:SET:F", "ACK"),
        ("BTN:START:GET?", "0:0"),
        ("BTN:START:GET?", "0:1"),
        ("LED:RUN:SET:1", "ACK"),
    ]))
    monkeypatch.setattr('sr.robot3.motor_board.SerialWrapper', MockSerialWrapper([
        ("*IDN?", "Student Robotics:MCv4B:MOT123:4.4"),
        ("*IDN?", "Student Robotics:MCv4B:MOT123:4.4"),
    ]))
    monkeypatch.setattr('sr.robot3.servo_board.SerialWrapper', MockSerialWrapper([
        ("*IDN?", "Student Robotics:SBv4B:TEST123:4.3"),
        ("*IDN?", "Student Robotics:SBv4B:TEST123:4.3"),
    ]))
    monkeypatch.setattr('sr.robot3.arduino.SerialWrapper', MockSerialWrapper([
        ("v", "SRduino:2.0"),
        ("v", "SRduino:2.0"),
    ]))

    # monkey patch atexit to avoid running cleanup code
    monkeypatch.setattr('sr.robot3.power_board.atexit', MockAtExit())
    monkeypatch.setattr('sr.robot3.motor_board.atexit', MockAtExit())
    monkeypatch.setattr('sr.robot3.servo_board.atexit', MockAtExit())

    # Monkey patch serial comport lookup so only manual boards are found
    monkeypatch.setattr('sr.robot3.power_board.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.motor_board.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.servo_board.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.arduino.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.bus_servo.comports', lambda: [])

    # Forget the camera
    monkeypatch.setattr('sr.robot3.robot._setup_cameras', lambda *_: {})

    manual_boards = {
        'PBv4B': ['test://'],
        'SBv4B': ['test://'],
        'MCv4B': ['test://'],
        'Arduino': ['test://'],
    }
    # check logging
    caplog.clear()
    caplog.set_level(logging.CRITICAL, logger='sr.robot3.mqtt')
    caplog.set_level(logging.CRITICAL, logger='sr.robot3.astoria')
    caplog.set_level(logging.INFO, logger='sr.robot3.robot')

    # Test that we can obtain a lock before creating a robot object
    lock = obtain_lock()
    assert isinstance(lock, socket.socket)
    lock.close()  # release the lock

    # Test that we can create a robot object
    r = Robot(wait_for_start=False, manual_boards=manual_boards, debug=True)
    assert caplog.record_tuples[1:] == [
        # First line contains the version number
        ('sr.robot3.robot', logging.INFO, 'Found PowerBoard, serial: POW123'),
        ('sr.robot3.robot', logging.INFO, 'Found MotorBoard, serial: MOT123'),
        ('sr.robot3.robot', logging.INFO, 'Found ServoBoard, serial: TEST123'),
        ('sr.robot3.robot', logging.INFO, 'Found Arduino, serial: test://'),
    ]

    # Check we found all the boards
    assert r.power_board._identity == BoardIdentity(
        "Student Robotics", "PBv4B", "POW123", "4.4.1")

    assert r.motor_board._identity == BoardIdentity(
        "Student Robotics", "MCv4B", "MOT123", "4.4")

    assert r.servo_board._identity == BoardIdentity(
        "Student Robotics", "SBv4B", "TEST123", "4.3")

    assert r.arduino._identity == BoardIdentity(
        "Student Robotics", "SRduino", "test://", "2.0")

    # Check that a RuntimeError is raised if we have 0 instances of a board
    with pytest.raises(RuntimeError, match="No boards of this type found"):
        r.camera.identify()
    assert dict(r.cameras) == {}

    # Check that we can't obtain a lock when a robot object already exists
    with pytest.raises(
        OSError,
        match="Unable to obtain lock, Is another robot instance already running?"
    ):
        obtain_lock()

    # Check that an exception is raised if we try to access metadata before wait_start
    with pytest.raises(
        MetadataNotReadyError,
        match=".*can only be used after wait_start has been called"
    ):
        r.zone

    r.wait_start()

    assert r.zone == 0

    # Check _something_ is configured to handle SIGTERM
    assert signal.getsignal(signal.SIGTERM) != signal.SIG_DFL


def test_robot_without_sr_boards(monkeypatch) -> None:
    """The robot API can start when no SR hardware is connected."""
    monkeypatch.setattr('sr.robot3.robot.obtain_lock', lambda: socket.socket())
    monkeypatch.setattr(
        'sr.robot3.power_board.PowerBoard._get_supported_boards',
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr('sr.robot3.motor_board.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.servo_board.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.arduino.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.bus_servo.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.robot.init_astoria_mqtt', lambda: (None, None))
    monkeypatch.setattr('sr.robot3.robot.KCH', lambda *args: object())
    monkeypatch.setattr(Robot, '_init_camera', lambda self: setattr(self, '_cameras', {}))

    robot = Robot(wait_for_start=False)
    try:
        assert robot.motor_boards == {}
        assert robot.servo_boards == {}
        assert robot.arduinos == {}
        with pytest.raises(RuntimeError, match="No power board is connected"):
            _ = robot.power_board
    finally:
        robot._lock.close()


def test_robot_with_multiple_cameras(monkeypatch) -> None:
    """Several cameras are reachable through robot.cameras, not robot.camera."""
    monkeypatch.setattr('sr.robot3.robot.obtain_lock', lambda: socket.socket())
    monkeypatch.setattr(
        'sr.robot3.power_board.PowerBoard._get_supported_boards',
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr('sr.robot3.motor_board.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.servo_board.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.arduino.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.bus_servo.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.robot.init_astoria_mqtt', lambda: (None, None))
    monkeypatch.setattr('sr.robot3.robot.KCH', lambda *args: object())
    cameras = {"A1B2C3D4": object(), "usb-1-1.2": object()}
    monkeypatch.setattr(
        Robot, '_init_camera',
        lambda self: setattr(self, '_cameras', MappingProxyType(cameras)),
    )
    monkeypatch.setattr(Robot, '_log_connected_boards', lambda self: None)

    robot = Robot(wait_for_start=False)
    try:
        assert dict(robot.cameras) == cameras
        with pytest.raises(TypeError):
            robot.cameras["extra"] = object()  # type: ignore[index]
        with pytest.raises(RuntimeError, match=r"robot\.cameras.*'A1B2C3D4', 'usb-1-1.2'"):
            _ = robot.camera
    finally:
        robot._lock.close()


def test_wait_start_announces_state(monkeypatch) -> None:
    """A status LED service can tell when code is waiting for the start button."""
    published = []

    class FakeMQTT:
        def wrapped_publish(self, topic, payload, retain=False, **kwargs):
            published.append((topic, payload))

        def subscribe(self, topic, callback, abs_path=False):  # the battery sensor
            pass

    class FakeAstoria:
        presses = iter([False, False, True])

        def get_start_button_pressed(self):
            return next(self.presses)

        def fetch_current_metadata(self):
            return Metadata()

    monkeypatch.setattr('sr.robot3.robot.obtain_lock', lambda: socket.socket())
    monkeypatch.setattr(
        'sr.robot3.power_board.PowerBoard._get_supported_boards',
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr('sr.robot3.motor_board.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.servo_board.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.arduino.comports', lambda: [])
    monkeypatch.setattr('sr.robot3.bus_servo.comports', lambda: [])
    monkeypatch.setattr(
        'sr.robot3.robot.init_astoria_mqtt', lambda: (FakeMQTT(), FakeAstoria()),
    )
    monkeypatch.setattr('sr.robot3.robot.KCH', MockKCH)
    monkeypatch.setattr(Robot, '_init_camera', lambda self: setattr(self, '_cameras', {}))
    monkeypatch.setattr(Robot, 'sleep', lambda self, secs: None)

    robot = Robot(wait_for_start=False)
    try:
        robot.wait_start()
    finally:
        robot._lock.close()

    states = [payload for topic, payload in published if topic == 'psc/robot/state']
    assert states == ['waiting_start', 'started']


class MockKCH:
    def __init__(self, *args) -> None:
        pass

    def _flash_start(self) -> None:
        pass

    _start = False


@pytest.hookimpl(trylast=True)
@pytest.mark.hardware
def test_robot_discovery() -> None:
    """Test that we can discover all the boards when creating a Robot object."""
    robot = Robot(wait_for_start=False)

    # Test that we can access the singular boards
    power_asset_tag = robot.power_board.identify().asset_tag
    servo_asset_tag = robot.servo_board.identify().asset_tag
    motor_asset_tag = robot.motor_board.identify().asset_tag

    # Test that we can access the boards by asset tag
    assert robot.power_board == robot.boards[power_asset_tag]
    assert robot.servo_board == robot.boards[servo_asset_tag]
    assert robot.motor_board == robot.boards[motor_asset_tag]


def test_cannot_subclass() -> None:
    with pytest.raises(TypeError, match="Robot cannot be sub-classed"):
        class MyRobot(Robot):
            pass


@pytest.mark.parametrize(("env", "expected"), [("0", False), ("1", True), (None, True)])
def test_kch_hat_opt_out(env, expected, monkeypatch) -> None:
    """SR_KCH_HAT=0 stops the API driving the KCH hat's GPIO pins."""
    import importlib
    import sys
    import types

    import sr.robot3.kch as kch

    fake_gpio = types.ModuleType('RPi.GPIO')
    fake_rpi = types.ModuleType('RPi')
    fake_rpi.GPIO = fake_gpio  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, 'RPi', fake_rpi)
    monkeypatch.setitem(sys.modules, 'RPi.GPIO', fake_gpio)
    if env is None:
        monkeypatch.delenv('SR_KCH_HAT', raising=False)
    else:
        monkeypatch.setenv('SR_KCH_HAT', env)
    try:
        assert importlib.reload(kch).HAS_HAT is expected
    finally:
        monkeypatch.undo()
        importlib.reload(kch)
