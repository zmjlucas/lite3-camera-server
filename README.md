# Lite3 Dev Server

[![CI](https://github.com/zmjlucas/lite3-camera-server/actions/workflows/ci.yml/badge.svg)](https://github.com/zmjlucas/lite3-camera-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Share cameras and bounded robot communications through one development service. The browser console and Python SDK offer Lite3 UDP control, simulation and R5 serial feedback. RealSense/V4L2 camera streaming and existing SSH workflows remain supported; ROS mode remains camera-only.

**Start with the [Dev Server guide](docs/DEV_SERVER.md)** for simulation, control tokens, the `/control` console, API/SDK and deployment. Motion defaults to simulation; real dog output requires explicit hardware mode. R5 hardware motion is blocked because the source project's 2026-11-27 hold test failed; read-only feedback and simulated jogs are available. Software stop cannot prove physical stopping.

The launcher, simulation and tests use the Python standard library. R5 hardware feedback additionally needs pySerial on its host. The client does not need ROS.

The RealSense/V4L2 viewer has a **Source** dropdown for the Lite3's D435i RGB camera and arm-mounted **SXW USB Camera**. It discovers their usable `/dev/videoN` capture nodes and labels each camera. Switching stops the previous capture and briefly interrupts video; all viewers of that server share the selected source. See [camera selection](docs/CAMERA_STREAM.md#switch-between-the-d435i-and-arm-camera).

## Quick start

The launcher requires Python 3.8 or later. A robot connection also requires OpenSSH (`ssh` and `scp`) and network access to the perception host. Use a maintained Python version for development. Python 3.8 support is for older devices.

The client runs natively on Windows 10/11 and macOS. The perception host still runs Linux with Bash and its camera dependencies. Windows does not need WSL, Git Bash, Make, or ROS to run the client.

```bash
git clone https://github.com/zmjlucas/lite3-camera-server.git
cd lite3-camera-server
python3 src/client.py --help
```

Run the Python entry points in `src/` directly. You do not need `pip install`. See the [camera guide](docs/CAMERA_STREAM.md) for all procedures.

On Windows, use PowerShell and replace `python3` in the examples with `py -3` (or `python` if that is your Python 3 command):

```powershell
git clone https://github.com/zmjlucas/lite3-camera-server.git
cd lite3-camera-server
py -3 src/client.py --help
Get-Command ssh, scp
py -3 src/client.py doctor user@192.168.1.100
py -3 src/client.py view user@192.168.1.100
# For RealSense or V4L2, deploy before starting that backend:
py -3 src/client.py deploy user@192.168.1.100
py -3 src/client.py view user@192.168.1.100 --backend realsense
```

If `ssh` or `scp` is missing, install **OpenSSH Client** through Windows Optional Features, then reopen PowerShell. See [Microsoft's OpenSSH installation guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse). The local computer needs only the client component.

## Connect to a robot

Replace the example address with your perception host. Before the first connection, check the SSH host key.

```bash
# Check the host, dependencies, camera services, and ROS environment.
python3 src/client.py doctor user@192.168.1.100

# Use an existing ROS image topic and a compatible web_video_server.
python3 src/client.py view user@192.168.1.100
```

| Mode | Requirements on the perception host | Command |
| --- | --- | --- |
| ROS (default) | An active image topic, the matching ROS environment, and `web_video_server` | `view HOST` |
| RealSense | A camera that is not in use, compatible `pyrealsense2`, `numpy`, and `cv2` | Run `deploy HOST`. Then run `view HOST --backend realsense`. |
| V4L2 RGB | Linux, OpenCV with V4L2 support, and a confirmed RGB device node that is not in use | Run `deploy HOST --backend v4l2 --device /dev/videoN`. Then run `view HOST --backend v4l2 --device /dev/videoN`. |

Camera dependencies must match the device architecture, Python version, and vendor SDK. The repository does not install these dependencies from a shared requirements file. The server uses Python 3.6-compatible syntax. The full test suite requires Python 3.8 or later. Python 3.6 runtime operation remains unverified.

On macOS, `doctor`, `deploy`, and `view` read local routes and Wi-Fi interfaces to select a path to `192.168.1.*` targets. If the target route uses robot Wi-Fi, the launcher connects directly. If the target route uses another interface or gateway, the launcher checks for a direct Wi-Fi link to `192.168.2.1`. If that link is available, the launcher uses `192.168.2.1` as an SSH jump host. When the phone and robot subnets overlap, the launcher uses robot Wi-Fi. The same commands work with robot Wi-Fi alone or with a phone USB hotspot, without changes to system routes.

To select a jump host manually, use `--jump user@192.168.2.1`. To use your existing SSH configuration without automatic route selection, use `--direct`. The `--dry-run` option reads local routes and prints the selected path and commands. It does not connect to the robot.

Automatic selection applies only on macOS when the target is an explicit IPv4 address in `192.168.1.*`. SSH aliases, other subnets, and other operating systems use the existing SSH configuration. After a network change, run the command again. The `deploy` command uploads the server, control modules, arm helper and viewer pages to `~/lite3-camera-server/` and installs/enables the systemd service `lite3-camera-server.service`. The remote host needs systemd and root or sudo access; sudo can prompt in the SSH terminal.

The service starts at every boot without an SSH login, using the deployment account and its Python interpreter. Its defaults are RealSense, port 8080, 640 x 480, 15 fps, and quality 70. For V4L2, deploy with `--backend v4l2 --device /dev/v4l/by-id/NAME`, replacing `NAME` with the confirmed RGB camera link. `deploy` also accepts `--remote-port`, `--width`, `--height`, `--fps`, `--quality`, and RealSense `--depth`/`--serial` to save boot settings. It leaves current camera processes running; new settings take effect at the next boot or an explicit service restart. See [boot service management](docs/CAMERA_STREAM.md#boot-service) for migration, logs, and disabling autostart.

On Windows with robot Wi-Fi and a phone USB hotspot connected together, select the robot gateway explicitly if the direct route goes through the phone: `py -3 src/client.py view user@192.168.1.100 --jump user@192.168.2.1`. Use the same `--jump` option for `doctor` and `deploy`. Windows automatic route detection is not implemented.

The native RealSense/V4L2 server trusts the local network and listens on `0.0.0.0:8080` by default. Open `http://PERCEPTION_HOST:8080/` directly from any reachable LAN device, or use `view` for an SSH tunnel at `http://127.0.0.1:18080/`. Camera viewing needs no session token or private link; control POSTs require the configured control token. The local tunnel still binds to `127.0.0.1`.

Multiple clients can run `view` against the same native server and remote port. Clients reuse the boot service when it is listening; if no server is listening, the first client starts a detached shared server. Each client can disconnect independently. **Ctrl+C closes that client's tunnel; the native server keeps running**, even after all clients disconnect. All viewers share the selected camera and capture settings. Options such as `--depth`, `--device`, and resolution on `view` only apply when starting a new detached server. On one computer, use different `--local-port` values for separate launcher processes, or open multiple tabs on one tunnel.

See [shared server lifecycle](docs/CAMERA_STREAM.md#shared-server-lifecycle) for stopping, restarting and updating the server. A manual foreground server can use `--bind 127.0.0.1` to listen only on loopback. Requests carrying an Origin header must still match the request's origin. ROS mode continues to use the upstream `web_video_server`, its authentication policy, and its foreground SSH lifecycle.

## HTTP endpoints

These endpoints apply to the RealSense and V4L2 servers. ROS mode uses the endpoints from `web_video_server`.

The camera endpoints below remain available without credentials to clients that can reach the server. New control POST endpoints require a private bearer token; see the [control API](docs/DEV_SERVER.md#http-contract). The page's snapshot link downloads the latest displayed frame.

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

CI is configured to run offline tests on Linux, macOS, and Windows, with browser playback checks on all three. Native Windows tests include deployment file handles and junction refusal. Remote Bash and POSIX signal integration tests run on Linux/macOS and are skipped on Windows; the Windows suite still exercises launcher commands, anonymous HTTP access, interrupt cleanup, and HTTP streaming. CI does not connect to a robot. Configuration of a CI job is not a hardware acceptance result.

## Existing camera test status and limits

These hardware observations predate the dev server changes. They do not validate the new motion interfaces.

- Systemd service installation, stop/start, and automatic recovery after terminating the server process were verified on a Lite3 perception host on 2026-11-27, with live SXW and D435i RGB streams and source switching. The host was not rebooted; actual boot startup and delayed USB initialization still require hardware tests.
- Source switching was tested on connected Lite3 hardware with V4L2 and RealSense SDK capture, including restoration of the D435i false-color depth preview after switching back from SXW. RealSense `--depth` does not give calibrated distance measurements. Recovery after USB disconnection, extended Wi-Fi operation, and video delay still require hardware tests.
- The separate `native-depth` helper backend is not implemented. Its four tests remain skipped. Skipped tests do not count as passed checks. After you implement this backend, remove the skip markers. All tests must pass.
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
