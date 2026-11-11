"""An implementation of a camera board using the april_vision library."""
from __future__ import annotations

import json
import logging
import re
import sys
from collections import Counter
from pathlib import Path
from typing import (
    Any, Callable, Dict, Iterable, List, Optional, Sequence, Union,
)

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from april_vision import CalibratedCamera, Frame, FrameSource
from april_vision import Marker as AprilMarker
from april_vision import (
    Processor, USBCamera, __version__, calibrations,
    find_cameras, generate_marker_size_mapping,
)
from april_vision.helpers import Base64Sender
from numpy.typing import NDArray

from .marker import Marker
from .utils import (
    IN_SIMULATOR, Board, BoardIdentity, BoardInfo, get_simulator_boards,
)

PathLike = Union[Path, str]
LOGGER = logging.getLogger(__name__)

robot_calibrations = calibrations.copy()
# Include calibrations from the package after calibrations from the user's directory
robot_calibrations.insert(1, str(Path(__file__).parent.resolve() / 'calibrations'))


def _manifest_calibration_locations(root: Path) -> list[str]:
    """Read calibration directories listed by the code USB manifest."""
    for manifest_name in ('camera-calibrations.toml', 'camera-calibrations.json'):
        manifest_path = root / manifest_name
        if not manifest_path.is_file():
            continue

        try:
            if manifest_path.suffix == '.json':
                manifest = json.loads(manifest_path.read_text())
            else:
                manifest = tomllib.loads(manifest_path.read_text())
            if not isinstance(manifest, dict):
                raise ValueError("manifest root must be an object")
            entries = manifest.get('calibrations', [])
            valid_entries = isinstance(entries, list) and all(
                isinstance(entry, str) for entry in entries
            )
            if not valid_entries:
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
            LOGGER.warning(
                "Ignoring invalid camera calibration manifest %s: %s",
                manifest_path,
                error,
            )
            return []
    return []


def _camera_calibration_locations() -> list[str]:
    locations = list(robot_calibrations)
    if not IN_SIMULATOR:
        locations.extend(_manifest_calibration_locations(Path.cwd()))
    return list(dict.fromkeys(locations))


def discover_camera_candidates() -> list[CalibratedCamera]:
    """Return working USB cameras, including cameras without calibration data.

    This is intended for setup tools and web interfaces. Robot startup continues
    to use only calibrated cameras through :meth:`AprilCamera._discover`.
    """
    if IN_SIMULATOR:
        return []

    return find_cameras(_camera_calibration_locations(), include_uncalibrated=True)


# Serials shared by many generic webcams, so they can't tell units apart.
_PLACEHOLDER_SERIALS = {'200901010001', '0123456789', '123456789', '12345678', 'SN0001'}
_UNSAFE_ID_CHARS = re.compile(r'[^A-Za-z0-9_.-]')
_USB_PORT = re.compile(r'\d+-\d+(\.\d+)*')


def _usable_serial(serial: Optional[str]) -> Optional[str]:
    """Return the camera's USB serial if it looks unique to that unit."""
    if serial is None:
        return None
    serial = serial.strip()
    if (
        len(serial) < 6
        or len(set(serial.lower())) < 4
        or serial.upper() in _PLACEHOLDER_SERIALS
        or _UNSAFE_ID_CHARS.search(serial)
    ):
        return None
    return serial


def _usb_port(index: int) -> Optional[str]:
    """Return the USB port path (e.g. '1-1.2') a V4L2 camera is plugged into."""
    try:
        device = Path(f'/sys/class/video4linux/video{index}/device').resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    # The device links to a USB interface such as '1-1.2:1.0'; its parent is the port.
    for node in (device, *device.parents):
        if _USB_PORT.fullmatch(node.name):
            return node.name
    return None


