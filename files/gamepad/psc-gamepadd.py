#!/usr/bin/env python3
"""PSC Robotics gamepad service.

Pairs Bluetooth gamepads for the web interface, keeps Bluetooth working, and shows
the input from connected gamepads (Bluetooth or USB) so they can be tested.

    GET  /gamepad-api/status      Bluetooth state, nearby and paired controllers
    GET  /gamepad-api/input       What each connected gamepad is doing
    POST /gamepad-api/scan        {"on": true}: search for controllers for a while
    POST /gamepad-api/pair        {"address": ...}: pair, trust and connect
    POST /gamepad-api/disconnect  {"address": ...}
    POST /gamepad-api/forget      {"address": ...}: remove a paired controller

Only controllers paired from here are accepted: the pairing agent rejects any other
device that tries to pair or connect.

A competition USB (one with astoria.json) can switch Bluetooth off for a match with
"bluetooth_enabled": false.
"""
# No "from __future__ import annotations": dbus_next reads the agent's D-Bus
# signatures from its annotations at runtime.
import asyncio
import importlib.util
import json
import logging
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dbus_next import BusType, DBusError, Message, MessageType, Variant
from dbus_next.aio import MessageBus
from dbus_next.service import ServiceInterface, method

LOGGER = logging.getLogger("psc-gamepadd")
LISTEN = ("127.0.0.1", 8766)
BLUEZ = "org.bluez"
AGENT_PATH = "/uk/psc_robotics/agent"
ASTDISKD_TOPIC = "astoria/astdiskd"
SCAN_SECONDS = 45
REFRESH_SECONDS = 1.5
ADAPTER_MISSING_RESTART_S = 20
MAX_HCIUART_RESTARTS = 3
GAMEPAD_NAMES = re.compile(
    r"controller|gamepad|joystick|xbox|dualsense|dualshock|stadia|8bitdo|pro controller|"
    r"joy-con|wireless controller|game ?pad",
    re.IGNORECASE,
)
BAD_PAIRING_HINT = (
    "Put the controller back in pairing mode, then search and pair again. If it keeps "
    "failing, turn the controller off and on, and keep it close to the robot."
)


