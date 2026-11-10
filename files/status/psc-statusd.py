#!/usr/bin/env python3
"""PSC Robotics status LED, buzzer and start button service.

Shows the robot's state on the first LED of a WS2812 chain, turns presses of a
GPIO start button into the same Astoria start event that the web UI's Start
button sends, and plays sounds on the buzzer: system sounds, and tones requested
by robot code through robot.buzzer.

    Booting                  blue, pulsing
    No code USB              pink, flashing
    Waiting for start        green, pulsing slowly (dim)
    Same, competition mode   yellow, pulsing slowly (dim)
    Competition USB plugged
      in, or no code USB yet yellow, flashing rapidly
    Code running             green
    Code finished            white (dim)
    Code crashed             red, flashing

Sounds (defaults; each can be changed in /etc/psc-status.toml):

    Waiting for start  one beep (A6, as Student Robotics uses)
    Start pressed      three rising notes
    Code crashed       fast short beeps

It also stops DC motors that robot code drove from the Pi's pins when the code
stops, in case the code was killed before it could stop them itself.

A chain of WS2812 ("NeoPixel") LEDs shows the status on its first LED, and
the colour robot code sets (robot.brain.led) on the next. Robot code's LED turns off
when the code stops. The chain's data is sent on GPIO 18 (PWM, as the Brain Board
HAT is wired) and GPIO 10 (SPI MOSI) at once, so it works on either pin.

Pins, LED polarity, the LED chain, buzzer type, volume and sounds are set in
/etc/psc-status.toml.

The web UI's settings scale the buzzer's volume and the LEDs' brightness, from 0 to
100% of those values. They're saved in /var/lib/psc/settings.json and applied at
once. A competition USB (competition mode) puts both back to 100% and locks them,
until it's removed.

The battery is read from an ADS1115 on the HAT (I2C 0x48, AIN0, through a divider)
and shown in the web UI. If it stays below [battery] low_voltage, the buzzer beeps
rapidly, every LED flashes red and robot code is stopped, until the battery is
back above recover_voltage. The alarm ignores the volume and brightness settings.
"""
from __future__ import annotations

import atexit
import ctypes
import fcntl
import json
import math
import os
import queue
import signal
import struct
import threading
import time
import tomllib
import uuid
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("PSC_STATUS_CONFIG", "/etc/psc-status.toml"))
ASTPROCD_TOPIC = "astoria/astprocd"
ASTDISKD_TOPIC = "astoria/astdiskd"
ASTMETAD_TOPIC = "astoria/astmetad"
ROBOT_STATE_TOPIC = "psc/robot/state"
START_BUTTON_TOPIC = "astoria/broadcast/start_button"
BUZZER_TOPIC = "psc/buzzer"
MOTORS_TOPIC = "psc/motors"
LEDS_TOPIC = "psc/leds"
SETTINGS_TOPIC = "psc/settings"          # the settings in use, retained, from this service
SETTINGS_SET_TOPIC = "psc/settings/set"  # changes from the web UI
BATTERY_TOPIC = "psc/battery"            # the battery voltage, retained
KILL_TOPIC = "astoria/astprocd/request/kill"
CODE_ACTIVE = ("code_starting", "code_running")
SETTINGS_FILE = Path(os.environ.get("PSC_SETTINGS_FILE", "/var/lib/psc/settings.json"))
DEFAULT_SETTINGS = {"buzzer_volume": 100, "led_brightness": 100}

# How often the LEDs are updated, and how long a flash or pulse takes.
TICK = 0.05
FLASH_PERIOD = 1.0
RAPID_FLASH_PERIOD = 0.2
# How long the rapid flash shows that a competition USB was plugged in, when the LED
# then has something else to show.
COMP_USB_FLASH_SECONDS = 3.0
PULSE_PERIOD = 2.5
PULSE_FLOOR = 0.08  # a pulse's dimmest point, so the LED never looks off


@dataclass(frozen=True)
class LedPattern:
    """A status on the WS2812 status LED."""
    # (red, green, blue) from 0 to 255, before status_brightness; and its effect.
    colour: tuple[int, int, int]
    effect: str = "solid"  # "solid", "flash", "rapid" or "pulse"

    def chain(self, now: float) -> tuple[float, float, float]:
        """The status LED's colour at a moment, each part from 0 to 255."""
        level = 1.0
        if self.effect == "flash":
            level = 1.0 if now % FLASH_PERIOD < FLASH_PERIOD / 2 else 0.0
        elif self.effect == "rapid":
            level = 1.0 if now % RAPID_FLASH_PERIOD < RAPID_FLASH_PERIOD / 2 else 0.0
        elif self.effect == "pulse":
            # A sine wave, squared so it looks even to the eye (LEDs look much
            # brighter at low levels than their duty cycle).
            wave = (1 - math.cos(2 * math.pi * now / PULSE_PERIOD)) / 2
            level = PULSE_FLOOR + (1 - PULSE_FLOOR) * wave * wave
        return tuple(part * level for part in self.colour)  # type: ignore[return-value]


