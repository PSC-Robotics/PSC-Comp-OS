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
