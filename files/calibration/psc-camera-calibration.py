#!/usr/bin/env python3
"""Small local camera calibration service for the PSC Robotics web UI."""
from __future__ import annotations

import base64
import filecmp
import io
import json
import os
import re
import shutil
import threading
import time
import tomllib
import xml.etree.ElementTree as ET
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import cv2
import numpy as np
from PIL import Image
import april_vision
from april_vision import calibrations, find_cameras
from april_vision.detect_cameras import generate_calibration_file_map
from april_vision.cli.calibrate import CalBoard, parse_detections, write_cal_file
from april_vision.utils import Frame
from april_vision.vision import Processor

try:
    # Use the robot API's own naming so this page shows the same camera IDs.
    from sr.robot3.camera import assign_camera_ids
except Exception as error:  # e.g. RPi.GPIO refuses to load off a Raspberry Pi
    print(f"Camera IDs unavailable: {error}", flush=True)
    assign_camera_ids = None

CALIBRATION_DIR = Path(os.environ.get(
    "PSC_CALIBRATION_DIR", "/var/lib/psc-robotics/calibrations"
))
BOARD_FILE = Path(os.environ.get(
    "PSC_CALIBRATION_BOARD_FILE",
    "/var/lib/psc-robotics/calibration-boards/psc-apriltag-tag36h11-4x6-40mm-A4.pdf",
))
LOCK = threading.Lock()
BOARD_LOCK = threading.Lock()
Point3D = tuple[float, float, float]
Point2D = tuple[float, float]
CalibrationView = tuple[list[Point3D], list[Point2D], int, tuple[int, int], list[int]]
SESSIONS: dict[str, list[CalibrationView]] = {}
SAVED_CALIBRATIONS: dict[str, Path] = {}
CAMERA_CACHE: list[dict[str, object]] | None = None
ACTIVE_STREAMS: set[int] = set()
LAST_FRAMES: dict[int, np.ndarray] = {}
USB_MOUNT_ROOT = Path(os.environ.get("PSC_CAMERA_USB_ROOT", "/media/robot"))
BUILTIN_CALIBRATION_DIR = Path(april_vision.__file__).resolve().parent / "calibrations"
V4L_ROOT = Path(os.environ.get("PSC_V4L_ROOT", "/sys/class/video4linux"))
# Calibrations are also kept on code USB drives, in this folder, so they move
# with the team's code and are imported onto any robot the drive is used with.
USB_CALIBRATION_FOLDER = "camera-calibrations"
USB_MANIFEST = "camera-calibrations.toml"
USB_IMPORT_INTERVAL_S = 5
RECENT_IMPORTS: list[dict[str, object]] = []
# Set while robot code is running: live streams stop, so the code can use the cameras.
CODE_RUNNING = threading.Event()
CODE_RUNNING_MESSAGE = "Robot code is running and may be using the camera. Stop it to see the live view."


def manifest_calibration_locations(root: Path) -> list[str]:
    for name in ("camera-calibrations.toml", "camera-calibrations.json"):
        manifest_path = root / name
        if not manifest_path.is_file():
            continue
        try:
            if manifest_path.suffix == ".json":
                manifest = json.loads(manifest_path.read_text())
            else:
                manifest = tomllib.loads(manifest_path.read_text())
            if not isinstance(manifest, dict):
                raise ValueError("manifest root must be an object")
            entries = manifest.get("calibrations", [])
            if not isinstance(entries, list) or not all(isinstance(entry, str) for entry in entries):
                raise ValueError("'calibrations' must be a list of directory paths")
            root_path = root.resolve()
            locations = []
            for entry in entries:
                location = (root_path / entry).resolve()
                if root_path not in location.parents or not location.is_dir():
                    raise ValueError(f"Invalid calibration directory: {entry}")
                locations.append(str(location))
            return locations
        except (OSError, json.JSONDecodeError, tomllib.TOMLDecodeError, ValueError) as error:
            print(f"Ignoring invalid camera calibration manifest {manifest_path}: {error}", flush=True)
            return []
    return []


