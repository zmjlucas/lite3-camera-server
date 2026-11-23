# Lite3 Camera Server

[![CI](https://github.com/zmjlucas/lite3-camera-server/actions/workflows/ci.yml/badge.svg)](https://github.com/zmjlucas/lite3-camera-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

View the camera on a Lite3 robot in a Mac browser through SSH. Use an existing ROS image topic, RealSense, or V4L2 RGB. A local demo works without a robot.

This tool shows camera images. It does not send motion commands. The client does not need ROS. The local demo and unit tests use only the Python standard library.

## Quick start

The launcher requires Python 3.8 or later. A robot connection also requires OpenSSH (`ssh` and `scp`) and network access to the perception host. Use a maintained Python version for development. Python 3.8 support is for older devices.

```bash
git clone https://github.com/zmjlucas/lite3-camera-server.git
cd lite3-camera-server
python3 scripts/lite3_camera.py demo
```

The browser opens `http://127.0.0.1:18080/` and shows synthetic frames at 160 x 120 pixels. To stop the demo, press Ctrl+C in the terminal. If no desktop is available, add `--no-open`.

Run the scripts directly. You do not need `pip install`. See the [camera guide](docs/CAMERA_STREAM.md) for all procedures.

## Connect to a robot

Replace the example address with your perception host. Before the first connection, check the SSH host key.

```bash
# Check the host, dependencies, camera services, and ROS environment.
python3 scripts/lite3_camera.py doctor ysc@192.168.1.103

# Use an existing ROS image topic and a compatible web_video_server.
python3 scripts/lite3_camera.py view ysc@192.168.1.103
```

| Mode | Requirements on the perception host | Command |
| --- | --- | --- |
| ROS (default) | An active image topic, the matching ROS environment, and `web_video_server` | `view HOST` |
| RealSense | A camera that is not in use, compatible `pyrealsense2`, `numpy`, and `cv2` | Run `deploy HOST`. Then run `view HOST --backend realsense`. |
| V4L2 RGB | Linux, OpenCV with V4L2 support, and a confirmed RGB device node that is not in use | Run `deploy HOST`. Then run `view HOST --backend v4l2 --device /dev/videoN`. |
| Demo | The local Python standard library | `demo` |

Camera dependencies must match the device architecture, Python version, and vendor SDK. The repository does not install these dependencies from a shared requirements file. The server uses Python 3.6-compatible syntax. The full test suite requires Python 3.8 or later. Python 3.6 runtime operation remains unverified.

On macOS, `doctor`, `deploy`, and `view` read local routes and Wi-Fi interfaces to select a path to `192.168.1.*` targets. If the target route uses robot Wi-Fi, the launcher connects directly. If the target route uses another interface or gateway, the launcher checks for a direct Wi-Fi link to `192.168.2.1`. If that link is available, the launcher uses `192.168.2.1` as an SSH jump host. When the phone and robot subnets overlap, the launcher uses robot Wi-Fi. The same commands work with robot Wi-Fi alone or with a phone USB hotspot, without changes to system routes.

To select a jump host manually, use `--jump ysc@192.168.2.1`. To use your existing SSH configuration without automatic route selection, use `--direct`. The `--dry-run` option reads local routes and prints the selected path and commands. It does not connect to the robot.

Automatic selection applies only on macOS when the target is an explicit IPv4 address in `192.168.1.*`. SSH aliases, other subnets, and other operating systems use the existing SSH configuration. After a network change, run the command again. The `deploy` command uploads the server script, viewer page, and demo assets to `~/robo-comp-camera/`.

The HTTP server and SSH tunnel bind to `127.0.0.1`. The default ports are `18080` on the client and `8080` on the perception host. The HTTP server has no separate authentication. Use it through the SSH tunnel.

## HTTP endpoints

These endpoints apply to the demo, RealSense, and V4L2 servers. ROS mode uses the endpoints from `web_video_server`.

| Path | Response |
| --- | --- |
| `/` | Camera page. It hides the last frame and reconnects when the stream stops. |
| `/stream.mjpg` | MJPEG stream |
| `/snapshot.jpg` | The latest valid JPEG frame |
| `/healthz` | JSON status and frame age at the server. Returns HTTP 503 when no valid frame is available. |

The `frame_age_s` field measures the time since the server captured the latest frame. It does not measure video delay from the camera to the browser.

## Development and checks

```bash
make check                 # Run syntax checks and unit tests.
make browser-test          # Optional: run the test in local Chrome.
# For Linux or a different Chromium path:
make browser-test BROWSER=/usr/bin/google-chrome
```

To run the tests without Make:

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
python3 tests/camera_browser_smoke.py
```

CI runs the tests with multiple Python versions on Linux and with Python on macOS. A separate CI job runs the Chrome test. CI does not connect to a robot.

## Test status and limits

- RealSense `--depth` shows a depth preview with false colors. It does not give distance measurements. Depth preview, recovery after USB disconnection, extended Wi-Fi operation, and video delay still require hardware tests.
- The separate `native-depth` helper backend is not implemented. Its four tests remain skipped. Skipped tests do not count as passed checks. After you implement this backend, remove the skip markers. All tests must pass.
- The ROS page can keep the last frame after the publisher stops. Check the ROS topic frequency to confirm that new frames arrive.

## Repository layout

```text
scripts/         SSH launcher and camera server
scripts/remote/  Remote diagnostic and startup helpers
scripts/web/     Browser viewer page
scripts/assets/  Synthetic JPEG frames for the local demo
docs/            Camera usage guide
tests/           Unit tests and the optional Chrome test
.github/         CI, dependency updates, and contribution templates
```

The project uses the MIT license. See [LICENSE](LICENSE).
