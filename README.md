# PSC Robotics OS

PSC Robotics OS is a Raspberry Pi image for PSC Robotics. It is based on the
Student Robotics image build system, with PSC branding, Astoria-compatible
services, Wi-Fi networking, a web UI, and a local USB-camera calibration tool.

The image boots and provides its web and calibration services without Student
Robotics kit boards. SR-specific USB rules and the optional `SR_COMP_USB`
read-only mount are compatibility features, not boot requirements. The robot
user is also configured for generic USB serial, video, and GPIO access. PSC
competition hardware can use the GPIO assignments in `files/astoria/astoria.toml`.
The same arm64 image supports Raspberry Pi 3 Model B and Pi 4. On Pi 3 Model B,
a boot-time fallback retries the onboard BCM43430 Wi-Fi driver with the
alternate firmware shipped in Raspberry Pi OS if the normal driver fails to
create `wlan0`. This is a recovery attempt; the AP still requires valid USB
Wi-Fi metadata and a working radio. Pi 3 B+ and Pi 4 retain their own normal
firmware paths.

## Web Interface

The kit UI is served by nginx on port 80. On a device connected to the robot's
network, open `http://robot.lan/`; the camera setup page is at
`http://robot.lan/calibrate`. If local DNS is unavailable, use the Raspberry
Pi's IP address with the same paths.

For camera calibration:

1. Connect a standard USB UVC webcam.
2. Open `/calibrate` and download the A4 landscape AprilTag PDF. The page shows
	a continuous live webcam preview to help position the board. Print the PDF at
	actual size (100% scale), with “fit to page” disabled, then verify the printed
	tag size matches the value shown on the page. The default 6-by-4 board fits on
	one landscape A4 sheet while retaining the correct calibration spacing.
3. Select the webcam, capture at least 10 clear board views from varied angles
	and distances, and save the calibration. Keep the printed board rigid and
	flat; do not bend or curl it. Each accepted frame must show at least 6 clearly
	detected tags, but the whole sheet need not fit in the frame.
4. The OpenCV XML calibration is saved in
	`/var/lib/psc-robotics/calibrations`. The calibration directory is supplied
	to the camera service and Astoria-launched robot programs through
	`OPENCV_CALIBRATIONS`.
	Results above 2 pixels reprojection error are rejected and are not saved;
	reposition or reprint the board and capture clearer, varied views.
	The calibration page lists previous results, including rejected calibrations,
	with their date, frame count, resolution, and reprojection error.

The Logitech calibrations bundled with `april_vision` appear as built-in,
read-only cameras in the setup page. To provide team calibrations on a code USB,
place XML files in a directory such as `camera-calibrations/` and add a root
manifest named `camera-calibrations.toml` containing
`calibrations = ["camera-calibrations"]`, or JSON containing
`{"calibrations": ["camera-calibrations"]}`. Those cameras become available
only when a matching calibration is loaded.

Useful service diagnostics:

```sh
systemctl status nginx psc-camera-calibration
journalctl -u nginx -u psc-camera-calibration
```

## Networking And USB

The image retains the configured Wi-Fi bridge/hotspot and Astoria Wi-Fi service;
these are independent of SR board hardware. USB storage is optional. Devices
labelled `SR_COMP_USB` are mounted read-only for compatibility, while ordinary
USB webcams and serial devices remain usable without that drive.

## Build

Requirements: Docker Desktop or Docker Engine, Git, and this repository cloned.
The build runs Packer from `mkaczanowski/packer-builder-arm` inside Docker and
downloads the Raspberry Pi base image and current kit-ui release.

```sh
./build-image.sh nocompress
```

Omit `nocompress` to produce an `.img.xz` distribution image. The output name
is `PSC Robotics OS-image-<git-description>.img[.xz]`; it includes the nearest
Git tag (or commit hash). Working-tree changes add `-dirty` to the description.
Existing image files are not removed by the build script. Keep an eye on free
disk space because the uncompressed image is several gigabytes.

The Packer provisioners are ordered in `pi.json`; OS customization is in
`scripts/` and `files/`. Camera service setup is installed by
`scripts/060-astoria.sh`, and nginx/web UI setup is in `scripts/090-webui.sh`.
