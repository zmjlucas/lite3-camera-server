# Lite3 Dev Server

[![CI](https://github.com/zmjlucas/lite3-dev-server/actions/workflows/ci.yml/badge.svg)](https://github.com/zmjlucas/lite3-dev-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Share cameras and bounded robot communications through one development service. The browser console and Python SDK offer Lite3 UDP control, simulation and R5 serial feedback. RealSense/V4L2 camera streaming and existing SSH workflows remain supported; ROS mode remains camera-only.

Native deployment defaults to `dist` with hardware control, including real Lite3 commands and read-only R5 feedback. `--env dev --simulate` enables offline development. Commissioned R5 motion diagnostics require `dev` and a local profile that disables dog output. Physical motion and stopping remain unverified. [Dev Server guide](docs/DEV_SERVER.md) · [R5 diagnostics guide](docs/R5_DIAGNOSTICS.md)

Install the Python SDK from this checkout with `python -m pip install .` and import `DevClient` from `lite3_dev_server`. It supports Python 3.8 and later and uses only the standard library at runtime.

The launcher, simulation and tests use the Python standard library. R5 hardware feedback additionally needs pySerial on its host. The client does not need ROS.

The RealSense/V4L2 viewer has a **Source** dropdown for the Lite3's D435i RGB camera and arm-mounted **SXW USB Camera**. It discovers their usable `/dev/videoN` capture nodes and labels each camera. Add `--wide-angle-host IPV4` to enable the motion host's vendor RTSP stream as a third source. Switching stops the previous capture and briefly interrupts video. All viewers of that server share the selected source. See [camera selection](docs/CAMERA_STREAM.md#switch-camera-sources) and [wide-angle setup](docs/CAMERA_STREAM.md#enable-the-lite3-wide-angle-camera).

## Quick start

Use these default robot connection values in code and examples:

| Setting | Default |
| --- | --- |
| Username | `ysc` |
| Password | `'` (one ASCII single quote) |
| Perception host | `192.168.1.103` |
| Motion control (UDP) | `192.168.1.120:43893` |

Use `ysc@192.168.1.103` for SSH. Enter `'` at SSH or sudo password prompts. The dev server assumes a secure, trusted network with no public internet access. All endpoints work without authentication. Do not add authentication to the dev server.

The launcher requires Python 3.8 or later. A robot connection also requires OpenSSH (`ssh` and `scp`) and network access to the perception host. Use a maintained Python version for development. Python 3.8 support is for older devices.

The client runs natively on Windows 10/11 and macOS. The perception host still runs Linux with Bash and its camera dependencies. Windows does not need WSL, Git Bash, Make, or ROS to run the client.

```bash
git clone https://github.com/zmjlucas/lite3-dev-server.git
cd lite3-dev-server
python3 src/client.py --help
```

Run the Python entry points in `src/` directly. You do not need `pip install`. See the [camera guide](docs/CAMERA_STREAM.md) for all procedures.

On Windows, use PowerShell and replace `python3` in the examples with `py -3` (or `python` if that is your Python 3 command):

```powershell
git clone https://github.com/zmjlucas/lite3-dev-server.git
cd lite3-dev-server
py -3 src/client.py --help
Get-Command ssh, scp
py -3 src/client.py doctor ysc@192.168.1.103
py -3 src/client.py view ysc@192.168.1.103
# For RealSense or V4L2, deploy before starting that backend:
py -3 src/client.py deploy ysc@192.168.1.103
py -3 src/client.py view ysc@192.168.1.103 --backend realsense
```

If `ssh` or `scp` is missing, install **OpenSSH Client** through Windows Optional Features, then reopen PowerShell. See [Microsoft's OpenSSH installation guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse). The local computer needs only the client component.

## Connect to a robot

Connect to the default perception host at `ysc@192.168.1.103`. Before the first connection, check the SSH host key.

```bash
# Check the host, dependencies, camera services, and ROS environment.
python3 src/client.py doctor ysc@192.168.1.103

# Use an existing ROS image topic and a compatible web_video_server.
python3 src/client.py view ysc@192.168.1.103
```

| Mode | Requirements on the perception host | Command |
| --- | --- | --- |
| ROS (default) | An active image topic, the matching ROS environment, and `web_video_server` | `view ysc@192.168.1.103` |
| RealSense | A camera that is not in use, compatible `pyrealsense2`, `numpy`, and `cv2` | Run `deploy ysc@192.168.1.103`. Then run `view ysc@192.168.1.103 --backend realsense`. |
| V4L2 RGB | Linux, OpenCV with V4L2 support, and a confirmed RGB device node that is not in use | Run `deploy ysc@192.168.1.103 --backend v4l2 --device /dev/videoN`. Then run `view ysc@192.168.1.103 --backend v4l2 --device /dev/videoN`. |

Camera dependencies must match the device architecture, Python version, and vendor SDK. The repository does not install these dependencies from a shared requirements file. The server uses Python 3.6-compatible syntax. The full test suite requires Python 3.8 or later.

On macOS, `doctor`, `deploy`, and `view` read local routes and Wi-Fi interfaces to select a path to `192.168.1.*` targets. If the target route uses robot Wi-Fi, the launcher connects directly. If the target route uses another interface or gateway, the launcher checks for a direct Wi-Fi link to `192.168.2.1`. If that link is available, the launcher uses `192.168.2.1` as an SSH jump host. When the phone and robot subnets overlap, the launcher uses robot Wi-Fi. The same commands work with robot Wi-Fi alone or with a phone USB hotspot, without changes to system routes.

To select a jump host manually, use `--jump ysc@192.168.2.1`. To use your existing SSH configuration without automatic route selection, use `--direct`. The `--dry-run` option reads local routes and prints the selected path and commands. It does not connect to the robot.

Automatic selection applies only on macOS when the target is an explicit IPv4 address in `192.168.1.*`. SSH aliases, other subnets, and other operating systems use the existing SSH configuration. After a network change, run the command again. The `deploy` command uploads the server, control modules, arm helper and viewer pages to `~/lite3-camera-server/` and installs/enables the systemd service `lite3-camera-server.service`. The remote host needs systemd and root or sudo access; sudo can prompt in the SSH terminal.

The service starts at every boot without an SSH login, using the deployment account and its Python interpreter. Its defaults are RealSense, port 8080, 640 x 480, 15 fps, and quality 70. For V4L2, deploy with `--backend v4l2 --device /dev/v4l/by-id/NAME`, replacing `NAME` with the confirmed RGB camera link. `deploy` also accepts `--remote-port`, `--width`, `--height`, `--fps`, `--quality`, `--wide-angle-host`, and RealSense `--depth`/`--serial` to save boot settings. New settings take effect at the next boot or an explicit service restart. See [boot service management](docs/CAMERA_STREAM.md#boot-service) for migration, logs, and disabling autostart.

On Windows with robot Wi-Fi and a phone USB hotspot connected together, select the robot gateway explicitly if the direct route goes through the phone: `py -3 src/client.py view ysc@192.168.1.103 --jump ysc@192.168.2.1`. Use the same `--jump` option for `doctor` and `deploy`. Windows automatic route detection is not implemented.

The native RealSense/V4L2 server trusts the local network and listens on `0.0.0.0:8080` by default. Open `http://192.168.1.103:8080/` directly from any reachable LAN device, or use `view` for an SSH tunnel at `http://127.0.0.1:18080/`. All camera and control endpoints are available to clients on the trusted LAN. The local tunnel still binds to `127.0.0.1`.

Multiple clients can run `view` against the same native server and remote port. Clients reuse a control-capable listener when its environment, simulation setting, control mode and R5 diagnostics setting match the request. If no server is listening, the first client starts a detached shared server. **Ctrl+C closes that client's tunnel. The native server keeps running**, even after all clients disconnect. All viewers share the selected camera and capture settings. Options such as `--depth`, `--device`, and resolution on `view` only apply when starting a new detached server. On one computer, use different `--local-port` values for separate launcher processes, or open multiple tabs on one tunnel.

See [shared server lifecycle](docs/CAMERA_STREAM.md#shared-server-lifecycle) for stopping, restarting and updating the server. A manual foreground server can use `--bind 127.0.0.1` to listen only on loopback. Requests carrying an Origin header must still match the request's origin. ROS mode continues to use the upstream `web_video_server` and its foreground SSH lifecycle.

## HTTP endpoints

These endpoints apply to the RealSense and V4L2 servers. ROS mode uses the endpoints from `web_video_server`.

The camera endpoints below and the [control API](docs/DEV_SERVER.md#http-contract) are available without credentials to clients that can reach the server. The page's snapshot link downloads the latest displayed frame.

| Path | Response |
| --- | --- |
| `/` | Camera page. It hides the last frame and reconnects when the stream stops. |
| `/stream.mjpg` | MJPEG stream |
| `/snapshot.jpg` | The latest valid JPEG frame |
| `/healthz` | JSON status and frame age at the server. Returns HTTP 503 when no valid frame is available. |
| `/sources` | JSON catalog of supported detected cameras and the selected source ID. |
| `/source` | POST with JSON `{"id": "<catalog source ID>"}` to switch capture. Returns the updated catalog. |

The `frame_age_s` field measures the time since the server captured the latest frame. It does not measure video delay from the camera to the browser.

## Development and checks

```bash
make check                 # Run syntax checks and unit tests.
make browser-test          # Optional: detect local Chrome/Chromium/Edge.
# For Linux or a different Chromium path:
make browser-test BROWSER=/usr/bin/google-chrome
```

To run the checks without Make on macOS/Linux:

```bash
python3 -B scripts/check.py --shell
python3 -B tests/camera_browser_smoke.py
python3 -B tests/camera_sources_browser_smoke.py
```

On Windows (PowerShell):

```powershell
py -3 -B scripts/check.py
py -3 -B tests/camera_browser_smoke.py
py -3 -B tests/camera_sources_browser_smoke.py
# Optional explicit browser path:
py -3 -B tests/camera_browser_smoke.py --browser 'C:\Program Files\Google\Chrome\Application\chrome.exe'
```

CI is configured to run offline tests on Linux, macOS, and Windows, with browser playback checks on all three. Native Windows tests include deployment file handles and junction refusal. Remote Bash and POSIX signal integration tests run on Linux/macOS and are skipped on Windows; the Windows suite still exercises launcher commands, anonymous HTTP access, interrupt cleanup, and HTTP streaming.

## Camera limitations

- RealSense `--depth` does not give calibrated distance measurements.
- The separate `native-depth` helper backend is not implemented.
- The ROS page can keep the last frame after the publisher stops. Check the ROS topic frequency to confirm that new frames arrive.

## Repository layout

```text
src/             SSH launcher, dev server, motion gateway and Python SDK
src/remote/      Remote diagnostic and startup helpers
src/web/         Camera viewer and development console
docs/            Development API and camera usage guides
tests/           Unit tests and the optional Chrome test
.github/         CI, dependency updates, and contribution templates
```

Reserve `scripts/` for repository maintenance scripts. Runtime helpers belong in `src/remote/`. Offline camera inputs and browser fixtures belong in `tests/fixtures/` and are not deployed.

The project uses the MIT license. See [LICENSE](LICENSE).