BOOTING = LedPattern((0, 0, 255), "pulse")
NO_USB = LedPattern((255, 0, 110), "flash")
READY = LedPattern((60, 60, 60))
WAITING_FOR_START = LedPattern((0, 140, 0), "pulse")
COMP_WAITING_FOR_START = LedPattern((140, 95, 0), "pulse")
COMP_USB = LedPattern((255, 170, 0), "rapid")
RUNNING = LedPattern((0, 255, 0))
CRASHED = LedPattern((255, 0, 0), "flash")
LOW_BATTERY = LedPattern((255, 0, 0), "rapid")  # on every LED in the chain

# Sounds are lists of (frequency in Hz, seconds, volume); a frequency or volume of
# 0 is silence. Volume is the PWM duty cycle in percent: 50 is a passive
# buzzer's loudest.
Sound = list[tuple[float, float, float]]
DEFAULT_SOUNDS: dict[str, Sound] = {
    "waiting": [(1760, 0.1, 15)],  # A6 for 0.1s, as SR's power board plays
    "start": [(1047, 0.06, 8), (1319, 0.06, 8), (1568, 0.10, 8)],
    "crash": [(2093, 0.06, 15), (0, 0.06, 0)] * 6,
}
DEFAULT_CODE_VOLUME = 50
SILENCE: Sound = []
# The low battery alarm: 1 second of fast beeps, played again until the battery recovers.
LOW_BATTERY_SOUND: Sound = [(3000, 0.05, 50), (0, 0.05, 0)] * 10


def is_competition_usb(path: str) -> bool:
    """
    Whether a mounted disk is a competition USB, by the same rule as Astoria: it has an
    astoria.json, and no robot.py to make it a code USB. astdiskd only publishes paths.
    """
    disk = Path(path)
    try:
        return (disk / "astoria.json").exists() and not (disk / "robot.py").exists()
    except OSError:
        return False


class RobotStatus:
    """Track Astoria's code status and the robot's start state."""

    def __init__(self, is_comp_usb=is_competition_usb) -> None:
        self._is_comp_usb = is_comp_usb
        self.comp_usb_present = False
        self.comp_usb_flash_until = 0.0
        self.competition_mode = False
        self._lock = threading.Lock()
        self.connected = False
        self.astprocd_running = False
        self.code_status: str | None = None
        self.usb_present = False
        self.waiting_for_start = False
        self.sounds = dict(DEFAULT_SOUNDS)

    def _update_disks(self, disks: object, now: float) -> None:
        paths = disks.values() if isinstance(disks, dict) else ()
        present = any(self._is_comp_usb(str(path)) for path in paths)
        if present and not self.comp_usb_present:
            self.comp_usb_flash_until = now + COMP_USB_FLASH_SECONDS
        self.comp_usb_present = present

    def handle(self, topic: str, payload: bytes) -> Sound | None:
        """Update the status from an MQTT message; return a sound to play, if any."""
        try:
            message = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(message, dict):
            return None
        with self._lock:
            if topic == ASTPROCD_TOPIC:
                self.astprocd_running = message.get("status") == "RUNNING"
                self.usb_present = message.get("disk_info") is not None
                code_status = message.get("code_status")
                changed = code_status != self.code_status
                if changed:
                    # A new run (or the end of one) resets the start state.
                    self.waiting_for_start = False
                self.code_status = code_status
                if changed and code_status == "code_crashed":
                    return self.sounds["crash"]
                if code_status not in ("code_starting", "code_running", "code_crashed"):
                    # Finished, killed or no code: stop any tone the code left
                    # playing. Done on every such message, not just on a change,
                    # so a missed status change can't leave the buzzer on.
                    return SILENCE
            elif topic == ASTDISKD_TOPIC:
                self._update_disks(message.get("disks"), time.monotonic())
            elif topic == ASTMETAD_TOPIC:
                metadata = message.get("metadata")
                self.competition_mode = isinstance(metadata, dict) and metadata.get("mode") == "COMP"
            elif topic == ROBOT_STATE_TOPIC:
                state = message.get("data")
                was_waiting = self.waiting_for_start
                self.waiting_for_start = state == "waiting_start"
                if self.waiting_for_start and not was_waiting:
                    return self.sounds["waiting"]
                if state == "started" and was_waiting:
                    return self.sounds["start"]
        return None

    @property
    def code_active(self) -> bool:
        with self._lock:
            return self.code_status in CODE_ACTIVE

    def set_connected(self, connected: bool) -> None:
        with self._lock:
            self.connected = connected

    def pattern(self, now: float | None = None) -> LedPattern:
        now = time.monotonic() if now is None else now
        with self._lock:
            if not (self.connected and self.astprocd_running):
                return BOOTING
            # Plugging in a competition USB always shows, whatever the robot is doing.
            if now < self.comp_usb_flash_until:
                return COMP_USB
            if self.code_status == "code_crashed":
                return CRASHED
            if self.code_status in ("code_starting", "code_running"):
                if not self.waiting_for_start:
                    return RUNNING
                return COMP_WAITING_FOR_START if self.competition_mode else WAITING_FOR_START
            if not self.usb_present:
                return COMP_USB if self.comp_usb_present else NO_USB
            # Finished or killed: ready for the next run.
            return READY


