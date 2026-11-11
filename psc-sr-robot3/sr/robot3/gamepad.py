"""
Gamepads connected to the robot over Bluetooth or USB.

Gamepads are read directly from the Linux input devices (``/dev/input/event*``), so
any controller that Linux supports works, however it is connected. Each controller's
buttons and sticks are mapped to one standard layout, named by position like an Xbox
controller: ``a`` is the bottom face button (Cross on PlayStation controllers), ``b``
the right, ``x`` the left and ``y`` the top.

This module only uses the standard library, so that the robot's Bluetooth service can
load it on its own to show gamepad input in the web interface.
"""
from __future__ import annotations

import errno
import logging
import os
import select
import struct
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Optional

try:
    import fcntl
except ImportError:  # Windows, where there are no Linux input devices to read
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

INPUT_DIR = Path("/dev/input")
RESCAN_INTERVAL_S = 0.5
MAX_GAMEPADS = 4
DEFAULT_DEADZONE = 0.1

# linux/input-event-codes.h
EV_SYN, EV_KEY, EV_ABS = 0x00, 0x01, 0x03
SYN_REPORT, SYN_DROPPED = 0, 3
ABS_X, ABS_Y, ABS_Z, ABS_RX, ABS_RY, ABS_RZ = 0x00, 0x01, 0x02, 0x03, 0x04, 0x05
ABS_GAS, ABS_BRAKE = 0x09, 0x0A
ABS_HAT0X, ABS_HAT0Y = 0x10, 0x11
ABS_MT_POSITION_X = 0x35
ABS_CNT = 0x40
KEY_CNT = 0x300
INPUT_PROP_ACCELEROMETER = 0x06
BTN_JOYSTICK, BTN_GAMEPAD, BTN_THUMBR = 0x120, 0x130, 0x13E
BTN_SOUTH, BTN_EAST, BTN_NORTH, BTN_WEST = 0x130, 0x131, 0x133, 0x134
BTN_TL, BTN_TR, BTN_TL2, BTN_TR2 = 0x136, 0x137, 0x138, 0x139
BTN_SELECT, BTN_START, BTN_MODE, BTN_THUMBL = 0x13A, 0x13B, 0x13C, 0x13D
BTN_DPAD_UP, BTN_DPAD_DOWN, BTN_DPAD_LEFT, BTN_DPAD_RIGHT = 0x220, 0x221, 0x222, 0x223
KEY_BACK, KEY_HOMEPAGE, KEY_MENU = 158, 172, 139
(BTN_TRIGGER, BTN_THUMB, BTN_THUMB2, BTN_TOP, BTN_TOP2, BTN_PINKIE,
 BTN_BASE, BTN_BASE2, BTN_BASE3, BTN_BASE4, BTN_BASE5, BTN_BASE6) = range(0x120, 0x12C)

VENDOR_SONY = 0x054C

BUTTONS = (
    "a", "b", "x", "y", "left_bumper", "right_bumper", "back", "start", "guide",
    "left_stick_button", "right_stick_button", "dpad_up", "dpad_down", "dpad_left",
    "dpad_right",
)

# Kernel drivers for Xbox controllers (xpad, and hid-generic over Bluetooth), Stadia and
# most generic gamepads report the left face button as BTN_X (BTN_NORTH) and the top as
# BTN_Y (BTN_WEST). Sony's drivers report them by position, so swap them back.
STANDARD_BUTTONS: dict[str, tuple[int, ...]] = {
    "a": (BTN_SOUTH,),
    "b": (BTN_EAST,),
    "x": (BTN_NORTH,),
    "y": (BTN_WEST,),
    "left_bumper": (BTN_TL,),
    "right_bumper": (BTN_TR,),
    "back": (BTN_SELECT, KEY_BACK),
    "start": (BTN_START, KEY_MENU),
    "guide": (BTN_MODE, KEY_HOMEPAGE),
    "left_stick_button": (BTN_THUMBL,),
    "right_stick_button": (BTN_THUMBR,),
    "dpad_up": (BTN_DPAD_UP,),
    "dpad_down": (BTN_DPAD_DOWN,),
    "dpad_left": (BTN_DPAD_LEFT,),
    "dpad_right": (BTN_DPAD_RIGHT,),
}
SONY_BUTTONS = {**STANDARD_BUTTONS, "x": (BTN_WEST,), "y": (BTN_NORTH,)}

