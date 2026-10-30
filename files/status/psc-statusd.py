#!/usr/bin/env python3
"""PSC Robotics status LED, buzzer and start button service.

Shows the robot's state on an RGB LED, turns presses of a GPIO start button
into the same Astoria start event that the web UI's Start button sends, and
plays sounds on the buzzer: system sounds, and tones requested by robot code
through robot.buzzer.

    Booting            blue, flashing
    Ready              green
    Waiting for start  green, flashing
    Code running       blue
    Code crashed       red

Sounds (defaults; each can be changed in /etc/psc-status.toml):

    Waiting for start  one beep (A6, as Student Robotics uses)
    Start pressed      three rising notes
    Code crashed       fast short beeps

It also stops DC motors that robot code drove from the Pi's pins when the code
stops, in case the code was killed before it could stop them itself.

A chain of WS2812 ("NeoPixel") LEDs shows the same status on its first LED, and
the colours robot code sets (robot.leds) on the rest. Robot code's LEDs turn off
when the code stops. The chain's data is sent on GPIO 18 (PWM, as the Brain Board
HAT is wired) and GPIO 10 (SPI MOSI) at once, so it works on either pin.

Pins, LED polarity, the LED chain, buzzer type, volume and sounds are set in
/etc/psc-status.toml.
"""
from __future__ import annotations

import atexit
import fcntl
import json
import os
import queue
import signal
import struct
import threading
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path

CONFIG_FILE = Path(os.environ.get("PSC_STATUS_CONFIG", "/etc/psc-status.toml"))
ASTPROCD_TOPIC = "astoria/astprocd"
ROBOT_STATE_TOPIC = "psc/robot/state"
START_BUTTON_TOPIC = "astoria/broadcast/start_button"
BUZZER_TOPIC = "psc/buzzer"
MOTORS_TOPIC = "psc/motors"
LEDS_TOPIC = "psc/leds"
CODE_ACTIVE = ("code_starting", "code_running")

OFF = (False, False, False)
RED = (True, False, False)
GREEN = (False, True, False)
BLUE = (False, False, True)


@dataclass(frozen=True)
class LedPattern:
    colour: tuple[bool, bool, bool]
    flashing: bool = False


BOOTING = LedPattern(BLUE, flashing=True)
READY = LedPattern(GREEN)
WAITING_FOR_START = LedPattern(GREEN, flashing=True)
RUNNING = LedPattern(BLUE)
CRASHED = LedPattern(RED)

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


class RobotStatus:
    """Track Astoria's code status and the robot's start state."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.connected = False
        self.astprocd_running = False
        self.code_status: str | None = None
        self.waiting_for_start = False
        self.sounds = dict(DEFAULT_SOUNDS)

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

    def pattern(self) -> LedPattern:
        with self._lock:
            if not (self.connected and self.astprocd_running):
                return BOOTING
            if self.code_status == "code_crashed":
                return CRASHED
            if self.code_status in ("code_starting", "code_running"):
                return WAITING_FOR_START if self.waiting_for_start else RUNNING
            # No code, finished or killed: ready for the next run.
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
        self._requests: queue.Queue[Sound | None] = queue.Queue()
        gpio.setup(pin, gpio.OUT, initial=gpio.LOW)
        self._pwm = gpio.PWM(pin, 1000) if passive else None
        if self._pwm is not None:
            self._pwm.start(0)
        self._thread = threading.Thread(target=self._run, name="buzzer", daemon=True)
        self._thread.start()

    def play(self, sound: Sound) -> None:
        self._requests.put(sound)

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
        sound: Sound | None = self._requests.get()
        while sound is not None:
            replaced = False
            for frequency, duration, volume in sound:
                if frequency > 0 and volume > 0:
                    self._tone(frequency, volume)
                else:
                    self._silence()
                try:
                    # Wait out the note, unless a new sound replaces this one.
                    sound = self._requests.get(timeout=duration)
                    replaced = True
                    break
                except queue.Empty:
                    pass
            if not replaced:
                self._silence()
                sound = self._requests.get()


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
    and again on every status update (twice a second), so LEDs connected or
    glitched after a change still show the right colours.
    """

    def __init__(self, strips: list, status_led: bool, status_brightness: float,
                 brightness: float) -> None:
        self._strips = strips
        self.count = max(strip.count for strip in strips)
        self._status_led = status_led
        self._status_brightness = status_brightness
        self._brightness = brightness
        self._lock = threading.Lock()
        self._status = (0, 0, 0)
        self._user = [(0, 0, 0)] * (self.count - 1 if status_led else self.count)
        self._shown: list[tuple[int, int, int]] | None = None

    def set_status(self, colour: tuple[bool, bool, bool]) -> None:
        level = round(255 * self._status_brightness)
        with self._lock:
            self._status = tuple(level if lit else 0 for lit in colour)  # type: ignore[assignment]
        self._show(refresh=True)

    def set_user(self, payload: bytes) -> None:
        """Set robot code's LEDs from a robot.leds message, ignoring invalid ones."""
        try:
            colours = json.loads(payload)["colours"]
            colours = [tuple(int(part) for part in colour) for colour in colours]
        except (ValueError, TypeError, KeyError):
            return
        if not all(len(c) == 3 and all(0 <= part <= 255 for part in c) for c in colours):
            return
        with self._lock:
            for i, colour in enumerate(colours[:len(self._user)]):
                self._user[i] = tuple(round(part * self._brightness) for part in colour)
        self._show()

    def clear_user(self) -> None:
        with self._lock:
            self._user = [(0, 0, 0)] * len(self._user)
        self._show()

    def _show(self, refresh: bool = False) -> None:
        with self._lock:
            colours = ([self._status] if self._status_led else []) + self._user
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
    led, button = config["led"], config["start_button"]
    for name in ("red", "green", "blue"):
        if not isinstance(led.get(name), int):
            raise ValueError(f"led.{name} must be a BCM pin number")
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
    strip.setdefault("pins", [18, 10])
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


