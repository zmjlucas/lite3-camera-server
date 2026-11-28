# Lite3 Dev Server

[![CI](https://github.com/zmjlucas/lite3-dev-server/actions/workflows/ci.yml/badge.svg)](https://github.com/zmjlucas/lite3-dev-server/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

Share cameras and bounded robot communications through one development service. The browser console and Python SDK offer Lite3 UDP control, simulation and R5 serial feedback. RealSense/V4L2 camera streaming and existing SSH workflows remain supported; ROS mode remains camera-only.

Native deployment defaults to `dist` with hardware control, including real Lite3 commands and read-only R5 feedback. `--env dev --simulate` enables offline development. Commissioned R5 motion diagnostics require `dev` and a local profile that disables dog output. Physical motion and stopping remain unverified. [Dev Server guide](docs/DEV_SERVER.md) · [R5 diagnostics guide](docs/R5_DIAGNOSTICS.md)

Install or upgrade the Python SDK from this checkout with `python -m pip install --upgrade .`. It supports Python 3.8 and later and uses the standard library at runtime. `lite3_dev_server.__version__` and `BUILD_INFO` identify the installed release and its source. [Release and running-build checks](docs/DEV_SERVER.md#release-and-build-identity)

`ManagedClient` runs bounded actions with lease acquisition, preparation, heartbeats, result polling and cleanup. `DevClient` exposes each HTTP operation for explicit lifecycle control. For example, with a local simulation server:

```python
from lite3_dev_server import DogAction, ManagedClient

with ManagedClient('http://127.0.0.1:8080') as robot:
    result = robot.run(DogAction('forward', speed=0.1, duration=1.0))
    print(result.phase, result.reason)
```

Managed sequences require `policy='completed_only'` and stop on any other outcome, including `commanded_unverified`. Completion describes the software lifecycle; physical stopping remains unverified. [Managed sequences and low-level SDK](docs/DEV_SERVER.md#python-scripts)

Select `DevClient(url, target="simulation")` or `target="hardware"` to enable automatic transport and capability checks before acquiring control and submitting motion. Read-only `preflight()` returns structured blockers. Public parameter validators support offline SDK and CLI input checks. [Target preflight and validation](docs/DEV_SERVER.md#target-preflight-and-offline-validation)

Installation provides `lite3-control` (or `python -m lite3_dev_server`). Run `lite3-control --help` for commands, or `lite3-control --url http://127.0.0.1:8080` to query status. Motion requires an explicit `--transport simulated|real` and the complete `--execute` flag. `--dry-run` validates and prints requests offline. [CLI examples and exit codes](docs/DEV_SERVER.md#control-cli)

The SDK and control console provide timed `forward`, `backward`, `turn_left` and `turn_right` actions with speed and duration. Actions require control ownership, stand preparation, fresh standing feedback and continuing lease heartbeats. The server receives robot state passively on UDP `--dog-state-port 43897`; configure the robot's upload destination and use an unoccupied port. Simulation marks its feedback as simulated. [Action lifecycle and limits](docs/DEV_SERVER.md#predefined-dog-actions)

Use `DevClient.dog_action_status(action_id)` to read an active or retained action without a lease. Terminal results remain available for up to 5 minutes and 128 records, including after another action starts.

In `dev + simulate`, `arm_action(targets, duration)` advances multiple absolute joint targets over time. Use `wait_arm_operation(id)` to poll the accepted operation and renew an existing lease while waiting; it also supports commissioned diagnostics. See [simulated arm actions and waiting](docs/DEV_SERVER.md#simulated-arm-actions-and-waiting).

`CameraClient` provides typed source catalogs and health, JPEG snapshots, and context-managed MJPEG iteration. [Python camera examples](docs/CAMERA_STREAM.md#python-camera-sdk)

The launcher, simulation and tests use the Python standard library. The same distribution includes `lite3-sim`, `lite3-launcher`, and the `lite3_dev_server_tools.testing` module. R5 hardware feedback additionally needs pySerial on its host. The client does not need ROS.

The RealSense/V4L2 viewer has a **Source** dropdown for the Lite3's D435i RGB camera and arm-mounted **SXW USB Camera**. It discovers their usable `/dev/videoN` capture nodes and labels each camera. Add `--wide-angle-host IPV4` to enable the motion host's vendor RTSP stream as a third source. Switching stops the previous capture and briefly interrupts video. All viewers of that server share the selected source. See [camera selection](docs/CAMERA_STREAM.md#switch-camera-sources) and [wide-angle setup](docs/CAMERA_STREAM.md#enable-the-lite3-wide-angle-camera).

## Quick start

Install the tools from this checkout, then run the commands from any directory:

```bash
python -m pip install '.[tools]'
lite3-sim --port 8080
```

Open `http://127.0.0.1:8080/control` for the simulated control console. `lite3-sim` selects dev simulation with no camera and binds to `127.0.0.1`. Use `--port 0` to select a free port; the command prints the actual URL. Press Ctrl+C to stop it. Simulation requires no camera, serial device, SSH client or robot connection.

In another terminal, preview deployment with the installed launcher:

```bash
lite3-launcher deploy ysc@192.168.1.103 --direct --dry-run
```

The wheel includes the server modules, remote scripts and browser assets. The `tools` extra adds no dependencies; these tools also ship with a base SDK installation. Hardware dependencies are optional:

| Install from this checkout | Packages selected |
| --- | --- |
| `python -m pip install '.[arm]'` | pySerial for R5 serial feedback |
| `python -m pip install '.[camera]'` | NumPy and OpenCV for V4L2 capture |
| `python -m pip install '.[realsense]'` | NumPy, OpenCV and pyrealsense2 for RealSense capture |

Select hardware packages on the capture or serial host. They must support that host's architecture, Python version and vendor SDK. Device-specific vendor installation can still be required. OpenSSH, systemd, camera drivers and the wide-angle camera's FFmpeg executable are system dependencies. The standalone deployment bundle retains support for Python 3.6 devices; installing the Python distribution requires Python 3.8 or later.

For Python integration tests, use `from lite3_dev_server_tools.testing import simulated_server`. The context yields a ready HTTP URL and reaps its child process on exit. See the [simulation test example](docs/DEV_SERVER.md#simulation-in-python-tests).

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
python3 src/launcher.py --help
```

You can also run the Python entry points in `src/` directly without installation. See the [camera guide](docs/CAMERA_STREAM.md) for all procedures.

`lite3-launcher` and `src/launcher.py` provide the same SSH deployment and viewing CLI. Python applications use the `lite3_dev_server` HTTP SDK.

On Windows, use PowerShell and replace `python3` in the examples with `py -3` (or `python` if that is your Python 3 command):

```powershell
git clone https://github.com/zmjlucas/lite3-dev-server.git
cd lite3-dev-server
py -3 src/launcher.py --help
Get-Command ssh, scp
py -3 src/launcher.py doctor ysc@192.168.1.103
py -3 src/launcher.py view ysc@192.168.1.103
# For RealSense or V4L2, deploy before starting that backend:
py -3 src/launcher.py deploy ysc@192.168.1.103
py -3 src/launcher.py view ysc@192.168.1.103 --backend realsense
```

If `ssh` or `scp` is missing, install **OpenSSH Client** through Windows Optional Features, then reopen PowerShell. See [Microsoft's OpenSSH installation guide](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse). The local computer needs only the client component.

## Connect to a robot

Connect to the default perception host at `ysc@192.168.1.103`. Before the first connection, check the SSH host key.

```bash
# Check the host, dependencies, camera services, and ROS environment.
python3 src/launcher.py doctor ysc@192.168.1.103

# Use an existing ROS image topic and a compatible web_video_server.
python3 src/launcher.py view ysc@192.168.1.103
```

| Mode | Requirements on the perception host | Command |
| --- | --- | --- |
| ROS (default) | An active image topic, the matching ROS environment, and `web_video_server` | `view ysc@192.168.1.103` |
| RealSense | A camera that is not in use, compatible `pyrealsense2`, `numpy`, and `cv2` | Run `deploy ysc@192.168.1.103`. Then run `view ysc@192.168.1.103 --backend realsense`. |
| V4L2 RGB | Linux, OpenCV with V4L2 support, and a confirmed RGB device node that is not in use | Run `deploy ysc@192.168.1.103 --backend v4l2 --device /dev/videoN`. Then run `view ysc@192.168.1.103 --backend v4l2 --device /dev/videoN`. |

Camera dependencies must match the device architecture, Python version, and vendor SDK. The package extras above select optional Python dependencies; deployment uses the packages already installed on the device. The server uses Python 3.6-compatible syntax. The full test suite requires Python 3.8 or later.

On macOS, `doctor`, `deploy`, `service`, and `view` read local routes and Wi-Fi interfaces to select a path to `192.168.1.*` targets. If the target route uses robot Wi-Fi, the launcher connects directly. If the target route uses another interface or gateway, the launcher checks for a direct Wi-Fi link to `192.168.2.1`. If that link is available, the launcher uses `192.168.2.1` as an SSH jump host. When the phone and robot subnets overlap, the launcher uses robot Wi-Fi. The same commands work with robot Wi-Fi alone or with a phone USB hotspot, without changes to system routes.

To select a jump host manually, use `--jump ysc@192.168.2.1`. To use your existing SSH configuration without automatic route selection, use `--direct`. The `--dry-run` option reads local routes and prints the selected path and commands. It does not connect to the robot.

Automatic selection applies only on macOS when the target is an explicit IPv4 address in `192.168.1.*`. SSH aliases, other subnets, and other operating systems use the existing SSH configuration. After a network change, run the command again. The `deploy` command uploads the server, control modules, arm helper and viewer pages to `~/lite3-dev-server/` and installs/enables the systemd service `lite3-dev-server.service`. The remote host needs systemd and root or sudo access; sudo can prompt in the SSH terminal.

The service starts at every boot without an SSH login, using the deployment account and its Python interpreter. Its defaults are RealSense, port 8080, 640 x 480, 15 fps, and quality 70. For V4L2, deploy with `--backend v4l2 --device /dev/v4l/by-id/NAME`, replacing `NAME` with the confirmed RGB camera link. `deploy` also accepts `--remote-port`, `--width`, `--height`, `--fps`, `--quality`, `--wide-angle-host`, and RealSense `--depth`/`--serial` to save boot settings. New settings take effect at the next boot or an explicit service restart. See [boot service management](docs/CAMERA_STREAM.md#boot-service) for migration, logs, and disabling autostart.

Inspect and activate the deployed service from the same launcher:

```bash
python3 src/launcher.py service status ysc@192.168.1.103
python3 src/launcher.py service logs ysc@192.168.1.103 --lines 100
python3 src/launcher.py service restart ysc@192.168.1.103
# Optional: require the build identifier printed by deploy.
python3 src/launcher.py service restart ysc@192.168.1.103 --expect-build sha256:YOUR_DEPLOYED_DIGEST
```

These commands support `--jump`, `--direct`, and `--dry-run`, with the usual SSH/sudo prompts. Use `--remote-port` when deployment uses a port other than 8080. Restart checks systemd, port ownership and HTTP readiness, including `--backend none` deployments. It reports active legacy services and conflicting listeners for the operator to resolve. Deployment preserves the running process; restart interrupts its viewers and control sessions.

On Windows with robot Wi-Fi and a phone USB hotspot connected together, select the robot gateway explicitly if the direct route goes through the phone: `py -3 src/launcher.py view ysc@192.168.1.103 --jump ysc@192.168.2.1`. Use the same `--jump` option for `doctor`, `deploy`, and `service`. Windows automatic route detection is not implemented.

The native RealSense/V4L2 server trusts the local network and listens on `0.0.0.0:8080` by default. Open `http://192.168.1.103:8080/` directly from any reachable LAN device, or use `view` for an SSH tunnel at `http://127.0.0.1:18080/`. All camera and control endpoints are available to clients on the trusted LAN. The local tunnel still binds to `127.0.0.1`.

Multiple clients can run `view` against the same native server and remote port. Clients reuse a control-capable listener when its environment, simulation setting, control mode and R5 diagnostics setting match the request. If no server is listening, the first client starts a detached shared server. **Ctrl+C closes that client's tunnel. The native server keeps running**, even after all clients disconnect. All viewers share the selected camera and capture settings. Options such as `--depth`, `--device`, and resolution on `view` only apply when starting a new detached server. On one computer, use different `--local-port` values for separate launcher processes, or open multiple tabs on one tunnel.

To connect to an existing service, including one managed by systemd, use `--existing-only`:

```bash
python3 src/launcher.py view ysc@192.168.1.103 --existing-only --console
python3 src/launcher.py view ysc@192.168.1.103 --existing-only --url-only \
  --jump ysc@192.168.2.1 --local-port 18081 --remote-port 8080
```

This mode creates only an SSH forward and checks the dev server's HTTP protocols. The service keeps its current environment, simulation, control, diagnostics and camera settings. Supply connection options; explicit server launch options such as `--backend` and `--env` are rejected. `--url-only` prints the verified URL to stdout, keeps diagnostics on stderr and leaves the tunnel running for SDK clients. Keep the terminal open until finished. Readiness checks run even with `--no-open`; the default timeout is 15 seconds, configurable with `--wait-timeout SECONDS` (1–300). An unavailable or incompatible listener produces a diagnostic and a failure exit status. See [existing-service connections](docs/CAMERA_STREAM.md#connect-to-an-existing-service) for details.

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

For SDK release checks, use a development virtual environment (Python 3.11 recommended):

```bash
python -m pip install '.[dev]'
python -B scripts/check_distribution.py
node tests/dev_client_test.js  # Node 22+ for browser client contract checks
```

The distribution check runs SDK and simulation-context typing checks, builds a source distribution and its wheel, and installs the tools into a clean temporary environment. It checks an external typed consumer, both console commands, launcher previews, browser assets, the simulation context and HTTP control against the packaged deployment bundle. CI runs these checks on Linux, macOS and Windows. SDK and tools installations use only the standard library at runtime; hardware extras are opt-in.

## Camera limitations

- RealSense `--depth` does not give calibrated distance measurements.
- The separate `native-depth` helper backend is not implemented.
- The ROS page can keep the last frame after the publisher stops. Check the ROS topic frequency to confirm that new frames arrive.

## Repository layout

```text
src/server.py    Server configuration, startup and shutdown
src/http_server.py  HTTP parsing, response/error mapping, streaming and static assets
src/application.py  Control and arm observation orchestration
src/camera.py    Frame lifecycle, capture backends and camera controller
src/camera_sources.py  Camera discovery and labels
src/control.py   Control ownership, watchdog and UDP writer
src/arm_gateway.py  Read-only serial gateway
src/arm_motion.py   Commissioned arm operations
src/arm_actions.py  Timed simulated arm targets
src/launcher.py  SSH deployment, diagnosis and viewing CLI
src/simulation.py  Installed loopback simulation command
src/testing.py   Reusable subprocess simulation context
src/lite3_dev_server/  Installable HTTP SDK, types and response validation
src/remote/      Remote diagnostic and startup helpers
src/web/         Camera viewer, control UI and browser HTTP client
docs/            Development API and camera usage guides
tests/           Unit tests and the optional Chrome test
.github/         CI, dependency updates, and contribution templates
```

Reserve `scripts/` for repository maintenance scripts. Runtime helpers belong in `src/remote/`. Offline camera inputs and browser fixtures belong in `tests/fixtures/` and are not deployed.

HTTP requests reach `ControlApplication`, which coordinates `ControlService` and arm gateways. Serial reads reconcile capabilities with the current control state after I/O; the independently scheduled watchdog continues to own command expiry. `build_info.BUNDLE_FILES` lists the deployed modules and assets shared by `launcher.SERVER_BUNDLE` and startup build identification, with the server entry point uploaded last. Device modules keep Python 3.6 syntax and avoid runtime imports of the Python 3.8 SDK.

Setuptools maps the runtime modules in `src/` to the installed `lite3_dev_server_tools` package. The SDK remains in `lite3_dev_server`. Both packages use the same distribution; runtime files have one source tree for installation and deployment.

The project uses the MIT license. See [LICENSE](LICENSE).