def load_gamepad_module():
    """Load sr.robot3.gamepad on its own, without importing the whole robot library."""
    import sr  # A namespace package once installed, so use __path__ rather than __file__

    path = next((Path(base) / "robot3" / "gamepad.py" for base in sr.__path__
                 if (Path(base) / "robot3" / "gamepad.py").exists()), None)
    if path is None:
        raise RuntimeError("Can't find sr/robot3/gamepad.py; is the robot library installed?")
    spec = importlib.util.spec_from_file_location("psc_gamepad", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules["psc_gamepad"] = module
    spec.loader.exec_module(module)
    return module


def address_of(device_path: str) -> str:
    return device_path.rsplit("dev_", 1)[-1].replace("_", ":").upper()


def unwrap(properties: dict) -> dict:
    return {key: value.value if isinstance(value, Variant) else value for key, value in properties.items()}


def is_gamepad_like(device: dict) -> bool:
    if device.get("Icon") == "input-gaming":
        return True
    if device.get("Appearance") in (0x03C3, 0x03C4):  # Joystick, Gamepad
        return True
    device_class = device.get("Class") or 0
    if (device_class >> 8) & 0x1F == 0x05 and (device_class >> 2) & 0x0F in (1, 2):
        return True
    return bool(GAMEPAD_NAMES.search(device.get("Alias") or device.get("Name") or ""))


def competition_bluetooth_enabled(mount_paths: list[str]) -> tuple[bool, str]:
    """Read "bluetooth_enabled" from any competition (metadata) USB that is plugged in."""
    try:
        from astoria.common.disks import DiskType, DiskTypeCalculator
        calculator = DiskTypeCalculator("robot.py")
    except Exception:
        LOGGER.exception("Can't load Astoria's disk types; assuming no competition USB")
        return True, ""
    for mount_path in mount_paths:
        path = Path(mount_path)
        try:
            if calculator.calculate(path) is not DiskType.METADATA:
                continue
            data = json.loads((path / "astoria.json").read_text())
        except (OSError, ValueError) as error:
            LOGGER.warning(f"Can't read the competition USB at {mount_path}: {error}")
            continue
        value = data.get("bluetooth_enabled", True) if isinstance(data, dict) else True
        if isinstance(value, str):
            value = value.strip().lower() not in ("false", "0", "no", "off")
        if value is False or value == 0:
            return False, mount_path
    return True, ""


class Agent(ServiceInterface):
    """Accepts pairing, without a PIN, only for controllers being paired from the web UI."""

    def __init__(self, service: "GamepadService") -> None:
        super().__init__("org.bluez.Agent1")
        self._service = service

    def _check(self, device: str) -> None:
        if not self._service.allowed(address_of(device)):
            LOGGER.warning(f"Rejected pairing or connection from {address_of(device)}")
            raise DBusError("org.bluez.Error.Rejected", "Pair controllers from the robot's web interface")

    @method()
    def Release(self):  # noqa: N802
        pass

    @method()
    def RequestPinCode(self, device: "o") -> "s":  # noqa: N802,F821
        self._check(device)
        return "0000"

    @method()
    def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802,F821
        self._check(device)

    @method()
    def RequestPasskey(self, device: "o") -> "u":  # noqa: N802,F821
        self._check(device)
        return 0

    @method()
    def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802,F821
        self._check(device)

    @method()
    def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802,F821
        self._check(device)

    @method()
    def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
        self._check(device)

    @method()
    def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
        self._check(device)

    @method()
    def Cancel(self):  # noqa: N802
        pass


class GamepadService:
    def __init__(self, gamepad_module) -> None:
        self.loop: asyncio.AbstractEventLoop | None = None
        self.bus: MessageBus | None = None
        self.adapter: str | None = None
        self.adapter_props: dict = {}
        self.devices: dict[str, dict] = {}  # address -> properties (and "path")
        self.jobs: dict[str, dict] = {}  # address -> {"state", "message"}
        self.pairing: set[str] = set()
        self.scan_until = 0.0
        self.blocked = False
        self.blocked_by = ""
        self.mounts: list[str] = []
        self.adapter_missing_since: float | None = time.monotonic()
        self.hciuart_restarts = 0
        self.last_restart = 0.0
        self.lock = threading.Lock()
        self.rfkill_blocked: bool | None = None
        self.rfkill_time = 0.0
        self.gamepads = gamepad_module.GamepadManager()
        self.gamepads.start()

    # ------------------------------------------------------------- D-Bus helpers
    async def call(self, path: str, interface: str, member: str, signature: str = "",
                   body: list | None = None, timeout: float = 10) -> list:
        assert self.bus is not None
        reply = await asyncio.wait_for(self.bus.call(Message(
            destination=BLUEZ, path=path, interface=interface, member=member,
            signature=signature, body=body or [],
        )), timeout)
        if reply.message_type == MessageType.ERROR:
            raise DBusError(reply.error_name, reply.body[0] if reply.body else reply.error_name)
        return reply.body

    async def set_property(self, path: str, interface: str, name: str, signature: str, value) -> None:
        await self.call(path, "org.freedesktop.DBus.Properties", "Set", "ssv",
                        [interface, name, Variant(signature, value)])

    def rfkill(self, block: bool) -> None:
        """Block or unblock Bluetooth's radio, re-checking every 30 seconds."""
        now = time.monotonic()
        if self.rfkill_blocked == block and now - self.rfkill_time < 30:
            return
        self.rfkill_blocked, self.rfkill_time = block, now
        run_command("rfkill", "block" if block else "unblock", "bluetooth")

    def allowed(self, address: str) -> bool:
        with self.lock:
            if address in self.pairing:
                return True
            device = self.devices.get(address)
        return bool(device and device.get("Trusted"))

    # ------------------------------------------------------------- state
    async def refresh(self) -> None:
        body = await self.call("/", "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        adapter, adapter_props, devices = None, {}, {}
        for path, interfaces in body[0].items():
            if "org.bluez.Adapter1" in interfaces and adapter is None:
                adapter, adapter_props = path, unwrap(interfaces["org.bluez.Adapter1"])
            if "org.bluez.Device1" in interfaces:
                properties = unwrap(interfaces["org.bluez.Device1"])
                properties["path"] = path
                devices[properties.get("Address", address_of(path)).upper()] = properties
        with self.lock:
            self.adapter, self.adapter_props, self.devices = adapter, adapter_props, devices

    async def keep_bluetooth_working(self) -> None:
        """Keep the adapter powered (or off, for a competition), and recover a missing one."""
        if self.blocked:
            if self.adapter and self.adapter_props.get("Powered"):
                await self.power(False)
            self.rfkill(True)
            return
        if self.adapter is None:
            self.rfkill(False)
            now = time.monotonic()
            if self.adapter_missing_since is None:
                self.adapter_missing_since = now
            missing_for = now - self.adapter_missing_since
            if (missing_for > ADAPTER_MISSING_RESTART_S and self.hciuart_restarts < MAX_HCIUART_RESTARTS
                    and now - self.last_restart > 30):
                self.hciuart_restarts += 1
                self.last_restart = now
                LOGGER.warning(f"No Bluetooth adapter after {missing_for:.0f}s; restarting "
                               f"hciuart (attempt {self.hciuart_restarts})")
                run_command("systemctl", "restart", "hciuart.service")
                run_command("systemctl", "restart", "bluetooth.service")
                await asyncio.sleep(5)
                await self.register_agent()
            return
        self.adapter_missing_since = None
        if not self.adapter_props.get("Powered"):
            self.rfkill(False)
            await self.power(True)
        if self.scan_until and time.monotonic() > self.scan_until:
            await self.set_scan(False)

    async def power(self, on: bool) -> None:
        try:
            await self.set_property(self.adapter, "org.bluez.Adapter1", "Powered", "b", on)
            if on:
                await self.set_property(self.adapter, "org.bluez.Adapter1", "Pairable", "b", True)
                await self.set_property(self.adapter, "org.bluez.Adapter1", "PairableTimeout", "u", 0)
                await self.set_property(self.adapter, "org.bluez.Adapter1", "Discoverable", "b", False)
            LOGGER.info(f"Bluetooth powered {'on' if on else 'off'}")
        except (DBusError, asyncio.TimeoutError) as error:
            LOGGER.warning(f"Can't power Bluetooth {'on' if on else 'off'}: {error}")

    async def register_agent(self) -> None:
        try:
            await self.call("/org/bluez", "org.bluez.AgentManager1", "RegisterAgent", "os",
                            [AGENT_PATH, "NoInputNoOutput"])
        except DBusError as error:
            if "AlreadyExists" not in str(error.type):
                LOGGER.warning(f"Can't register the pairing agent: {error}")
                return
        except asyncio.TimeoutError:
            return
        try:
            await self.call("/org/bluez", "org.bluez.AgentManager1", "RequestDefaultAgent", "o", [AGENT_PATH])
        except (DBusError, asyncio.TimeoutError) as error:
            LOGGER.warning(f"Can't make the pairing agent the default: {error}")

    async def set_scan(self, on: bool) -> None:
        if self.adapter is None:
            return
        try:
            if on:
                await self.call(self.adapter, "org.bluez.Adapter1", "SetDiscoveryFilter", "a{sv}",
                                [{"Transport": Variant("s", "auto")}])
                await self.call(self.adapter, "org.bluez.Adapter1", "StartDiscovery")
                self.scan_until = time.monotonic() + SCAN_SECONDS
            else:
                self.scan_until = 0.0
                if self.adapter_props.get("Discovering"):
                    await self.call(self.adapter, "org.bluez.Adapter1", "StopDiscovery")
        except DBusError as error:
            if "InProgress" not in str(error.type) and "NotReady" not in str(error.type):
                LOGGER.info(f"Discovery {'start' if on else 'stop'}: {error}")
        except asyncio.TimeoutError:
            pass

    # ------------------------------------------------------------- pairing
    def set_job(self, address: str, state: str, message: str = "") -> None:
        with self.lock:
            self.jobs[address] = {"state": state, "message": message, "time": time.time()}

    async def pair(self, address: str) -> None:
        with self.lock:
            device = self.devices.get(address)
            self.pairing.add(address)
        try:
            if device is None:
                self.set_job(address, "failed", "The controller isn't in range any more. Search again.")
                return
            path, was_paired = device["path"], bool(device.get("Paired"))
            self.set_job(address, "pairing", "Pairing...")
            # Searching while pairing makes connections less reliable on the Pi's radio.
            await self.set_scan(False)
            if not was_paired:
                try:
                    await self.call(path, "org.bluez.Device1", "Pair", timeout=60)
                except DBusError as error:
                    if "AlreadyExists" not in str(error.type):
                        LOGGER.warning(f"Pairing {address} failed: {error.type} {error.text}")
                        await self.remove(path)
                        self.set_job(address, "failed", f"Pairing failed ({short(error)}). {BAD_PAIRING_HINT}")
                        return
                except asyncio.TimeoutError:
                    await self.remove(path)
                    self.set_job(address, "failed", f"The controller didn't answer. {BAD_PAIRING_HINT}")
                    return
            await self.set_property(path, "org.bluez.Device1", "Trusted", "b", True)
            self.set_job(address, "connecting", "Connecting...")
            if await self.connect(path, address):
                self.set_job(address, "done", "Connected")
                LOGGER.info(f"Paired and connected {address} ({device.get('Alias')})")
            else:
                self.set_job(address, "failed",
                             "Paired, but it didn't connect. Press the controller's home button "
                             "to connect it, or forget it and pair again.")
        except Exception as error:  # Report anything else rather than leaving the job stuck
            LOGGER.exception(f"Pairing {address} failed")
            self.set_job(address, "failed", f"Pairing failed ({error}). {BAD_PAIRING_HINT}")
        finally:
            with self.lock:
                self.pairing.discard(address)

    async def connect(self, path: str, address: str) -> bool:
        for attempt in range(4):
            await self.refresh()
            if self.devices.get(address, {}).get("Connected"):
                break
            try:
                await self.call(path, "org.bluez.Device1", "Connect", timeout=25)
                break
            except DBusError as error:
                if "AlreadyConnected" in str(error.type):
                    break
                LOGGER.info(f"Connecting {address} (attempt {attempt + 1}): {short(error)}")
            except asyncio.TimeoutError:
                LOGGER.info(f"Connecting {address} (attempt {attempt + 1}) timed out")
            await asyncio.sleep(2)
        for _ in range(20):  # Wait for the link (and its input device) to be up
            await self.refresh()
            if self.devices.get(address, {}).get("Connected"):
                return True
            await asyncio.sleep(0.5)
        return False

    async def remove(self, path: str) -> None:
        if self.adapter is None:
            return
        try:
            await self.call(self.adapter, "org.bluez.Adapter1", "RemoveDevice", "o", [path])
        except (DBusError, asyncio.TimeoutError):
            pass

    async def forget(self, address: str) -> None:
        device = self.devices.get(address)
        if device is not None:
            await self.remove(device["path"])
        with self.lock:
            self.jobs.pop(address, None)
        await self.refresh()

    async def disconnect(self, address: str) -> None:
        device = self.devices.get(address)
        if device is not None:
            try:
                await self.call(device["path"], "org.bluez.Device1", "Disconnect")
            except (DBusError, asyncio.TimeoutError):
                pass
        await self.refresh()

    # ------------------------------------------------------------- competition USB
    def disks_changed(self, mounts: list[str]) -> None:
        enabled, mount_path = competition_bluetooth_enabled(mounts)
        if self.blocked != (not enabled):
            LOGGER.info("Competition USB: Bluetooth switched off" if not enabled
                        else "Bluetooth allowed again")
        self.blocked, self.blocked_by = not enabled, mount_path
        if self.blocked:
            self.scan_until = 0.0
            self.rfkill(True)

    # ------------------------------------------------------------- views
    def status(self) -> dict:
        with self.lock:
            devices, adapter, props = dict(self.devices), self.adapter, dict(self.adapter_props)
            jobs = {address: dict(job) for address, job in self.jobs.items() if time.time() - job["time"] < 300}
        listed = []
        for address, device in devices.items():
            paired = bool(device.get("Paired"))
            if not paired and device.get("RSSI") is None and address not in jobs:
                continue  # Not seen in this search
            name = device.get("Alias") or device.get("Name") or address
            if not paired and name.replace("-", ":").upper() == address and address not in jobs:
                continue  # Nameless devices are almost never controllers
            listed.append({
                "address": address, "name": name, "paired": paired,
                "trusted": bool(device.get("Trusted")), "connected": bool(device.get("Connected")),
                "gamepad": is_gamepad_like(device), "rssi": device.get("RSSI"),
                "job": jobs.get(address),
            })
        listed.sort(key=lambda d: (not d["paired"], not d["gamepad"], -(d["rssi"] or -999), d["name"].lower()))
        return {
            "bluetooth": {
                "present": adapter is not None, "powered": bool(props.get("Powered")),
                "searching": bool(props.get("Discovering")) and self.scan_until > 0,
                "address": props.get("Address"),
            },
            "competition_blocked": self.blocked,
            "devices": listed,
            "gamepads": self.input()["gamepads"],
        }

    def input(self) -> dict:
        snapshots = []
        for index, snapshot in enumerate(self.gamepads.snapshots()):
            if snapshot.connected:
                snapshots.append({
                    "player": index + 1, "name": snapshot.name, "address": snapshot.address.upper(),
                    "left_x": round(snapshot.left_x, 3), "left_y": round(snapshot.left_y, 3),
                    "right_x": round(snapshot.right_x, 3), "right_y": round(snapshot.right_y, 3),
                    "left_trigger": round(snapshot.left_trigger, 3),
                    "right_trigger": round(snapshot.right_trigger, 3),
                    "pressed": [name for name, down in snapshot.buttons.items() if down],
                })
        return {"gamepads": snapshots}

    # ------------------------------------------------------------- main loop
    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        while True:
            try:
                self.bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
                break
            except Exception as error:
                LOGGER.warning(f"Waiting for the system D-Bus: {error}")
                await asyncio.sleep(2)
        self.bus.export(AGENT_PATH, Agent(self))
        await self.register_agent()
        registered_for = None
        while True:
            try:
                await self.refresh()
                if self.adapter is not None and self.adapter != registered_for:
                    await self.register_agent()  # bluetoothd may have restarted
                    registered_for = self.adapter
                elif self.adapter is None:
                    registered_for = None
                await self.keep_bluetooth_working()
            except (DBusError, asyncio.TimeoutError) as error:
                LOGGER.info(f"BlueZ isn't ready: {short(error)}")
                with self.lock:
                    self.adapter, self.adapter_props = None, {}
            except Exception:
                LOGGER.exception("Unexpected error")
            await asyncio.sleep(REFRESH_SECONDS)

    def submit(self, coroutine, wait: bool = True):
        assert self.loop is not None
        future = asyncio.run_coroutine_threadsafe(coroutine, self.loop)
        return future.result(timeout=15) if wait else None


def short(error: Exception) -> str:
    if isinstance(error, DBusError):
        return str(error.type).rsplit(".", 1)[-1]
    return str(error)


def run_command(*command: str) -> None:
    try:
        subprocess.run(command, check=False, timeout=60,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired) as error:
        LOGGER.warning(f"{' '.join(command)} failed: {error}")


def watch_disks(service: GamepadService) -> None:
    """Follow Astoria's disk list over MQTT, to spot a competition USB."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        LOGGER.warning("paho-mqtt isn't installed; competition USBs can't switch Bluetooth off")
        return

    def on_connect(client, userdata, flags, reason_code, properties=None):
        if not reason_code.is_failure:
            client.subscribe(ASTDISKD_TOPIC)

    def on_message(client, userdata, message):
        try:
            disks = json.loads(message.payload).get("disks") or {}
            mounts = [str(path) for path in disks.values()]
        except (ValueError, AttributeError):
            return
        if service.loop is not None:
            service.loop.call_soon_threadsafe(service.disks_changed, mounts)

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="psc-gamepadd")
    client.on_connect, client.on_message = on_connect, on_message
    client.reconnect_delay_set(min_delay=1, max_delay=5)
    client.connect_async("localhost", 1883)
    client.loop_start()


def make_handler(service: GamepadService):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):  # noqa: A002
            pass

        def reply(self, status: int, data: dict) -> None:
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/gamepad-api/status"):
                self.reply(200, service.status())
            elif self.path.startswith("/gamepad-api/input"):
                self.reply(200, service.input())
            else:
                self.reply(404, {"error": "Not found"})

        def do_POST(self) -> None:  # noqa: N802
            try:
                length = int(self.headers.get("Content-Length") or 0)
                data = json.loads(self.rfile.read(length) or b"{}") if length else {}
            except ValueError:
                return self.reply(400, {"error": "Invalid JSON"})
            address = str(data.get("address", "")).upper()
            if service.blocked and self.path != "/gamepad-api/forget":
                return self.reply(409, {"error": "Bluetooth is switched off by the competition USB."})
            if self.path == "/gamepad-api/scan":
                if service.adapter is None:
                    return self.reply(503, {"error": "Bluetooth isn't ready yet. Wait a few seconds."})
                service.submit(service.set_scan(bool(data.get("on", True))))
                return self.reply(200, {"ok": True})
            if not re.fullmatch(r"([0-9A-F]{2}:){5}[0-9A-F]{2}", address):
                return self.reply(400, {"error": "A Bluetooth address is needed"})
            if self.path == "/gamepad-api/pair":
                job = service.jobs.get(address)
                if job and job["state"] in ("pairing", "connecting"):
                    return self.reply(200, {"ok": True})
                service.set_job(address, "pairing", "Pairing...")
                service.submit(service.pair(address), wait=False)
                return self.reply(202, {"ok": True})
            if self.path == "/gamepad-api/disconnect":
                service.submit(service.disconnect(address))
                return self.reply(200, {"ok": True})
            if self.path == "/gamepad-api/forget":
                service.submit(service.forget(address))
                return self.reply(200, {"ok": True})
            return self.reply(404, {"error": "Not found"})

    return Handler


class QuietRejections(logging.Filter):
    """dbus_next sends a D-Bus error reply for a DBusError, then logs it as unexpected."""

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        return not ("unexpected error processing a message" in message
                    and "dbus_next.errors.DBusError" in message)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for handler in logging.getLogger().handlers:
        handler.addFilter(QuietRejections())
    service = GamepadService(load_gamepad_module())
    server = ThreadingHTTPServer(LISTEN, make_handler(service))
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, name="http", daemon=True).start()
    watch_disks(service)
    LOGGER.info(f"Listening on {LISTEN[0]}:{LISTEN[1]}")
    asyncio.run(service.run())


if __name__ == "__main__":
    main()