def calibration_locations() -> list[str]:
    configured_locations = os.environ.get("OPENCV_CALIBRATIONS", "").split(":")
    locations = [*calibrations, *(path for path in configured_locations if path)]
    if USB_MOUNT_ROOT.is_dir():
        mounts = [USB_MOUNT_ROOT, *USB_MOUNT_ROOT.iterdir()]
        for mount in mounts:
            if mount.is_dir():
                locations.extend(manifest_calibration_locations(mount))
    return list(dict.fromkeys(locations))


def is_read_only_calibration(camera: object) -> bool:
    calibration = getattr(camera, "calibration", None)
    if calibration is None:
        return False
    try:
        Path(calibration).resolve().relative_to(BUILTIN_CALIBRATION_DIR.resolve())
        return True
    except ValueError:
        return False


def is_read_only_camera_index(index: int) -> bool:
    return any(
        camera["index"] == index and camera["read_only"]
        for camera in cameras()
    )


def cameras() -> list[dict[str, object]]:
    global CAMERA_CACHE
    with LOCK:
        if CAMERA_CACHE is not None and ACTIVE_STREAMS:
            return [dict(camera) for camera in CAMERA_CACHE]
    found = find_cameras(calibration_locations(), include_uncalibrated=True)
    robot_ids = assign_camera_ids(found) if assign_camera_ids else [None] * len(found)
    detected = {camera.index: camera for camera in found}
    robot_id_by_index = {camera.index: robot_id for camera, robot_id in zip(found, robot_ids)}
    result = []
    for index in range(10):
        device = f"/dev/video{index}"
        if not Path(device).exists():
            continue
        capture = cv2.VideoCapture(index)
        if not capture.isOpened():
            capture.release()
            continue
        width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH)) or 1280
        height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT)) or 720
        capture.release()
        camera = detected.get(index)
        vidpid = camera.vidpid if camera else f"video:{index}"
        result.append({
            "index": index,
            "name": camera.name if camera else f"USB webcam {index}",
            "vidpid": vidpid,
            "serial": camera.serial_num if camera else None,
            "robot_id": robot_id_by_index.get(index),
            "calibrated": bool(camera and camera.calibration is not None),
            "read_only": bool(camera and is_read_only_calibration(camera)),
            "calibration_source": (
                "Built-in" if camera and is_read_only_calibration(camera)
                else "Custom" if camera and camera.calibration is not None
                else None
            ),
            "width": width,
            "height": height,
        })
    with LOCK:
        CAMERA_CACHE = result
    return [dict(camera) for camera in result]


def connected_cameras() -> list[SimpleNamespace]:
    """List connected USB cameras from sysfs, without opening them."""
    found = []
    for node in V4L_ROOT.glob("video*"):
        try:
            index = int(node.name[5:])
            # UVC webcams also expose a metadata node (index 1) that isn't a camera.
            if (node / "index").read_text().strip() != "0":
                continue
            name = (node / "name").read_text().strip()
            product = re.search(
                r"PRODUCT=([0-9a-f]{1,4})/([0-9a-f]{1,4})", (node / "device/uevent").read_text()
            )
        except (OSError, ValueError):
            continue
        if product is None:
            continue
        serial_file = node / "device/../serial"
        found.append(SimpleNamespace(
            index=index,
            name=name,
            vidpid=f"{int(product[1], 16):04x}:{int(product[2], 16):04x}",
            serial_num=serial_file.read_text().strip() if serial_file.exists() else None,
        ))
    found.sort(key=lambda camera: camera.index)
    return found


def camera_summary() -> list[dict[str, object]]:
    """
    List connected cameras and whether they are calibrated, without opening them.

    This reads sysfs only, so the web UI can poll it while robot code is using
    a camera.
    """
    found = connected_cameras()
    calibration_map = generate_calibration_file_map(calibration_locations())
    robot_ids = assign_camera_ids(found) if assign_camera_ids else [None] * len(found)
    summary = []
    for camera, robot_id in zip(found, robot_ids):
        camera.calibration = calibration_map.get(camera.vidpid)
        summary.append({
            "index": camera.index,
            "name": camera.name,
            "vidpid": camera.vidpid,
            "robot_id": robot_id,
            "calibrated": camera.calibration is not None,
            "read_only": is_read_only_calibration(camera),
        })
    return summary