def assign_camera_ids(cameras: Sequence[CalibratedCamera]) -> List[str]:
    """
    Give each connected camera a name that stays the same between boots.

    A camera's USB serial number is used when it is present and unique among
    the connected cameras. Otherwise the USB port it is plugged into is used,
    as ``usb-<port>``, so it keeps its name while it stays in that port.
    Cameras with neither fall back to ``<name> - <index>``.

    :param cameras: Every connected camera, calibrated or not, so that names
        don't change when a calibration is added.
    :return: The camera IDs, in the same order as ``cameras``.
    """
    serials = [_usable_serial(getattr(camera, 'serial_num', None)) for camera in cameras]
    serial_counts = Counter(serial for serial in serials if serial)

    ids = []
    for camera, serial in zip(cameras, serials):
        if serial and serial_counts[serial] == 1:
            ids.append(serial)
        elif (port := _usb_port(camera.index)) is not None:
            ids.append(f'usb-{port}')
        else:
            ids.append(f'{camera.name} - {camera.index}')

    id_counts = Counter(ids)
    return [
        camera_id if id_counts[camera_id] == 1 else f'{camera_id} - {camera.index}'
        for camera_id, camera in zip(ids, cameras)
    ]


class AprilCamera(Board):
    """
    Virtual Camera Board for detecting fiducial markers.

    Additionally, it will do pose estimation, along with some calibration
    in order to determine the spatial positon and orientation of the markers
    that it has detected.

    :param camera_source: The source of the camera frames.
    :param calibration: The intrinsic calibration of the camera.
    :param serial_num: The serial number of the camera.
    :param name: The name of the camera.
    :param vidpid: The VID:PID of the camera.
    """
    __slots__ = ('_serial_num', '_cam')

    @staticmethod
    def get_board_type() -> str:
        """
        Return the type of this board.

        :return: The literal string 'Camera'.
        """
        return 'Camera'

    @classmethod
    def _discover(cls) -> Dict[str, 'AprilCamera']:
        """
        Discover the connected cameras that have calibration data available.

        The calibration data from april_vision, this package, the code USB
        manifest, and `OPENCV_CALIBRATIONS` is included when searching.

        :return: A dict of cameras, keyed by the IDs from :func:`assign_camera_ids`.
        """
        if IN_SIMULATOR:
            return {
                camera_info.serial_number: cls.from_webots_camera(camera_info)
                for camera_info in get_simulator_boards('CameraBoard')
            }

        candidates = discover_camera_candidates()
        return {
            camera_id:
            cls.from_id(camera_data.index, camera_data=camera_data, serial_num=camera_id)
            for camera_id, camera_data in zip(assign_camera_ids(candidates), candidates)
            if camera_data.calibration is not None
        }

    def __init__(
        self, camera_source: FrameSource,
        calibration: tuple[float, float, float, float] | None,
        serial_num: str,
        name: str,
        vidpid: str = "",
    ) -> None:
        # The processor handles the detection and pose estimation
        self._cam = Processor(
            camera_source,
            calibration=calibration,
            name=name,
            vidpid=vidpid,
            mask_unknown_size_tags=True,
        )
        self._serial_num = serial_num

    @classmethod
    def from_webots_camera(cls, camera_info: BoardInfo) -> 'AprilCamera':
        """
        Create a camera from a webots camera.

        :param camera_info: The information about the virtual camera,
                            including the url to connect to.
        :return: The camera object.
        """
        from .simulator.camera import WebotsRemoteCameraSource

        camera_source = WebotsRemoteCameraSource(camera_info)
        return cls(
            camera_source,
            calibration=camera_source.calibration,
            serial_num=camera_info.serial_number,
            name=camera_info.serial_number,
        )

    @classmethod
    def from_id(
        cls,
        camera_id: int,
        camera_data: CalibratedCamera,
        serial_num: str,
    ) -> 'AprilCamera':
        """
        Create a camera from an ID.

        :param camera_id: The ID of the camera to create.
        :param camera_data: The calibration data for the camera.
        :param serial_num: The serial number of the camera.
        :return: The camera object.
        """
        # The camera source handles the connection between the camera and the processor
        camera_source = USBCamera.from_calibration_file(
            camera_id,
            calibration_file=camera_data.calibration,
            vidpid=camera_data.vidpid,
        )
        return cls(
            camera_source,
            calibration=camera_source.calibration,
            serial_num=serial_num,
            name=camera_data.name,
            vidpid=camera_data.vidpid,
        )

    def identify(self) -> BoardIdentity:
        """
        Get the identity of the camera.

        The asset tag of the board is the camera ID used in ``Robot.cameras``.
        The version is the version of the april_vision library.

        :return: The identity of the board.
        """
        return BoardIdentity(
            manufacturer='april_vision',
            board_type='camera',
            asset_tag=self._serial_num,
            sw_version=__version__,
        )

    def close(self) -> None:
        """
        Close the camera.

        The camera will no longer work after this method is called.
        """
        self._cam.close()

    def see(
        self,
        *,
        frame: Optional[NDArray] = None,
        save: Optional[PathLike] = None,
    ) -> List[Marker]:
        """
        Capture an image and identify fiducial markers.

        :param frame: An image to detect markers in, instead of capturing a new one,
        :param save: If given, save the annotated frame to the path.
                     This is given a JPEG extension if none is provided.
        :returns: list of markers that the camera could see.
        """
        if frame is None:
            frame = self._cam.capture()

        markers = self._cam.see(frame=frame)

        if save:
            self._cam.save(save, frame=frame, detections=markers)
        return [Marker.from_april_vision_marker(marker) for marker in markers]

    def capture(self, *, save: Optional[PathLike] = None) -> NDArray:
        """
        Get the raw image data from the camera.

        :param save: If given, save the annotated frame to the path.
                     This is given a JPEG extension if none is provided.
        :returns: Camera pixel data
        """
        raw_frame = self._cam.capture()
        if save:
            self._cam.save(save, frame=raw_frame, annotated=False)
        return raw_frame

    def _set_marker_sizes(
        self,
        tag_sizes: Union[float, Dict[int, float]],
    ) -> None:
        """
        Set the size of tags that are used for pose estimation.

        If a dict is given for tag_sizes, only marker IDs that are keys of the
        dict will be detected.

        :param tag_sizes: The size of the tags to use for pose estimation given in meters.
        """
        self._cam.set_marker_sizes(tag_sizes)

    def _set_detection_hook(
        self,
        callback: Callable[[Frame, List[AprilMarker]], None],
    ) -> None:
        """
        Setup a callback to be run after each detection.

        The callback will be passed the frame and the list of markers that were detected.

        :param callback: The function to run after each detection.
        """
        self._cam.detection_hook = callback

    def __repr__(self) -> str:
        return f"<{self.__class__.__qualname__}: {self._serial_num}>"


