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

Pins, LED polarity, buzzer type, volume and sounds are set in
/etc/psc-status.toml.
"""
from __future__ import annotations

import json
import os
import queue
import signal
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
                # An input, so the DRV8871's inputs read low (it has pull-downs) and
                # it coasts. Then release the pin: on bookworm (lgpio) only one
                # program can hold a pin, and the next run of robot code needs it.
                self._gpio.setup(pin, self._gpio.IN, pull_up_down=self._gpio.PUD_DOWN)
                self._gpio.cleanup(pin)
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

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="psc-statusd")
    client.reconnect_delay_set(min_delay=1, max_delay=5)

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            return
        status.set_connected(True)
        client.subscribe([(ASTPROCD_TOPIC, 0), (ROBOT_STATE_TOPIC, 0), (BUZZER_TOPIC, 0),
                          (MOTORS_TOPIC, 0)])

    def on_disconnect(client, userdata, flags, reason_code, properties=None):
        status.set_connected(False)

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    def on_message(client, userdata, msg):
        if msg.topic == MOTORS_TOPIC:
            motors.update(msg.payload)
            return
        if msg.topic == BUZZER_TOPIC:
            sound = buzz_request(msg.payload, buzzer_config["code_volume"])
        else:
            was_active = status.code_active
            sound = status.handle(msg.topic, msg.payload)
            if was_active and not status.code_active:
                motors.stop()
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
            stop.wait(0.5)
    finally:
        client.loop_stop()
        buzzer.close()
        GPIO.output(list(pins), off)
        GPIO.cleanup()


if __name__ == "__main__":
    main()