def usb_drives() -> list[Path]:
    """Mounted USB drives (robot code drives, competition drives and others)."""
    if not USB_MOUNT_ROOT.is_dir():
        return []
    drives = []
    for mount in sorted(USB_MOUNT_ROOT.iterdir()):
        try:
            if mount.is_dir() and os.path.ismount(mount):
                drives.append(mount)
        except OSError:
            continue
    return drives


def is_code_drive(drive: Path) -> bool:
    return (drive / "robot.py").is_file() or (drive / "robot-settings.toml").is_file()


def save_calibration_to_usb(source: Path) -> list[str]:
    """
    Copy a saved calibration onto every writable code USB drive.

    Also adds a camera-calibrations.toml manifest if the drive has none, so
    robot code can load the calibration straight from the drive.

    :return: The names of the drives it was saved to.
    """
    saved = []
    for drive in usb_drives():
        if not is_code_drive(drive) or not os.access(drive, os.W_OK):
            continue
        try:
            folder = drive / USB_CALIBRATION_FOLDER
            folder.mkdir(exist_ok=True)
            temporary = folder / (source.name + ".tmp")
            shutil.copyfile(source, temporary)
            os.replace(temporary, folder / source.name)
            if not (drive / USB_MANIFEST).exists() and not (drive / "camera-calibrations.json").exists():
                (drive / USB_MANIFEST).write_text(f'calibrations = ["{USB_CALIBRATION_FOLDER}"]\n')
            saved.append(drive.name)
        except OSError as error:
            print(f"Couldn't save calibration to USB drive {drive}: {error}", flush=True)
    if saved:
        os.sync()  # teams often unplug the drive straight after saving
    return saved


def usb_calibration_map() -> dict[str, tuple[Path, str]]:
    """Map USB ID to (calibration file, drive name) for calibrations on USB drives."""
    found: dict[str, tuple[Path, str]] = {}
    for drive in usb_drives():
        folders = [str(drive / USB_CALIBRATION_FOLDER), *manifest_calibration_locations(drive)]
        folders = [folder for folder in dict.fromkeys(folders) if Path(folder).is_dir()]
        for vidpid, path in generate_calibration_file_map(folders).items():
            found.setdefault(vidpid, (Path(path), drive.name))
    return found


def import_usb_calibrations() -> list[dict[str, object]]:
    """
    Import calibrations from USB drives for the cameras that are connected.

    A camera's calibration is copied onto the robot when a USB drive has one
    and the robot has none or a different one. A replaced calibration is kept
    in the "replaced" folder.
    """
    usb_map = usb_calibration_map()
    if not usb_map:
        return []
    imported = []
    for camera in connected_cameras():
        if camera.vidpid not in usb_map:
            continue
        source, drive = usb_map[camera.vidpid]
        target = CALIBRATION_DIR / (camera.vidpid.replace(":", "-") + ".xml")
        try:
            if target.exists():
                if filecmp.cmp(source, target, shallow=False):
                    continue
                replaced = CALIBRATION_DIR / "replaced"
                replaced.mkdir(exist_ok=True)
                os.replace(target, replaced / f"{target.stem}-{time.strftime('%Y%m%d-%H%M%S')}.xml")
            temporary = target.with_name(target.name + ".tmp")
            shutil.copyfile(source, temporary)
            os.replace(temporary, target)
        except OSError as error:
            print(f"Couldn't import calibration {source}: {error}", flush=True)
            continue
        record = {"name": camera.name, "vidpid": camera.vidpid, "drive": drive, "time": time.time()}
        imported.append(record)
        print(f"Imported calibration for {camera.name} ({camera.vidpid}) from USB drive {drive}", flush=True)
    if imported:
        global CAMERA_CACHE
        with LOCK:
            RECENT_IMPORTS.extend(imported)
            del RECENT_IMPORTS[:-20]
            CAMERA_CACHE = None
    return imported