def buzz_request(payload: bytes, volume: float = DEFAULT_CODE_VOLUME) -> Sound | None:
    """Turn a robot.buzzer.buzz() request into a sound, ignoring invalid ones."""
    try:
        message = json.loads(payload)
        frequency = float(message["frequency"])
        duration = float(message["duration"])
        volume = float(message.get("volume", volume))
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    if not (8 <= frequency <= 10_000 and 0 <= duration <= 60 and 0 <= volume <= 50):
        return None
    return [(frequency, duration, volume)]


def scaled_duty(duty: float, volume: float) -> float:
    """
    The PWM duty cycle (0 to 50) that plays a tone at a share (0 to 1) of the
    loudness duty gives. A square wave's fundamental is proportional to
    sin(pi * duty), so scaling the duty itself would barely change the loudness.
    """
    if volume >= 1:
        return duty
    if volume <= 0 or duty <= 0:
        return 0.0
    amplitude = volume * math.sin(math.pi * min(duty, 50) / 100)
    return math.asin(amplitude) / math.pi * 100


def level_to_share(level: int) -> float:
    """
    A web UI setting (0 to 100%) as a share of the brightness, squared, as the eye
    sees an LED at a quarter of its power as about half as bright.
    """
    return (level / 100) ** 2


class Settings:
    """
    The web UI's buzzer volume and LED brightness (each 0 to 100%), saved to a file.

    In competition mode both are 100% (the levels in /etc/psc-status.toml) and can't
    be changed; the saved values come back when it ends.
    """

    def __init__(self, path: Path = SETTINGS_FILE) -> None:
        self._path = path
        self.saved = dict(DEFAULT_SETTINGS)
        try:
            stored = json.loads(path.read_text())
            for name in DEFAULT_SETTINGS:
                value = stored.get(name) if isinstance(stored, dict) else None
                if isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 100:
                    self.saved[name] = value
        except (OSError, ValueError):
            pass

    def effective(self, locked: bool) -> dict:
        return dict(DEFAULT_SETTINGS) if locked else dict(self.saved)

    def update(self, payload: bytes) -> dict | None:
        """Apply a change from the web UI; return what changed, or None if it's invalid."""
        try:
            request = json.loads(payload)
        except ValueError:
            return None
        if not isinstance(request, dict):
            return None
        changed = {}
        for name in DEFAULT_SETTINGS:
            value = request.get(name)
            if value is None:
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 100:
                return None
            if round(value) != self.saved[name]:
                changed[name] = round(value)
        if changed:
            self.saved.update(changed)
            self._save()
        return changed

    def _save(self) -> None:
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._path.with_suffix(".tmp")
            temporary.write_text(json.dumps(self.saved) + "\n")
            os.replace(temporary, self._path)
        except OSError as error:
            print(f"Failed to save the settings to {self._path}: {error}", flush=True)


class Ads1115:
    """The HAT's ADS1115, read in single-shot mode on AIN0 at +-4.096V."""

    I2C_SLAVE = 0x0703
    # Config register: start a conversion, AIN0 against GND, +-4.096V, single shot,
    # 128 samples a second, comparator off.
    CONFIG = bytes([0x01, 0xC3, 0x83])

    def __init__(self, bus: int, address: int, scale: float) -> None:
        self._scale = scale
        self._fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self._fd, self.I2C_SLAVE, address)
            self.read()  # fails here if there's no ADS1115
        except OSError:
            os.close(self._fd)
            raise

    def read(self) -> float:
        """The battery's voltage."""
        os.write(self._fd, self.CONFIG)
        time.sleep(0.01)  # one conversion takes about 8ms
        os.write(self._fd, bytes([0x00]))
        raw = struct.unpack(">h", os.read(self._fd, 2))[0]
        return raw * 4.096 / 32768 * self._scale

    def close(self) -> None:
        os.close(self._fd)


class BatteryAlarm:
    """
    Whether the battery is low: below low_voltage for low_seconds, so a moment's
    sag under load doesn't trip it. It clears once the voltage is above
    recover_voltage for as long. Readings below 5V mean no battery is connected
    (the Pi powered another way), which isn't an alarm.
    """

    NO_BATTERY = 5.0

    def __init__(self, low_voltage: float, recover_voltage: float, low_seconds: float) -> None:
        self.low_voltage = low_voltage
        self.recover_voltage = recover_voltage
        self.low_seconds = low_seconds
        self.low = False
        self._since: float | None = None

    def update(self, voltage: float | None, now: float) -> bool:
        """Add a reading (None if unavailable); return whether the battery is low."""
        if voltage is None or voltage < self.NO_BATTERY:
            self.low, self._since = False, None
            return self.low
        crossing = voltage > self.recover_voltage if self.low else voltage < self.low_voltage
        if not crossing:
            self._since = None
        elif self._since is None:
            self._since = now
        elif now - self._since >= self.low_seconds:
            self.low, self._since = not self.low, None
        return self.low