def _setup_cameras(
    tag_sizes: Dict[Iterable[int], int],
    publish_func: Optional[Callable[..., None]] = None,
) -> Dict[str, AprilCamera]:
    """
    Find all connected cameras with calibration and configure tag sizes.

    Optionally set a callback to send a base64 encode JPEG bytestream of each
    image detection is run on. The callback is also passed
    ``extra={'camera': <camera ID>}`` so that frames from different cameras
    can be told apart.

    :param tag_sizes: The size of the tags to use for pose estimation given in millimeters
    :param publish_func: Optionally, a function to call with the base64 encoded JPEG bytestream
    :return: A dict of cameras, keyed by their camera ID.
    """
    # Unroll the tag ID iterables and convert the sizes to meters
    expanded_tag_sizes = generate_marker_size_mapping(tag_sizes)

    cameras = AprilCamera._discover()

    for camera_id, camera in cameras.items():
        # Set the tag sizes in the camera
        camera._set_marker_sizes(expanded_tag_sizes)
        if publish_func:
            frame_sender = Base64Sender(_labelled_publisher(publish_func, camera_id))
            camera._set_detection_hook(frame_sender.annotated_frame_hook)

    return cameras


def _labelled_publisher(
    publish_func: Callable[..., None],
    camera_id: str,
) -> Callable[[str, bytes], None]:
    """Wrap a publish function so each frame carries the ID of its camera."""
    extra: Dict[str, Any] = {'camera': camera_id}

    def publish(topic: str, payload: bytes) -> None:
        publish_func(topic, payload, extra=extra)

    return publish