def watch_code_status() -> None:
    """Track whether robot code is running, from astprocd's status on MQTT."""
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        print("paho-mqtt unavailable; live streams won't stop for robot code", flush=True)
        return

    def on_connect(client, userdata, flags, reason_code, properties=None):
        client.subscribe("astoria/astprocd")

    def on_message(client, userdata, message):
        try:
            status = json.loads(message.payload).get("code_status")
        except (ValueError, AttributeError):
            return
        if status in ("code_starting", "code_running"):
            CODE_RUNNING.set()
        else:
            CODE_RUNNING.clear()

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="psc-camera-calibration")
    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=5)
    client.connect_async("localhost", 1883)
    client.loop_start()


def watch_usb_calibrations() -> None:
    """Keep importing calibrations from USB drives as cameras and drives come and go."""
    while True:
        try:
            import_usb_calibrations()
        except Exception as error:  # keep watching whatever goes wrong
            print(f"USB calibration import failed: {error}", flush=True)
        time.sleep(USB_IMPORT_INTERVAL_S)


def view_preview(image: np.ndarray, detections: list) -> tuple[str, list[float]]:
    """Return a small annotated JPEG of a captured view and the board's bounding box."""
    height, width = image.shape[:2]
    corners = np.array(
        [[corner.x, corner.y] for detection in detections for corner in detection.pixel_corners],
        dtype=np.float32,
    )
    preview = image.copy()
    for detection in detections:
        outline = np.array([[corner.x, corner.y] for corner in detection.pixel_corners], dtype=np.int32)
        cv2.polylines(preview, [outline], True, (80, 200, 80), max(2, width // 320))
    scale = 240 / width
    preview = cv2.resize(preview, (240, max(1, int(height * scale))), interpolation=cv2.INTER_AREA)
    ok, encoded = cv2.imencode(".jpg", preview, [cv2.IMWRITE_JPEG_QUALITY, 70])
    thumbnail = "data:image/jpeg;base64," + base64.b64encode(encoded.tobytes()).decode() if ok else ""
    x0, y0 = corners.min(axis=0)
    x1, y1 = corners.max(axis=0)
    return thumbnail, [float(x0 / width), float(y0 / height), float(x1 / width), float(y1 / height)]


def capture_frame(index: int) -> np.ndarray:
    with LOCK:
        if index in ACTIVE_STREAMS and index in LAST_FRAMES:
            return LAST_FRAMES[index].copy()
    capture = cv2.VideoCapture(index)
    try:
        if not capture.isOpened():
            raise RuntimeError("Unable to open camera")
        ok, frame = capture.read()
        if not ok:
            raise RuntimeError("Unable to capture camera frame")
        return frame
    finally:
        capture.release()


def calibration_history() -> list[dict[str, object]]:
    records = []
    if not CALIBRATION_DIR.is_dir():
        return records
    for path in sorted(CALIBRATION_DIR.glob("*.xml*"), key=lambda item: item.stat().st_mtime, reverse=True):
        try:
            root = ET.parse(path).getroot()
            error_node = root.find("avg_reprojection_error")
            frames_node = root.find("framesCount")
            resolution_node = root.find("cameraResolution")
            vidpid_node = root.find("vidpid")
            camera_resolution = " ".join((resolution_node.text or "").split()) if resolution_node is not None else ""
            vidpid = (vidpid_node.text or "").strip().strip('"') if vidpid_node is not None else ""
            reprojection_error = float(error_node.text) if error_node is not None and error_node.text else None
            rejected = ".rejected-" in path.name or reprojection_error is None or reprojection_error > 2.0
            records.append({
                "file": path.name,
                "camera": vidpid or path.name,
                "date": (root.findtext("calibrationDate") or "").strip().strip('"'),
                "frames": int(frames_node.text) if frames_node is not None and frames_node.text else None,
                "resolution": camera_resolution,
                "reprojection_error": reprojection_error,
                "status": "rejected" if rejected else "accepted",
            })
        except (ET.ParseError, OSError, ValueError):
            continue
    return records


def build_calibration_sheet(rows: int, columns: int, marker_size_mm: float, tag_family: str = "tag36h11") -> bytes:
    family_map = {
        "tag25h9": (cv2.aruco.DICT_APRILTAG_25H9, 7),
        "tag36h10": (cv2.aruco.DICT_APRILTAG_36H10, 8),
        "tag36h11": (cv2.aruco.DICT_APRILTAG_36H11, 8),
    }
    if tag_family not in family_map:
        raise ValueError(f"Unsupported tag family: {tag_family}")
    if rows < 1 or columns < 1:
        raise ValueError("Board dimensions must be at least 1x1")
    if marker_size_mm <= 0:
        raise ValueError("Marker size must be greater than zero")
    dictionary_id, border_width = family_map[tag_family]
    dictionary = cv2.aruco.getPredefinedDictionary(dictionary_id)
    if rows * columns > dictionary.bytesList.shape[0]:
        raise ValueError("The selected tag family does not contain enough markers for this board")

    pixels_per_mm = 10
    marker_px = max(20, int(round(marker_size_mm * pixels_per_mm)))
    gap_mm = marker_size_mm / border_width
    board_width_mm = columns * marker_size_mm + (columns - 1) * gap_mm
    board_height_mm = rows * marker_size_mm + (rows - 1) * gap_mm
    page_width_mm, page_height_mm, margin_mm = 297, 210, 10
    if board_width_mm > page_width_mm - 2 * margin_mm or board_height_mm > page_height_mm - 2 * margin_mm:
        raise ValueError("This board does not fit on one A4 landscape page at the selected marker size")

    sheet = Image.new("L", (page_width_mm * pixels_per_mm, page_height_mm * pixels_per_mm), 255)

    def draw_marker(marker_id: int, size_px: int) -> np.ndarray:
        marker = np.zeros((size_px, size_px), dtype=np.uint8)
        if hasattr(cv2.aruco, "drawMarker"):
            cv2.aruco.drawMarker(dictionary, marker_id, size_px, marker, 1)
        else:
            cv2.aruco.generateImageMarker(dictionary, marker_id, size_px, marker, 1)
        return marker

    margin_x_px = int(round((page_width_mm - board_width_mm) * pixels_per_mm / 2))
    margin_y_px = int(round((page_height_mm - board_height_mm) * pixels_per_mm / 2))
    pitch_px = int(round((marker_size_mm + gap_mm) * pixels_per_mm))
    for row in range(rows):
        for col in range(columns):
            marker_id = row * columns + col
            marker = Image.fromarray(draw_marker(marker_id, marker_px))
            x = margin_x_px + col * pitch_px
            y = margin_y_px + row * pitch_px
            sheet.paste(marker, (x, y))

    output = io.BytesIO()
    sheet.save(output, format="PDF", resolution=pixels_per_mm * 25.4)
    return output.getvalue()


def fixed_calibration_sheet() -> bytes:
    with BOARD_LOCK:
        if not BOARD_FILE.is_file():
            BOARD_FILE.parent.mkdir(parents=True, exist_ok=True)
            temporary_file = BOARD_FILE.with_suffix(".tmp")
            temporary_file.write_bytes(build_calibration_sheet(4, 6, 40, "tag36h11"))
            temporary_file.replace(BOARD_FILE)
        return BOARD_FILE.read_bytes()


class Handler(BaseHTTPRequestHandler):
    def send_json(self, status: int, value: object) -> None:
        payload = json.dumps(value).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/cameras":
            try:
                self.send_json(200, {"cameras": cameras()})
            except Exception as error:
                self.send_json(500, {"error": str(error)})
            return

        if parsed.path == "/api/cameras/summary":
            try:
                with LOCK:
                    imports = [item for item in RECENT_IMPORTS if time.time() - item["time"] < 120]
                self.send_json(200, {"cameras": camera_summary(), "imports": imports})
            except Exception as error:
                self.send_json(500, {"error": str(error)})
            return

        if parsed.path == "/api/calibration/history":
            self.send_json(200, {"calibrations": calibration_history()})
            return

        if parsed.path == "/api/calibration/history/download":
            filename = Path(parse_qs(parsed.query).get("file", [""])[0]).name
            calibration_path = CALIBRATION_DIR / filename
            if not filename.endswith(".xml") or not calibration_path.is_file():
                self.send_json(404, {"error": "Calibration file not found"})
                return
            payload = calibration_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/api/preview":
            query = parse_qs(parsed.query)
            index = int(query.get("index", [0])[0])
            frame = capture_frame(index)
            ok, encoded = cv2.imencode(".jpg", frame)
            if not ok:
                self.send_json(500, {"error": "Unable to encode camera frame"})
                return
            payload = encoded.tobytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/api/stream":
            query = parse_qs(parsed.query)
            index = int(query.get("index", [0])[0])
            if CODE_RUNNING.is_set():
                self.send_json(409, {"error": CODE_RUNNING_MESSAGE})
                return
            with LOCK:
                ACTIVE_STREAMS.add(index)
            capture = cv2.VideoCapture(index)
            if not capture.isOpened():
                capture.release()
                with LOCK:
                    ACTIVE_STREAMS.discard(index)
                self.send_json(503, {"error": "Unable to open camera"})
                return
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.send_header("Connection", "close")
            self.end_headers()
            try:
                while not CODE_RUNNING.is_set():
                    ok, frame = capture.read()
                    if not ok:
                        time.sleep(0.05)
                        continue
                    with LOCK:
                        LAST_FRAMES[index] = frame.copy()
                    ok, encoded = cv2.imencode(".jpg", frame)
                    if not ok:
                        continue
                    payload = encoded.tobytes()
                    self.wfile.write(
                        b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: "
                        + str(len(payload)).encode()
                        + b"\r\n\r\n"
                        + payload
                        + b"\r\n"
                    )
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            finally:
                capture.release()
                with LOCK:
                    ACTIVE_STREAMS.discard(index)
                    LAST_FRAMES.pop(index, None)
            return

        if parsed.path == "/api/calibration/download":
            query = parse_qs(parsed.query)
            session = query.get("session", [""])[0]
            with LOCK:
                calibration_path = SAVED_CALIBRATIONS.get(session)
            if calibration_path is None or not calibration_path.is_file():
                self.send_json(404, {"error": "No saved calibration for this session"})
                return
            payload = calibration_path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "application/xml")
            self.send_header(
                "Content-Disposition",
                f'attachment; filename="{calibration_path.name}"',
            )
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        if parsed.path == "/api/calibration/board":
            try:
                payload = fixed_calibration_sheet()
            except (OSError, ValueError) as error:
                self.send_json(400, {"error": str(error)})
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/pdf")
            self.send_header("Content-Disposition", f'attachment; filename="{BOARD_FILE.name}"')
            self.send_header("Cache-Control", "public, max-age=31536000, immutable")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return

        self.send_json(404, {"error": "Not found"})

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        size = int(self.headers.get("Content-Length", "0"))
        body = json.loads(self.rfile.read(size) or b"{}")

        try:
            if parsed.path == "/api/calibration/start":
                if is_read_only_camera_index(int(body.get("index", -1))):
                    self.send_json(403, {"error": "The built-in camera calibration is read-only."})
                    return
                session = str(body.get("session", "default"))
                with LOCK:
                    SESSIONS[session] = []
                self.send_json(200, {"session": session})
                return

            if parsed.path == "/api/calibration/capture":
                if CODE_RUNNING.is_set():
                    self.send_json(409, {"error": CODE_RUNNING_MESSAGE})
                    return
                if is_read_only_camera_index(int(body["index"])):
                    self.send_json(403, {"error": "The built-in camera calibration is read-only."})
                    return
                session = str(body.get("session", "default"))
                rows = int(body["rows"])
                columns = int(body["columns"])
                marker_size_mm = float(body["marker_size_mm"])
                minimum_markers = int(body.get("minimum_markers", 1))
                board = CalBoard(rows, columns, marker_size_mm / 1000, str(body.get("tag_family", "tag36h11")))
                image = capture_frame(int(body["index"]))
                frame = Frame.from_colour_frame(image)
                detections = Processor(aruco_orientation=False)._detect(frame)
                detected_markers = len(detections)
                if detected_markers < minimum_markers:
                    message = (
                        f"Calibration board was not detected clearly enough: only {detected_markers} markers "
                        f"were found, need at least {minimum_markers}."
                    )
                    print(
                        f"[calibration-debug] session={session} capture={len(SESSIONS.get(session, [])) + 1} "
                        f"detected_markers={detected_markers} required={minimum_markers} "
                        f"board={rows}x{columns} marker_size_mm={marker_size_mm} index={body['index']}",
                        flush=True,
                    )
                    self.send_json(
                        422,
                        {
                            "error": message,
                            "detected_markers": detected_markers,
                            "required_markers": minimum_markers,
                            "board": {"rows": rows, "columns": columns, "marker_size_mm": marker_size_mm},
                        },
                    )
                    return
                # AprilTag detector order differs from CalBoard's point order by a horizontal flip.
                detections = [
                    detection._replace(
                        pixel_corners=(
                            detection.pixel_corners[1],
                            detection.pixel_corners[0],
                            detection.pixel_corners[3],
                            detection.pixel_corners[2],
                        )
                    )
                    for detection in detections
                ]
                points = parse_detections(detections, board)
                marker_ids = sorted(int(detection.id) for detection in detections)
                thumbnail, board_box = view_preview(image, detections)
                resolution = (int(image.shape[1]), int(image.shape[0]))
                with LOCK:
                    SESSIONS.setdefault(session, []).append(
                        (points[0], points[1], detected_markers, resolution, marker_ids)
                    )
                    count = len(SESSIONS[session])
                print(
                    f"[calibration-debug] session={session} capture={count} detected_markers={detected_markers} "
                    f"board={rows}x{columns} marker_size_mm={marker_size_mm} index={body['index']}",
                    flush=True,
                )
                self.send_json(
                    200,
                    {
                        "captured": count,
                        "detected_markers": detected_markers,
                        "detected_marker_ids": marker_ids,
                        "detected_corners": len(points[1]),
                        "total_markers": rows * columns,
                        "frame_resolution": resolution,
                        "thumbnail": thumbnail,
                        "board_box": board_box,
                        "board": {"rows": rows, "columns": columns, "marker_size_mm": marker_size_mm},
                    },
                )
                return

            if parsed.path == "/api/calibration/remove":
                session = str(body.get("session", "default"))
                view = int(body["view"])
                with LOCK:
                    views = SESSIONS.get(session, [])
                    if not 1 <= view <= len(views):
                        raise ValueError("No such captured view")
                    del views[view - 1]
                    count = len(views)
                self.send_json(200, {"captured": count})
                return

            if parsed.path == "/api/calibration/finish":
                if is_read_only_camera_index(int(body.get("index", -1))):
                    self.send_json(403, {"error": "The built-in camera calibration is read-only."})
                    return
                session = str(body.get("session", "default"))
                with LOCK:
                    frames = SESSIONS.get(session, [])
                if len(frames) < int(body.get("minimum_frames", 10)):
                    self.send_json(422, {"error": "Capture more calibration views first", "captured_frames": len(frames)})
                    return
                object_points = [np.array(view[0], dtype=np.float32) for view in frames]
                image_points = [np.array(view[1], dtype=np.float32) for view in frames]
                frame_marker_counts = [view[2] for view in frames]
                frame_resolutions = [list(view[3]) for view in frames]
                requested_resolution = list(map(int, body["resolution"]))
                width, height = requested_resolution
                error, matrix, distortion, rvecs, tvecs = cv2.calibrateCamera(
                    object_points, image_points, (width, height), None, None
                )
                per_view_diagnostics = []
                for index, (object_view, image_view, rotation, translation, view) in enumerate(
                    zip(object_points, image_points, rvecs, tvecs, frames), start=1
                ):
                    projected, _ = cv2.projectPoints(
                        object_view, rotation, translation, matrix, distortion
                    )
                    residuals = np.linalg.norm(
                        projected.reshape(-1, 2) - image_view.reshape(-1, 2), axis=1
                    )
                    per_view_diagnostics.append({
                        "view": index,
                        "detected_tags": view[2],
                        "tag_ids": view[4],
                        "corner_points": len(image_view),
                        "frame_resolution": list(view[3]),
                        "reprojection_rms_px": float(np.sqrt(np.mean(residuals ** 2))),
                        "worst_corner_residual_px": float(np.max(residuals)),
                    })
                resolution_mismatches = [
                    {"view": index, "actual": resolution, "requested": requested_resolution}
                    for index, resolution in enumerate(frame_resolutions, start=1)
                    if resolution != requested_resolution
                ]
                if not np.isfinite(error) or error > 2.0:
                    message = (
                        f"Calibration error is {error:.2f} px (maximum 2.00 px). "
                        "Do not save this result. Reprint the board at 100% scale, "
                        "keep it flat and sharply focused, and capture varied views "
                        "with at least 6 clearly detected tags each."
                    )
                    print(
                        f"[calibration-debug] session={session} calibration_rejected error={error:.3f} px "
                        f"frames={len(frames)} detected_tags_per_frame={frame_marker_counts} "
                        f"corner_points_per_frame={[len(view[1]) for view in frames]} "
                        f"min_detected_markers={min(frame_marker_counts)} max_detected_markers={max(frame_marker_counts)} "
                        f"requested_resolution={requested_resolution} actual_resolutions={frame_resolutions} "
                        f"board={body.get('rows')}x{body.get('columns')} marker_size_mm={body.get('marker_size_mm', 'unknown')}",
                        flush=True,
                    )
                    self.send_json(
                        422,
                        {
                            "error": message,
                            "reprojection_error": float(error),
                            "maximum_reprojection_error": 2.0,
                            "captured_frames": len(frames),
                            "detected_tags_per_frame": frame_marker_counts,
                            "detected_corners_per_frame": [len(view[1]) for view in frames],
                            "frame_resolutions": frame_resolutions,
                            "requested_resolution": requested_resolution,
                            "resolution_mismatches": resolution_mismatches,
                            "per_view_diagnostics": per_view_diagnostics,
                            "camera_matrix": matrix.tolist(),
                            "distortion_coefficients": distortion.ravel().tolist(),
                            "mean_detected_tags": float(np.mean(frame_marker_counts)),
                            "min_detected_markers": int(min(frame_marker_counts)),
                            "max_detected_markers": int(max(frame_marker_counts)),
                            "board": {"rows": int(body.get("rows", 0)), "columns": int(body.get("columns", 0)), "marker_size_mm": float(body.get("marker_size_mm", 0.0))},
                        },
                    )
                    return
                if not re.fullmatch(r"[0-9a-f]{4}:[0-9a-f]{4}", str(body["vidpid"])):
                    raise ValueError(
                        "This camera does not report a USB ID, so robot code cannot match a "
                        "calibration to it."
                    )
                CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
                safe_vidpid = "".join(
                    char if char.isalnum() or char in "-_." else "-"
                    for char in str(body["vidpid"])
                )
                if not safe_vidpid or safe_vidpid in {".", ".."}:
                    raise ValueError("Invalid camera identifier")
                filename = CALIBRATION_DIR / (safe_vidpid.replace(":", "-") + ".xml")
                write_cal_file(str(filename), len(frames), width, height, matrix, distortion, error, str(body["vidpid"]))
                with LOCK:
                    SAVED_CALIBRATIONS[session] = filename
                saved_to_usb = save_calibration_to_usb(filename)
                print(
                    f"[calibration-debug] session={session} calibration_accepted error={error:.3f} px "
                    f"frames={len(frames)} mean_detected_tags={np.mean(frame_marker_counts):.2f}",
                    flush=True,
                )
                self.send_json(200, {
                    "file": str(filename), "reprojection_error": error,
                    "captured_frames": len(frames), "saved_to_usb": saved_to_usb,
                })
                return
        except (KeyError, ValueError, RuntimeError) as error:
            self.send_json(400, {"error": str(error)})
            return

        self.send_json(404, {"error": "Not found"})


if __name__ == "__main__":
    CALIBRATION_DIR.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=watch_usb_calibrations, name="usb-import", daemon=True).start()
    watch_code_status()
    ThreadingHTTPServer(("127.0.0.1", 8765), Handler).serve_forever()