def kill_request() -> str:
    return json.dumps({"uuid": str(uuid.uuid4()), "sender_name": "psc-statusd"})


def tighten_timer_slack() -> None:
    """
    Make sleeps in this thread, and threads it starts, end on time (Linux only).

    RPi.GPIO times its software PWM with nanosleep, which Linux lets overrun by up to
    the thread's timer slack, 50 us by default. That stretches every short pulse to
    about 100 us, so all the quieter volumes (short pulses) sounded the same. The PWM
    thread takes its timer slack from the thread that starts it.
    """
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(29, 1, 0, 0, 0)  # PR_SET_TIMERSLACK, 1 ns
    except (OSError, AttributeError):
        pass


class BuzzerPlayer:
    """
    Plays sounds on the buzzer in a background thread.

    Starting a sound replaces the one playing, as robot.buzzer.buzz() promises.
    A passive buzzer is driven with a square wave at the tone's frequency; an
    active buzzer can only be switched on and off, at its own pitch.
    """

    def __init__(self, gpio, pin: int, passive: bool) -> None:
        self._gpio = gpio
        self._pin = pin
        self._passive = passive
        # The web UI's volume, from 0 to 1, as a share of each sound's loudness.
        self.volume = 1.0
        self._requests: queue.Queue[tuple[Sound, bool] | None] = queue.Queue()
        gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
        self._pwm = gpio.PWM(pin, 1000) if passive else None
        if self._pwm is not None:
            tighten_timer_slack()  # before start(), which starts RPi.GPIO's PWM thread
            self._pwm.start(0)
        self._thread = threading.Thread(target=self._run, name="buzzer", daemon=True)
        self._thread.start()

    def play(self, sound: Sound, full_volume: bool = False) -> None:
        """Play a sound, at the web UI's volume unless full_volume (for alarms)."""
        self._requests.put((sound, full_volume))

    def close(self) -> None:
        self._requests.put(None)
        self._thread.join(timeout=1)
        self._silence()
        if self._pwm is not None:
            self._pwm.stop()

    def _silence(self) -> None:
        if self._pwm is not None:
            self._pwm.ChangeDutyCycle(0)
        # At 0% duty the PWM never drives the pin high; drive it low too, so the
        # buzzer is definitely off whatever state the pin was left in.
        self._gpio.output(self._pin, self._gpio.LOW)

    def _tone(self, frequency: float, volume: float) -> None:
        if self._pwm is not None:
            self._pwm.ChangeFrequency(frequency)
            self._pwm.ChangeDutyCycle(volume)
        else:
            self._gpio.output(self._pin, self._gpio.HIGH)

    def _run(self) -> None:
        request = self._requests.get()
        while request is not None:
            sound, full_volume = request
            replaced = False
            for frequency, duration, volume in sound:
                if not full_volume:
                    volume = scaled_duty(volume, self.volume)
                if frequency > 0 and volume > 0:
                    self._tone(frequency, volume)
                else:
                    self._silence()
                try:
                    # Wait out the note, unless a new sound replaces this one.
                    request = self._requests.get(timeout=duration)
                    replaced = True
                    break
                except queue.Empty:
                    pass
            if not replaced:
                self._silence()
                request = self._requests.get()