def main() -> None:
    import paho.mqtt.client as mqtt
    import RPi.GPIO as GPIO

    config = load_config()
    led, button = config["led"], config["start_button"]
    pins = (led["red"], led["green"], led["blue"])
    on, off = (GPIO.HIGH, GPIO.LOW) if led.get("active_high", True) else (GPIO.LOW, GPIO.HIGH)
    status = RobotStatus()
    status.sounds = config["buzzer"]["sounds"]
    stop = threading.Event()

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(list(pins), GPIO.OUT, initial=off)
    pull = GPIO.PUD_UP if button.get("pull", "up") == "up" else GPIO.PUD_DOWN
    GPIO.setup(button["pin"], GPIO.IN, pull_up_down=pull)
    buzzer_config = config["buzzer"]
    buzzer = BuzzerPlayer(GPIO, buzzer_config.get("pin", 13),
                          passive=buzzer_config.get("type", "passive") == "passive")
    motors = MotorGuard(GPIO, {*pins, button["pin"], buzzer_config.get("pin", 13)})
    leds = open_led_chain(config)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="psc-statusd")
    client.reconnect_delay_set(min_delay=1, max_delay=5)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            return
        status.set_connected(True)
        client.subscribe([(ASTPROCD_TOPIC, 0), (ROBOT_STATE_TOPIC, 0), (BUZZER_TOPIC, 0),
                          (MOTORS_TOPIC, 0), (LEDS_TOPIC, 0)])

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
        if msg.topic == BUZZER_TOPIC:
            sound = buzz_request(msg.payload, buzzer_config["code_volume"])
        else:
            was_active = status.code_active
            sound = status.handle(msg.topic, msg.payload)
            if was_active and not status.code_active:
                motors.stop()
                if leds is not None:
                    leds.clear_user()
        if sound is not None:
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

    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    signal.signal(signal.SIGINT, lambda *_: stop.set())

    client.connect_async(config.get("mqtt", {}).get("host", "localhost"),
                         config.get("mqtt", {}).get("port", 1883))
    client.loop_start()

    print(f"psc-statusd: LED red/green/blue = BCM {pins}, start button = BCM {button['pin']}, "
          f"buzzer = BCM {buzzer_config.get('pin', 13)} ({buzzer_config.get('type', 'passive')})", flush=True)
    try:
        phase = False
        while not stop.is_set():
            pattern = status.pattern()
            phase = not phase
            colour = pattern.colour if (phase or not pattern.flashing) else OFF
            GPIO.output(list(pins), [on if lit else off for lit in colour])
            if leds is not None:
                leds.set_status(colour)
            stop.wait(0.5)
    finally:
        client.loop_stop()
        buzzer.close()
        if leds is not None:
            leds.close()
        GPIO.output(list(pins), off)
        GPIO.cleanup()


if __name__ == "__main__":
    main()