# Gamepads that report joystick buttons (BTN_TRIGGER...) rather than gamepad buttons,
# such as SNES-style USB pads. The usual order, from the HID button numbers.
JOYSTICK_BUTTONS: dict[str, tuple[int, ...]] = {
    "a": (BTN_TRIGGER,), "b": (BTN_THUMB,), "x": (BTN_THUMB2,), "y": (BTN_TOP,),
    "left_bumper": (BTN_TOP2,), "right_bumper": (BTN_PINKIE,),
    "back": (BTN_BASE3,), "start": (BTN_BASE4,),
    "left_stick_button": (BTN_BASE5,), "right_stick_button": (BTN_BASE6,),
}
JOYSTICK_TRIGGERS = (BTN_BASE, BTN_BASE2)

# Controllers whose layout needs a fix, by (vendor, product).
QUIRK_BUTTONS: dict[tuple[int, int], dict[str, tuple[int, ...]]] = {
    # DragonRise "USB Gamepad" SNES-style pads: X top, A right, B bottom, Y left.
    (0x0079, 0x0011): {
        "a": (BTN_THUMB2,), "b": (BTN_THUMB,), "x": (BTN_TOP,), "y": (BTN_TRIGGER,),
        "left_bumper": (BTN_TOP2,), "right_bumper": (BTN_PINKIE,),
        "back": (BTN_BASE3,), "start": (BTN_BASE4,),
    },
}


# ------------------------------------------------------------------ evdev ioctls
def _ioc(direction: int, number: int, size: int) -> int:
    return (direction << 30) | (size << 16) | (ord('E') << 8) | number


_READ = 2
EVIOCGID = _ioc(_READ, 0x02, 8)


def EVIOCGNAME(length: int) -> int:  # noqa: N802
    return _ioc(_READ, 0x06, length)


def EVIOCGUNIQ(length: int) -> int:  # noqa: N802
    return _ioc(_READ, 0x08, length)


def EVIOCGPROP(length: int) -> int:  # noqa: N802
    return _ioc(_READ, 0x09, length)


def EVIOCGKEY(length: int) -> int:  # noqa: N802
    return _ioc(_READ, 0x18, length)


def EVIOCGBIT(event_type: int, length: int) -> int:  # noqa: N802
    return _ioc(_READ, 0x20 + event_type, length)


def EVIOCGABS(axis: int) -> int:  # noqa: N802
    return _ioc(_READ, 0x40 + axis, 24)


# struct input_event: struct timeval (two longs), __u16 type, __u16 code, __s32 value
EVENT_FORMAT = "llHHi"
EVENT_SIZE = struct.calcsize(EVENT_FORMAT)


def _bits(data: bytes) -> set[int]:
    return {
        index * 8 + bit
        for index, byte in enumerate(data) for bit in range(8) if byte >> bit & 1
    }


@dataclass
class AxisInfo:
    """The range of an absolute axis."""
    value: int
    minimum: int
    maximum: int
    flat: int = 0


@dataclass
class DeviceInfo:
    """What an input device reports about itself."""
    name: str
    vendor: int
    product: int
    bustype: int
    uniq: str
    keys: set[int]
    axes: dict[int, AxisInfo]
    properties: set[int] = field(default_factory=set)
    pressed: set[int] = field(default_factory=set)


