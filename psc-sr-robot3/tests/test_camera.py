import sys
from types import SimpleNamespace

import pytest

from sr.robot3 import camera


def test_discover_calibrated_packaged_and_custom_cameras(monkeypatch) -> None:
    """Bundled and user calibration files both create normal robot cameras."""
    locations = ["/team/calibrations", "/package/april_vision/calibrations"]
    cameras = [
        SimpleNamespace(
            index=0,
            name="Logitech C920",
            calibration="/package/april_vision/calibrations/Logitech C920.xml",
            vidpid="046d:082d",
        ),
        SimpleNamespace(
            index=1,
            name="Custom webcam",
            calibration="/team/calibrations/custom.xml",
            vidpid="1234:5678",
        ),
        SimpleNamespace(
            index=2,
            name="Uncalibrated webcam",
            calibration=None,
            vidpid="abcd:0001",
        ),
    ]
    calls = []

    def find_cameras(calibration_locations, include_uncalibrated=False):
        calls.append((calibration_locations, include_uncalibrated))
        return cameras

    monkeypatch.setattr(camera, "robot_calibrations", locations)
    monkeypatch.setattr(camera, "find_cameras", find_cameras)
    monkeypatch.setattr(camera, "IN_SIMULATOR", False)
    monkeypatch.setattr(camera, "_usb_port", lambda index: None)
    monkeypatch.setattr(
        camera.AprilCamera,
        "from_id",
        classmethod(lambda cls, camera_id, camera_data, serial_num: camera_data),
    )

    candidates = camera.discover_camera_candidates()
    discovered = camera.AprilCamera._discover()

    assert candidates == cameras
    assert calls == [(locations, True), (locations, True)]
    assert set(discovered) == {"Logitech C920 - 0", "Custom webcam - 1"}
    assert discovered["Logitech C920 - 0"].calibration == cameras[0].calibration
    assert discovered["Custom webcam - 1"].calibration == cameras[1].calibration


@pytest.mark.parametrize(
    ("manifest_name", "manifest_contents"),
    [
        ("camera-calibrations.toml", 'calibrations = ["camera-calibrations"]\n'),
        ("camera-calibrations.json", '{"calibrations": ["camera-calibrations"]}'),
    ],
)
def test_usb_calibration_manifest(
    manifest_name, manifest_contents, tmp_path, monkeypatch,
) -> None:
    calibration_dir = tmp_path / "camera-calibrations"
    calibration_dir.mkdir()
    (tmp_path / manifest_name).write_text(manifest_contents)
    monkeypatch.setattr(camera, "robot_calibrations", ["/built-in", "/team"])
    monkeypatch.setattr(camera, "IN_SIMULATOR", False)
    monkeypatch.chdir(tmp_path)

    assert camera._camera_calibration_locations() == [
        "/built-in",
        "/team",
        str(calibration_dir),
    ]


def _cam(index, serial=None, name="Webcam", calibration="/cal.xml"):
    return SimpleNamespace(
        index=index, name=name, serial_num=serial, calibration=calibration, vidpid="1234:5678",
    )


def test_camera_ids_prefer_unique_serials(monkeypatch) -> None:
    ports = {0: "1-1.1", 1: "1-1.2", 2: "1-1.3", 3: "1-1.4"}
    monkeypatch.setattr(camera, "_usb_port", ports.get)

    ids = camera.assign_camera_ids([
        _cam(0, "A1B2C3D4"),         # unique serial
        _cam(1, "0001"),             # placeholder-looking serial
        _cam(2, "SAME12345"),        # duplicated serial
        _cam(3, "SAME12345"),
    ])

    assert ids == ["A1B2C3D4", "usb-1-1.2", "usb-1-1.3", "usb-1-1.4"]


@pytest.mark.parametrize(
    "serial", [None, "", "000000", "200901010001", "SN0001", "has space1"],
)
def test_unusable_serials_fall_back_to_port(serial, monkeypatch) -> None:
    monkeypatch.setattr(camera, "_usb_port", lambda index: "3-1")
    assert camera.assign_camera_ids([_cam(0, serial)]) == ["usb-3-1"]


def test_camera_ids_without_serial_or_port_stay_unique(monkeypatch) -> None:
    monkeypatch.setattr(camera, "_usb_port", lambda index: None)
    assert camera.assign_camera_ids([_cam(0), _cam(1)]) == ["Webcam - 0", "Webcam - 1"]


@pytest.mark.skipif(sys.platform == "win32", reason="fakes Linux's /sys with POSIX paths")
def test_usb_port_from_sysfs(tmp_path, monkeypatch) -> None:
    interface = tmp_path / "devices" / "usb1" / "1-1" / "1-1.3" / "1-1.3:1.0"
    interface.mkdir(parents=True)
    video = tmp_path / "class" / "video4linux" / "video2"
    video.mkdir(parents=True)
    (video / "device").symlink_to(interface)

    real_path = camera.Path
    monkeypatch.setattr(
        camera, "Path",
        lambda value: real_path(str(value).replace("/sys", str(tmp_path), 1)),
    )

    assert camera._usb_port(2) == "1-1.3"
    assert camera._usb_port(5) is None


def test_discover_keys_cameras_by_id_and_skips_uncalibrated(monkeypatch) -> None:
    candidates = [
        _cam(0, "A1B2C3D4"),
        _cam(1, None),
        _cam(2, "C3D4E5F6", calibration=None),
    ]
    monkeypatch.setattr(camera, "discover_camera_candidates", lambda: candidates)
    monkeypatch.setattr(camera, "IN_SIMULATOR", False)
    monkeypatch.setattr(camera, "_usb_port", lambda index: f"1-1.{index}")
    monkeypatch.setattr(
        camera.AprilCamera,
        "from_id",
        classmethod(lambda cls, camera_id, camera_data, serial_num: (serial_num, camera_data)),
    )

    discovered = camera.AprilCamera._discover()

    assert discovered == {
        "A1B2C3D4": ("A1B2C3D4", candidates[0]),
        "usb-1-1.1": ("usb-1-1.1", candidates[1]),
    }


def test_frames_are_labelled_with_their_camera(monkeypatch) -> None:
    class FakeCamera:
        def _set_marker_sizes(self, sizes):
            pass

        def _set_detection_hook(self, hook):
            self.hook = hook

    cams = {"front": FakeCamera(), "back": FakeCamera()}
    monkeypatch.setattr(camera.AprilCamera, "_discover", classmethod(lambda cls: cams))
    published = []

    class FakeSender:
        def __init__(self, publish):
            self.annotated_frame_hook = lambda: publish("camera/annotated", b"jpeg")

    monkeypatch.setattr(camera, "Base64Sender", FakeSender)
    camera._setup_cameras({}, lambda topic, payload, **kw: published.append((topic, kw)))
    cams["back"].hook()
    cams["front"].hook()

    assert published == [
        ("camera/annotated", {"extra": {"camera": "back"}}),
        ("camera/annotated", {"extra": {"camera": "front"}}),
    ]