class SpiLeds:
    """
    A chain of WS2812 LEDs on SPI0 MOSI (GPIO 10).

    Each WS2812 bit is sent as 4 SPI bits at about 3.2MHz: a 0 is high for one
    SPI bit (about 310ns) then low for three, a 1 is high for three then low.
    The SPI clock comes from the core clock, which the image fixes (core_freq).
    """

    SPI_IOC_WR_MODE = 0x40016B01
    SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
    SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04
    SPI_HZ = 3_200_000
    # Low bytes before and after the data: the LEDs latch the colours after 280us
    # low. This also makes every transfer long enough for the SPI driver to send it
    # by DMA, without gaps.
    RESET_BYTES = 128
    MAX_TRANSFER = 4096  # the spidev driver's default largest transfer
    MAX_COUNT = (MAX_TRANSFER - 2 * RESET_BYTES) // 12

    pin = 10

    def __init__(self, device: str, count: int) -> None:
        self.count = count
        self._fd = os.open(device, os.O_WRONLY)
        try:
            fcntl.ioctl(self._fd, self.SPI_IOC_WR_MODE, struct.pack("B", 0))
            fcntl.ioctl(self._fd, self.SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
            fcntl.ioctl(self._fd, self.SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", self.SPI_HZ))
        except OSError:
            os.close(self._fd)
            raise
        self._encoded = [self._encode(value) for value in range(256)]

    @staticmethod
    def _encode(value: int) -> bytes:
        bits = 0
        for i in range(7, -1, -1):
            bits = (bits << 4) | (0b1110 if value >> i & 1 else 0b1000)
        return bits.to_bytes(4, "big")

    def show(self, colours: list[tuple[int, int, int]]) -> None:
        data = bytearray(self.RESET_BYTES)
        for red, green, blue in colours[:self.count]:
            for value in (green, red, blue):  # WS2812s take green first
                data += self._encoded[value]
        data += bytes(self.RESET_BYTES)
        os.write(self._fd, data)

    def close(self) -> None:
        try:
            self.show([(0, 0, 0)] * self.count)
        finally:
            os.close(self._fd)


class PwmLeds:
    """
    A chain of WS2812 LEDs on GPIO 18, driven by the Pi's PWM hardware through
    rpi_ws281x. Needs root, and the analogue audio off (dtparam=audio=off), as
    that also uses the PWM hardware.
    """

    pin = 18
    DMA_CHANNEL = 10

    def __init__(self, count: int) -> None:
        from rpi_ws281x import PixelStrip, WS2811_STRIP_GRB

        self.count = count
        self._strip = PixelStrip(count, self.pin, 800_000, self.DMA_CHANNEL, False, 255, 0,
                                 WS2811_STRIP_GRB)
        try:
            self._strip.begin()  # raises RuntimeError if it can't
        except Exception:
            # Its exit handler would free hardware it never set up, and crash.
            atexit.unregister(self._strip._cleanup)
            raise

    def show(self, colours: list[tuple[int, int, int]]) -> None:
        for i in range(self.count):
            red, green, blue = colours[i] if i < len(colours) else (0, 0, 0)
            self._strip.setPixelColorRGB(i, red, green, blue)
        self._strip.show()

    def close(self) -> None:
        self.show([])
        # rpi_ws281x has no close; its exit handler releases the hardware.


class LedChain:
    """
    What the WS2812 chain shows: the status on the first LED (if status_led is on)
    and robot code's colours on the rest. Writes the LEDs when something changes,
    and again on every status update (20 times a second, for smooth effects), so
    LEDs connected or glitched after a change still show the right colours.

    The web UI's brightness scales both. An alarm colour, when set, shows on every
    LED at the configured status brightness, whatever the web UI's brightness.
    """

    def __init__(self, strips: list, status_led: bool, status_brightness: float,
                 brightness: float) -> None:
        self._strips = strips
        self.count = max(strip.count for strip in strips)
        self._status_led = status_led
        self._status_brightness = status_brightness
        self._brightness = brightness
        self._scale = 1.0
        self._lock = threading.Lock()
        self._status = (0.0, 0.0, 0.0)
        self._user = [(0, 0, 0)] * (self.count - 1 if status_led else self.count)
        self._alarm: tuple[float, float, float] | None = None
        self._shown: list[tuple[int, int, int]] | None = None

    def set_scale(self, scale: float) -> None:
        """Set the web UI's brightness, from 0 to 1."""
        with self._lock:
            self._scale = scale
        self._show(refresh=True)

    def set_status(self, colour: tuple[float, float, float]) -> None:
        """Set the status LED to (red, green, blue), each from 0 to 255."""
        with self._lock:
            self._status = colour
        self._show(refresh=True)

    def set_alarm(self, colour: tuple[float, float, float] | None) -> None:
        """Show a colour on every LED (each part 0 to 255), or None to stop."""
        with self._lock:
            self._alarm = colour
        self._show(refresh=True)

    def set_user(self, payload: bytes) -> None:
        """Set robot code's LED from a robot.brain.led message, ignoring invalid ones."""
        try:
            colours = json.loads(payload)["colours"]
            colours = [tuple(int(part) for part in colour) for colour in colours]
        except (ValueError, TypeError, KeyError):
            return
        if not all(len(c) == 3 and all(0 <= part <= 255 for part in c) for c in colours):
            return
        with self._lock:
            for i, colour in enumerate(colours[:len(self._user)]):
                self._user[i] = colour
        self._show()

    def clear_user(self) -> None:
        with self._lock:
            self._user = [(0, 0, 0)] * len(self._user)
        self._show()

    def colours(self) -> list[tuple[int, int, int]]:
        """The colours to send, first LED first. Call with the lock held."""
        def dim(colour, level):
            return tuple(round(part * level) for part in colour)

        if self._alarm is not None:
            return [dim(self._alarm, self._status_brightness)] * self.count
        status = [dim(self._status, self._status_brightness * self._scale)] if self._status_led else []
        return status + [dim(colour, self._brightness * self._scale) for colour in self._user]

    def _show(self, refresh: bool = False) -> None:
        with self._lock:
            colours = self.colours()
            if colours == self._shown and not refresh:
                return
            self._shown = colours
            for strip in self._strips:
                try:
                    strip.show(colours)
                except Exception as error:
                    print(f"Failed to set the LED chain on GPIO {strip.pin}: {error}", flush=True)

    def close(self) -> None:
        with self._lock:
            for strip in self._strips:
                try:
                    strip.close()
                except Exception as error:
                    print(f"Failed to turn off the LED chain on GPIO {strip.pin}: {error}",
                          flush=True)


def open_led_chain(config: dict) -> LedChain | None:
    """
    The WS2812 chain on each of its pins that works, or None if it's turned off or
    no pin works.
    """
    strip = config["led_strip"]
    if not strip["enabled"] or strip["count"] == 0:
        return None
    strips = []
    for pin in strip["pins"]:
        try:
            if pin == SpiLeds.pin:
                strips.append(SpiLeds(strip["device"], strip["count"]))
            else:
                strips.append(PwmLeds(strip["count"]))
        except Exception as error:
            hint = ("check dtparam=spi=on is in /boot/config.txt" if pin == SpiLeds.pin
                    else "check rpi_ws281x is installed and dtparam=audio=off")
            print(f"WS2812 LED chain not on GPIO {pin} ({error}); {hint}", flush=True)
    if not strips:
        return None
    print(f"WS2812 LED chain: {strip['count']} LEDs on GPIO "
          f"{' and '.join(str(s.pin) for s in strips)}", flush=True)
    return LedChain(strips, strip["status_led"], strip["status_brightness"],
                    strip["brightness"])


def load_config(path: Path = CONFIG_FILE) -> dict:
    config = tomllib.loads(path.read_text())
    button = config["start_button"]
    if not isinstance(button.get("pin"), int):
        raise ValueError("start_button.pin must be a BCM pin number")
    buzzer = config.setdefault("buzzer", {})
    if "pin" in buzzer and not isinstance(buzzer["pin"], int):
        raise ValueError("buzzer.pin must be a BCM pin number")
    if buzzer.get("type", "passive") not in ("passive", "active"):
        raise ValueError('buzzer.type must be "passive" or "active"')
    volume = buzzer.setdefault("code_volume", DEFAULT_CODE_VOLUME)
    if not isinstance(volume, (int, float)) or not 0 <= volume <= 50:
        raise ValueError("buzzer.code_volume must be from 0 to 50")
    strip = config.setdefault("led_strip", {})
    strip.setdefault("enabled", True)
    strip.setdefault("count", 2)
    strip.setdefault("status_led", True)
    strip.setdefault("status_brightness", 0.2)
    strip.setdefault("brightness", 1.0)
    strip.setdefault("pins", [18])
    strip.setdefault("device", "/dev/spidev0.0")
    pins = strip["pins"]
    if not isinstance(pins, list) or not set(pins) <= {10, 18} or len(set(pins)) != len(pins):
        raise ValueError("led_strip.pins must list GPIO 18, GPIO 10 or both")
    for name in ("enabled", "status_led"):
        if not isinstance(strip[name], bool):
            raise ValueError(f"led_strip.{name} must be true or false")
    count = strip["count"]
    if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= SpiLeds.MAX_COUNT:
        raise ValueError(f"led_strip.count must be from 0 to {SpiLeds.MAX_COUNT}")
    for name in ("status_brightness", "brightness"):
        value = strip[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 <= value <= 1:
            raise ValueError(f"led_strip.{name} must be from 0 to 1")
    battery = config.setdefault("battery", {})
    for name, default in (("enabled", True), ("bus", 1), ("address", 0x48), ("scale", 6.0),
                          ("low_voltage", 10.5), ("recover_voltage", 11.0), ("low_seconds", 5.0)):
        battery.setdefault(name, default)
    if not isinstance(battery["enabled"], bool):
        raise ValueError("battery.enabled must be true or false")
    for name in ("bus", "address"):
        if isinstance(battery[name], bool) or not isinstance(battery[name], int):
            raise ValueError(f"battery.{name} must be a whole number")
    for name in ("scale", "low_voltage", "recover_voltage", "low_seconds"):
        value = battery[name]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
            raise ValueError(f"battery.{name} must be a number, 0 or more")
    if battery["recover_voltage"] < battery["low_voltage"]:
        raise ValueError("battery.recover_voltage must be at least battery.low_voltage")
    sounds = buzzer.setdefault("sounds", {})
    for name in sounds:
        if name not in DEFAULT_SOUNDS:
            raise ValueError(f"buzzer.sounds.{name} isn't a sound; use waiting, start or crash")
    buzzer["sounds"] = {name: parse_sound(sounds.get(name, default), name)
                        for name, default in DEFAULT_SOUNDS.items()}
    return config


def parse_sound(steps, name: str) -> Sound:
    """Check a sound from the config file: a list of [frequency, seconds, volume]."""
    sound = []
    for step in steps:
        if (
            not isinstance(step, (list, tuple)) or len(step) != 3
            or not all(isinstance(value, (int, float)) for value in step)
        ):
            raise ValueError(f"buzzer.sounds.{name} steps must be [frequency, seconds, volume]")
        frequency, seconds, volume = step
        if not (frequency == 0 or 8 <= frequency <= 10_000):
            raise ValueError(f"buzzer.sounds.{name}: frequency must be 0 or 8 to 10000")
        if not 0 <= seconds <= 10:
            raise ValueError(f"buzzer.sounds.{name}: seconds must be 0 to 10")
        if not 0 <= volume <= 50:
            raise ValueError(f"buzzer.sounds.{name}: volume must be 0 to 50")
        sound.append((float(frequency), float(seconds), float(volume)))
    return sound


class MotorGuard:
    """Stop the DC motors robot code drove from the Pi's pins, when the code stops.

    The robot library publishes the pins to psc/motors. Its own exit handlers stop
    the motors, but they can't run if the code is killed outright (SIGKILL), and the
    pins would then keep whatever level they had.
    """

    def __init__(self, gpio, own_pins: set[int]) -> None:
        self._gpio = gpio
        self._own_pins = own_pins
        self._lock = threading.Lock()
        self._pins: list[int] = []

    def update(self, payload: bytes) -> None:
        try:
            pins = json.loads(json.loads(payload)["data"])["pi"]
        except (ValueError, KeyError, TypeError):
            return
        if not isinstance(pins, list):
            return
        with self._lock:
            self._pins = [
                pin for pin in pins
                if isinstance(pin, int) and 2 <= pin <= 27 and pin not in self._own_pins
            ]

    def stop(self) -> None:
        with self._lock:
            pins = list(self._pins)
        for pin in pins:
            try:
                # An input with a pull-down: the DRV8871's inputs read low, so it coasts.
                self._gpio.setup(pin, self._gpio.IN, pull_up_down=self._gpio.PUD_DOWN)
            except Exception as error:
                print(f"Failed to stop the motor on GPIO {pin}: {error}", flush=True)
        if pins:
            print(f"Stopped DC motors on GPIO {pins}", flush=True)


def start_event() -> str:
    return json.dumps({"event_name": "start_button", "sender_name": "psc-statusd", "priority": 0})


def open_battery(config: dict) -> Ads1115 | None:
    battery = config["battery"]
    if not battery["enabled"]:
        return None
    try:
        return Ads1115(battery["bus"], battery["address"], battery["scale"])
    except OSError:
        return None


def main() -> None:
    import paho.mqtt.client as mqtt
    import RPi.GPIO as GPIO

    config = load_config()
    button = config["start_button"]
    status = RobotStatus()
    status.sounds = config["buzzer"]["sounds"]
    settings = Settings()
    battery_config = config["battery"]
    alarm = BatteryAlarm(battery_config["low_voltage"], battery_config["recover_voltage"],
                         battery_config["low_seconds"])
    stop = threading.Event()

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    pull = GPIO.PUD_UP if button.get("pull", "up") == "up" else GPIO.PUD_DOWN
    GPIO.setup(button["pin"], GPIO.IN, pull_up_down=pull)
    buzzer_config = config["buzzer"]
    buzzer = BuzzerPlayer(GPIO, buzzer_config.get("pin", 13),
                          passive=buzzer_config.get("type", "passive") == "passive")
    motors = MotorGuard(GPIO, {button["pin"], buzzer_config.get("pin", 13)})
    leds = open_led_chain(config)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="psc-statusd")
    client.reconnect_delay_set(min_delay=1, max_delay=5)
    applied: dict = {}

    def apply_settings() -> None:
        """Use the settings for the current mode, and publish them for the web UI."""
        locked = status.competition_mode
        current = settings.effective(locked)
        buzzer.volume = current["buzzer_volume"] / 100
        if leds is not None:
            leds.set_scale(level_to_share(current["led_brightness"]))
        state = dict(current, locked=locked)
        if state != applied.get("state") or not applied.get("published"):
            applied["state"] = state
            applied["published"] = status.connected
            client.publish(SETTINGS_TOPIC, json.dumps(state), qos=1, retain=True)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            return
        status.set_connected(True)
        client.subscribe([(ASTPROCD_TOPIC, 0), (ASTDISKD_TOPIC, 0), (ASTMETAD_TOPIC, 0),
                          (ROBOT_STATE_TOPIC, 0), (BUZZER_TOPIC, 0),
                          (MOTORS_TOPIC, 0), (LEDS_TOPIC, 0), (SETTINGS_SET_TOPIC, 0)])
        applied["published"] = False
        apply_settings()

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        status.set_connected(False)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    def on_message(client, userdata, msg):
        if msg.topic == MOTORS_TOPIC:
            motors.update(msg.payload)
            return
        if msg.topic == LEDS_TOPIC:
            # Only while code runs, so a late message can't light them after it ends.
            if leds is not None and status.code_active:
                leds.set_user(msg.payload)
            return
        if msg.topic == SETTINGS_SET_TOPIC:
            # Locked in competition mode: the competition USB's levels stay.
            changed = None if status.competition_mode else settings.update(msg.payload)
            apply_settings()
            if changed and "buzzer_volume" in changed and not alarm.low:
                buzzer.play(status.sounds["waiting"])  # so the new volume can be heard
            return
        if msg.topic == BUZZER_TOPIC:
            sound = buzz_request(msg.payload, buzzer_config["code_volume"])
        else:
            was_active = status.code_active
            was_comp = status.competition_mode
            sound = status.handle(msg.topic, msg.payload)
            if was_active and not status.code_active:
                motors.stop()
                if leds is not None:
                    leds.clear_user()
            if status.competition_mode != was_comp:
                apply_settings()
        # The low battery alarm's beeping isn't interrupted by other sounds.
        if sound is not None and not alarm.low:
            buzzer.play(sound)

    client.on_message = on_message

    def on_button(channel: int) -> None:
        # Pressed means pulled to the opposite of the idle level.
        pressed_level = GPIO.LOW if pull == GPIO.PUD_UP else GPIO.HIGH
        if GPIO.input(channel) == pressed_level and status.connected:
            client.publish(START_BUTTON_TOPIC, start_event(), qos=1)
            print("Start button pressed", flush=True)

    edge = GPIO.FALLING if pull == GPIO.PUD_UP else GPIO.RISING
    GPIO.add_event_detect(button["pin"], edge, callback=on_button, bouncetime=200)

    def watch_battery() -> None:
        """Read the battery each second, publish it, and run the low battery alarm."""
        monitor = None
        next_open = next_publish = next_kill = next_beep = 0.0
        last: dict = {}
        while not stop.is_set():
            now = time.monotonic()
            if monitor is None and battery_config["enabled"] and now >= next_open:
                monitor = open_battery(config)
                next_open = now + 30  # look for it again later, if it isn't there
                if monitor is not None:
                    print("Battery monitor: ADS1115 found", flush=True)
            voltage = None
            if monitor is not None:
                try:
                    voltage = monitor.read()
                except OSError as error:
                    print(f"Battery monitor stopped responding: {error}", flush=True)
                    monitor.close()
                    monitor = None
            was_low = alarm.low
            low = alarm.update(voltage, now)
            if low != was_low:
                reading = "no reading" if voltage is None else f"{voltage:.2f}V"
                print(f"Battery {'low' if low else 'recovered'}: {reading}", flush=True)
                if leds is not None:
                    leds.set_alarm(None)
                if not low:
                    buzzer.play(SILENCE, full_volume=True)
            if low:
                if leds is not None:
                    leds.set_alarm(LOW_BATTERY.chain(now))
                if now >= next_beep:
                    buzzer.play(LOW_BATTERY_SOUND, full_volume=True)
                    next_beep = now + sum(step[1] for step in LOW_BATTERY_SOUND)
                if status.code_active and status.connected and now >= next_kill:
                    client.publish(KILL_TOPIC, kill_request(), qos=1)
                    print("Battery low: stopping robot code", flush=True)
                    next_kill = now + 3
            state = {"voltage": None if voltage is None else round(voltage, 2), "low": low,
                     "low_voltage": battery_config["low_voltage"]}
            moved = (voltage is not None and last.get("voltage") is not None
                     and abs(voltage - last["voltage"]) >= 0.05)
            if status.connected and (state["low"] != last.get("low") or moved
                                     or (state["voltage"] is None) != (last.get("voltage") is None)
                                     or now >= next_publish):
                client.publish(BATTERY_TOPIC, json.dumps(state), qos=0, retain=True)
                last = state
                next_publish = now + 10
            stop.wait(1.0 if not low else TICK)
        if monitor is not None:
            monitor.close()

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    apply_settings()  # the saved levels from the start, before MQTT connects
    client.connect_async(config.get("mqtt", {}).get("host", "localhost"),
                         config.get("mqtt", {}).get("port", 1883))
    client.loop_start()
    battery_thread = threading.Thread(target=watch_battery, name="battery", daemon=True)
    battery_thread.start()

    print(f"psc-statusd: start button = BCM {button['pin']}, "
          f"buzzer = BCM {buzzer_config.get('pin', 13)} ({buzzer_config.get('type', 'passive')})", flush=True)
    try:
        while not stop.is_set():
            if leds is not None:
                leds.set_status(status.pattern().chain(time.monotonic()))
            stop.wait(TICK)
    finally:
        battery_thread.join(timeout=2)
        client.loop_stop()
        buzzer.close()
        if leds is not None:
            leds.close()
        GPIO.cleanup()


if __name__ == "__main__":
    main()