class DeviceIO:
    """Reads input devices. Replaced in tests."""

    def open(self, path: str) -> int:
        return os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)

    def close(self, fd: int) -> None:
        os.close(fd)

    def info(self, fd: int) -> DeviceInfo:
        def ioctl(request: int, length: int) -> bytes:
            return fcntl.ioctl(fd, request, bytes(length))

        def text(request: Callable[[int], int]) -> str:
            try:
                return ioctl(request(256), 256).split(b"\0", 1)[0].decode(errors="replace")
            except OSError:
                return ""

        bustype, vendor, product, _version = struct.unpack("HHHH", ioctl(EVIOCGID, 8))
        events = _bits(ioctl(EVIOCGBIT(0, 4), 4))
        keys = set()
        if EV_KEY in events:
            keys = _bits(ioctl(EVIOCGBIT(EV_KEY, KEY_CNT // 8), KEY_CNT // 8))
        axes = {}
        if EV_ABS in events:
            for axis in _bits(ioctl(EVIOCGBIT(EV_ABS, ABS_CNT // 8), ABS_CNT // 8)):
                value, minimum, maximum, _fuzz, flat, _res = struct.unpack(
                    "iiiiii", ioctl(EVIOCGABS(axis), 24))
                axes[axis] = AxisInfo(value, minimum, maximum, flat)
        try:
            properties = _bits(ioctl(EVIOCGPROP(4), 4))
        except OSError:
            properties = set()
        pressed = _bits(ioctl(EVIOCGKEY(KEY_CNT // 8), KEY_CNT // 8)) if keys else set()
        return DeviceInfo(text(EVIOCGNAME), vendor, product, bustype, text(EVIOCGUNIQ).lower(),
                          keys, axes, properties, pressed)

    def read(self, fd: int) -> list[tuple[int, int, int]]:
        """Read the waiting events as (type, code, value). Raises OSError if unplugged."""
        try:
            data = os.read(fd, EVENT_SIZE * 64)
        except BlockingIOError:
            return []
        if not data:
            raise OSError(errno.ENODEV, "Input device closed")
        events = []
        for offset in range(0, len(data) - EVENT_SIZE + 1, EVENT_SIZE):
            _sec, _usec, event_type, code, value = struct.unpack_from(
                EVENT_FORMAT, data, offset)
            events.append((event_type, code, value))
        return events

    def paths(self) -> list[str]:
        try:
            names = os.listdir(INPUT_DIR)
        except OSError:
            return []
        return sorted(
            (str(INPUT_DIR / name) for name in names if name.startswith("event")),
            key=lambda path: int(path.rsplit("event", 1)[1] or 0),
        )

    def wait(self, fds: list[int], timeout: float) -> list[int]:
        if not fds:
            time.sleep(timeout)
            return []
        ready, _, _ = select.select(fds, [], [], timeout)
        return ready


def is_gamepad(info: DeviceInfo) -> bool:
    """Whether a device is a gamepad, rather than e.g. a keyboard or a motion sensor."""
    if INPUT_PROP_ACCELEROMETER in info.properties or ABS_MT_POSITION_X in info.axes:
        return False  # The motion sensors and touchpads of PlayStation controllers
    has_buttons = any(BTN_JOYSTICK <= key <= BTN_THUMBR for key in info.keys)
    has_controls = (
        ABS_X in info.axes or ABS_HAT0X in info.axes
        or BTN_DPAD_UP in info.keys
    )
    return has_buttons and has_controls


class Layout:
    """How one device's events map to the standard layout."""

    def __init__(self, info: DeviceInfo) -> None:
        quirk = QUIRK_BUTTONS.get((info.vendor, info.product))
        if quirk is not None:
            buttons = quirk
        elif not any(BTN_GAMEPAD <= key <= BTN_THUMBR for key in info.keys):
            buttons = JOYSTICK_BUTTONS
        elif info.vendor == VENDOR_SONY:
            buttons = SONY_BUTTONS
        else:
            buttons = STANDARD_BUTTONS
        self.buttons = {name: tuple(code for code in codes if code in info.keys)
                        for name, codes in buttons.items()}
        self.buttons = {name: codes for name, codes in self.buttons.items() if codes}

        axes = info.axes
        self.left = (ABS_X, ABS_Y) if ABS_X in axes and ABS_Y in axes else None
        self.right: Optional[tuple[int, int]] = None
        self.triggers: tuple[Optional[int], Optional[int]] = (None, None)
        if ABS_RX in axes and ABS_RY in axes:
            self.right = (ABS_RX, ABS_RY)
            self.triggers = (
                ABS_Z if ABS_Z in axes else None, ABS_RZ if ABS_RZ in axes else None)
        elif ABS_Z in axes and ABS_RZ in axes:
            self.right = (ABS_Z, ABS_RZ)
        if self.triggers == (None, None) and (ABS_BRAKE in axes or ABS_GAS in axes):
            self.triggers = (ABS_BRAKE if ABS_BRAKE in axes else None,
                             ABS_GAS if ABS_GAS in axes else None)
        joystick = buttons is JOYSTICK_BUTTONS or quirk is not None
        self.trigger_buttons = tuple(
            code if code in info.keys else None
            for code in (JOYSTICK_TRIGGERS if joystick else (BTN_TL2, BTN_TR2)))
        self.hat = ABS_HAT0X in axes and ABS_HAT0Y in axes
        # Pads with only a D-pad (e.g. SNES-style) report it on ABS_X and ABS_Y.
        self.dpad_from_left = (
            not self.hat and self.right is None and BTN_DPAD_UP not in info.keys
            and self.left is not None
        )


@dataclass
class GamepadSnapshot:
    """The state of a gamepad at one moment."""
    connected: bool = False
    name: str = ""
    address: str = ""
    left_x: float = 0.0
    left_y: float = 0.0
    right_x: float = 0.0
    right_y: float = 0.0
    left_trigger: float = 0.0
    right_trigger: float = 0.0
    buttons: dict[str, bool] = field(default_factory=lambda: dict.fromkeys(BUTTONS, False))


class _Device:
    """An open gamepad device and its current state."""

    def __init__(self, path: str, fd: int, info: DeviceInfo) -> None:
        self.path, self.fd, self.info = path, fd, info
        self.layout = Layout(info)
        self.axes = {axis: data.value for axis, data in info.axes.items()}
        self.pressed = set(info.pressed)
        # Some pads (cheap USB ones) report every axis as 0 until their first report,
        # which on a 0-255 stick is full left and up: treat an axis that starts at its
        # minimum as centred until it moves, so the robot doesn't drive on connect.
        self.unmoved = {axis for axis, data in info.axes.items()
                        if data.value == data.minimum and data.maximum > data.minimum}
        # Buttons that went down since the last _count_presses, so a tap whose press
        # and release arrive together still counts.
        self.went_down: set[int] = set()

    def apply(self, events: Iterable[tuple[int, int, int]]) -> None:
        for event_type, code, value in events:
            if event_type == EV_KEY:
                if value:
                    self.pressed.add(code)
                    self.went_down.add(code)
                else:
                    self.pressed.discard(code)
            elif event_type == EV_ABS:
                self.axes[code] = value
                self.unmoved.discard(code)

    def _stick(self, axis: Optional[int], deadzone: float, invert: bool = False) -> float:
        if axis is None or axis not in self.info.axes or axis in self.unmoved:
            return 0.0
        data = self.info.axes[axis]
        span = data.maximum - data.minimum
        if span <= 0:
            return 0.0
        value = (self.axes.get(axis, data.value) - data.minimum) * 2 / span - 1
        value = max(-1.0, min(1.0, -value if invert else value))
        if abs(value) <= deadzone:
            return 0.0
        return (abs(value) - deadzone) / (1 - deadzone) * (1 if value > 0 else -1)

    def _trigger(self, axis: Optional[int], button: Optional[int]) -> float:
        if axis is not None:
            data = self.info.axes[axis]
            span = data.maximum - data.minimum
            if span > 0:
                value = (self.axes.get(axis, data.value) - data.minimum) / span
                return max(0.0, min(1.0, value))
        if button is not None:
            return 1.0 if button in self.pressed else 0.0
        return 0.0

    def snapshot(self, deadzone: float) -> GamepadSnapshot:
        layout = self.layout
        left, right = layout.left or (None, None), layout.right or (None, None)
        state = GamepadSnapshot(
            connected=True, name=self.info.name, address=self.info.uniq,
            left_x=self._stick(left[0], deadzone), left_y=self._stick(left[1], deadzone, True),
            right_x=self._stick(right[0], deadzone),
            right_y=self._stick(right[1], deadzone, True),
            left_trigger=self._trigger(layout.triggers[0], layout.trigger_buttons[0]),
            right_trigger=self._trigger(layout.triggers[1], layout.trigger_buttons[1]),
        )
        for name, codes in layout.buttons.items():
            state.buttons[name] = any(code in self.pressed for code in codes)
        if layout.hat:
            hat_x, hat_y = self.axes.get(ABS_HAT0X, 0), self.axes.get(ABS_HAT0Y, 0)
            state.buttons.update(dpad_left=hat_x < 0, dpad_right=hat_x > 0,
                                 dpad_up=hat_y < 0, dpad_down=hat_y > 0)
        elif layout.dpad_from_left:
            raw_x, raw_y = self._stick(ABS_X, 0.5), self._stick(ABS_Y, 0.5, True)
            state.buttons.update(dpad_left=raw_x < 0, dpad_right=raw_x > 0,
                                 dpad_up=raw_y > 0, dpad_down=raw_y < 0)
        return state


class Gamepad:
    """
    One player's gamepad.

    The object stays the same while controllers connect and disconnect. While no
    controller is connected, ``connected`` is False, every stick and trigger reads 0
    and no button is pressed, so a robot driven from the gamepad stops.

    Sticks read from -1 to 1: ``left_x`` and ``right_x`` are positive to the right,
    and ``left_y`` and ``right_y`` are positive upwards. Triggers read from 0 to 1.
    """

    def __init__(self, manager: GamepadManager, index: int) -> None:
        self._manager = manager
        self._index = index
        self._seen_counts = dict.fromkeys(BUTTONS, 0)

    def _state(self) -> GamepadSnapshot:
        return self._manager._snapshot(self._index)

    @property
    def connected(self) -> bool:
        """Whether a controller is connected for this player."""
        return self._state().connected

    @property
    def name(self) -> str:
        """The connected controller's name, or an empty string."""
        return self._state().name

    @property
    def deadzone(self) -> float:
        """How far a stick must move from the centre before it reads more than 0 (0 to 0.9)."""
        return self._manager.deadzone

    @deadzone.setter
    def deadzone(self, value: float) -> None:
        if (
            isinstance(value, bool) or not isinstance(value, (int, float))
            or not 0 <= value <= 0.9
        ):
            raise ValueError("deadzone must be a number from 0 to 0.9")
        self._manager.deadzone = float(value)

    @property
    def left_x(self) -> float:
        """The left stick, from -1 (left) to 1 (right)."""
        return self._state().left_x

    @property
    def left_y(self) -> float:
        """The left stick, from -1 (down) to 1 (up)."""
        return self._state().left_y

    @property
    def right_x(self) -> float:
        """The right stick, from -1 (left) to 1 (right)."""
        return self._state().right_x

    @property
    def right_y(self) -> float:
        """The right stick, from -1 (down) to 1 (up)."""
        return self._state().right_y

    @property
    def left_trigger(self) -> float:
        """The left trigger, from 0 (released) to 1 (fully pressed)."""
        return self._state().left_trigger

    @property
    def right_trigger(self) -> float:
        """The right trigger, from 0 (released) to 1 (fully pressed)."""
        return self._state().right_trigger

    def is_pressed(self, button: str) -> bool:
        """
        Whether a button is held down.

        :param button: A button name, such as ``"a"`` or ``"dpad_up"``.
        """
        return self._state().buttons[self._check_button(button)]

    def was_pressed(self, button: str) -> bool:
        """
        Whether a button has been pressed since this was last asked for that button.

        Use this to do something once per press, even if your loop is slow.

        :param button: A button name, such as ``"a"`` or ``"dpad_up"``.
        """
        button = self._check_button(button)
        count = self._manager._press_count(self._index, button)
        pressed = count != self._seen_counts[button]
        self._seen_counts[button] = count
        return pressed

    @property
    def buttons(self) -> dict[str, bool]:
        """Every button and whether it is held down."""
        return dict(self._state().buttons)

    def wait_until_connected(self, timeout: Optional[float] = None) -> bool:
        """
        Wait until a controller is connected for this player.

        :param timeout: The longest time to wait, in seconds. Waits forever if None.
        :return: Whether a controller is connected.
        """
        return self._manager._wait_connected(self._index, timeout)

    @staticmethod
    def _check_button(button: str) -> str:
        if button not in BUTTONS:
            raise ValueError(
                f"Unknown button {button!r}; the buttons are: {', '.join(BUTTONS)}")
        return button

    def __getattr__(self, name: str) -> bool:
        if name in BUTTONS:
            return self.is_pressed(name)
        raise AttributeError(name)

    def __repr__(self) -> str:
        state = self._state()
        if not state.connected:
            return f"<Gamepad {self._index + 1}: not connected>"
        return f"<Gamepad {self._index + 1}: {state.name}>"


class GamepadManager:
    """
    Finds gamepads and keeps their state up to date in a background thread.

    Controllers are given to players in the order they connect. A player keeps their
    controller until it disconnects.
    """

    def __init__(
        self, io: Optional[DeviceIO] = None, max_gamepads: int = MAX_GAMEPADS,
    ) -> None:
        self._io = io or DeviceIO()
        self._lock = threading.Condition()
        self._slots: list[Optional[_Device]] = [None] * max_gamepads
        self._press_counts = [dict.fromkeys(BUTTONS, 0) for _ in range(max_gamepads)]
        self._last_buttons = [dict.fromkeys(BUTTONS, False) for _ in range(max_gamepads)]
        self._ignored: dict[str, tuple[int, int]] = {}  # path -> (device number, time)
        self._warned: set[str] = set()
        self.deadzone = DEFAULT_DEADZONE
        self.gamepads = tuple(Gamepad(self, index) for index in range(max_gamepads))
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._last_scan = 0.0

    def start(self) -> None:
        """Start reading gamepads in a background thread, if not already started."""
        with self._lock:
            if self._thread is not None:
                return
            self._thread = threading.Thread(target=self._run, name="gamepads", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)
        with self._lock:
            for index, device in enumerate(self._slots):
                if device is not None:
                    self._remove(index)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.poll(0.1)
            except Exception:  # Keep reading: one bad device must not stop the others.
                logger.exception("Error reading gamepads")
                time.sleep(0.5)

    def poll(self, timeout: float) -> None:
        """Look for new gamepads and read waiting input. Called by the background thread."""
        now = time.monotonic()
        if now - self._last_scan >= RESCAN_INTERVAL_S:
            self._last_scan = now
            self._scan()
        with self._lock:
            fds = [device.fd for device in self._slots if device is not None]
        for fd in self._io.wait(fds, timeout):
            with self._lock:
                index = next(
                    (i for i, d in enumerate(self._slots) if d is not None and d.fd == fd),
                    None)
                if index is None:
                    continue
                device = self._slots[index]
                assert device is not None
                try:
                    events = self._io.read(fd)
                except OSError:
                    logger.info(f"Gamepad {index + 1} disconnected: {device.info.name}")
                    self._remove(index)
                    continue
                if any(t == EV_SYN and c == SYN_DROPPED for t, c, _ in events):
                    try:  # Events were lost: read the whole state again.
                        info = self._io.info(fd)
                        device.axes = {axis: data.value for axis, data in info.axes.items()}
                        device.pressed = set(info.pressed)
                        device.unmoved = {axis for axis in device.unmoved
                                          if device.axes.get(axis) == info.axes[axis].minimum}
                    except OSError:
                        self._remove(index)
                        continue
                else:
                    device.apply(events)
                self._count_presses(index)
                self._lock.notify_all()

    def _scan(self) -> None:
        paths = self._io.paths()
        with self._lock:
            open_paths = {device.path for device in self._slots if device is not None}
        for path in paths:
            if path in open_paths:
                continue
            try:
                stat = os.stat(path)
                # ctime in ns: udev's permission change often lands in the same
                # second the node appears, and must still retry the open
                identity = (stat.st_rdev, stat.st_ctime_ns)
            except OSError:
                identity = (0, 0)
            if self._ignored.get(path) == identity:
                continue
            with self._lock:
                free = next((i for i, d in enumerate(self._slots) if d is None), None)
            if free is None:
                return
            try:
                fd = self._io.open(path)
            except OSError as error:
                if error.errno == errno.EACCES and path not in self._warned:
                    self._warned.add(path)
                    logger.warning(
                        f"No permission to read {path}; gamepads on it can't be used")
                self._ignored[path] = identity
                continue
            try:
                info = self._io.info(fd)
            except OSError:
                self._io.close(fd)
                continue
            if not is_gamepad(info):
                self._io.close(fd)
                self._ignored[path] = identity
                continue
            with self._lock:
                device = _Device(path, fd, info)
                self._slots[free] = device
                self._last_buttons[free] = device.snapshot(0).buttons
                self._lock.notify_all()
            self._ignored.pop(path, None)
            logger.info(f"Gamepad {free + 1} connected: {info.name}")

    def _remove(self, index: int) -> None:
        device = self._slots[index]
        self._slots[index] = None
        self._last_buttons[index] = dict.fromkeys(BUTTONS, False)
        if device is not None:
            try:
                self._io.close(device.fd)
            except OSError:
                pass
        self._lock.notify_all()

    def _count_presses(self, index: int) -> None:
        device = self._slots[index]
        if device is None:
            return
        buttons = device.snapshot(0).buttons
        peak = buttons
        went_down, device.went_down = device.went_down, set()
        if went_down - device.pressed:  # Pressed and released again in this batch
            held = device.pressed
            device.pressed = held | went_down
            peak = device.snapshot(0).buttons
            device.pressed = held
        for name, pressed in peak.items():
            if pressed and not self._last_buttons[index][name]:
                self._press_counts[index][name] += 1
        self._last_buttons[index] = buttons

    def _snapshot(self, index: int) -> GamepadSnapshot:
        self.start()
        with self._lock:
            device = self._slots[index]
            return device.snapshot(self.deadzone) if device is not None else GamepadSnapshot()

    def _press_count(self, index: int, button: str) -> int:
        self.start()
        with self._lock:
            return self._press_counts[index][button]

    def _wait_connected(self, index: int, timeout: Optional[float]) -> bool:
        self.start()
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._lock:
            while self._slots[index] is None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self._lock.wait(timeout=1 if remaining is None else min(1, remaining))
            return True

    def snapshots(self) -> list[GamepadSnapshot]:
        """The state of every player's gamepad."""
        return [self._snapshot(index) for index in range(len(self._slots))]
