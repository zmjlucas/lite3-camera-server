# View the Lite3 camera from Windows or macOS

The computer connects to the Linux perception host through SSH. The browser shows the camera stream through this connection. The Windows or macOS client needs Python 3.8 or later, OpenSSH (`ssh` and `scp`), and a browser. The camera commands do not send motion commands. Camera drivers, ROS and Bash run on the perception host.

Run these commands from the repository root. All robot connection examples use these defaults:

| Setting | Default |
| --- | --- |
| Username | `ysc` |
| Password | `'` (one ASCII single quote) |
| Perception host | `192.168.1.103` |
| Motion control (UDP) | `192.168.1.120:43893` |

Use `ysc@192.168.1.103` for SSH. Enter `'` at SSH or sudo password prompts. The camera and control APIs are available without authentication on the trusted LAN.

Examples below use `python3` for macOS/Linux. In Windows PowerShell, replace it with `py -3` or your installed `python` command. No local Bash or WSL is required. Confirm `Get-Command ssh, scp` succeeds; otherwise install **OpenSSH Client** in Windows Optional Features and reopen the terminal. See [Microsoft's instructions](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse).

```powershell
py -3 src/launcher.py doctor ysc@192.168.1.103
py -3 src/launcher.py deploy ysc@192.168.1.103
py -3 src/launcher.py view ysc@192.168.1.103 --backend realsense
```

Deployment refuses symbolic links in source paths and, on Windows, all reparse points including junctions. Use a normal local checkout if a redirected or cloud-managed folder is rejected. Capture files are read before SSH login and only allowlisted runtime assets are uploaded. The service also provides the [development control API and console](DEV_SERVER.md).

## Connect to the robot

1. Connect the computer to the robot's Wi-Fi or the specified wired network.
2. Before the first SSH connection, check the host key.
3. Check the camera interfaces on the perception host:

```bash
python3 src/launcher.py doctor ysc@192.168.1.103
```

The `doctor` command summarizes the installed bundle, running service, RGB devices, UDP feedback and serial readiness, alongside system details and ROS environments. It works before deployment. Checks use filesystem metadata, visible process descriptors, query-only V4L2 discovery and the running service's passive `OPTIONS /api/diagnostics` snapshot. Legacy servers reject this method without entering their control status handlers. Existing camera capture, control ownership, serial sessions and commissioning records are preserved. Doctor leaves service lifecycle and device settings unchanged.

The report identifies the installed bundle by SHA-256 and compares it with the bundle fingerprint captured at server startup. `matches_running: false` indicates changed files on disk; schedule an explicit restart when ready to load them. An older server without passive diagnostics leaves the running version and feedback `unverified`. Each missing tool or inaccessible source of evidence is reported independently.

```bash
python3 src/launcher.py doctor ysc@192.168.1.103 --remote-port 8080
# Optional paths and account for checking a planned installation:
python3 src/launcher.py doctor ysc@192.168.1.103 --service-user ysc \
  --arm-device /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0 --dog-state-port 43897
```

`--remote-port` selects the existing HTTP listener. `--install-dir` selects a bundle directory (default: `~/lite3-dev-server`). Actual runtime identity and device configuration are used when available; `--service-user`, `--arm-device`, `--dog-host` and `--dog-state-port` provide inspection overrides. UDP feedback retains its actual receiver host/port; requested socket inspections are labeled separately. Defaults are labeled when runtime configuration cannot be obtained. Device permission checks evaluate Unix mode bits and parent traversal for the service account, including its supplementary groups. ACLs and systemd device policies remain unverified. Dependency imports use the doctor's Python interpreter and SSH account; the service interpreter is reported separately, with its environment unverified. An empty ownership list covers only visible processes and cannot establish exclusive availability. RGB ownership by the running service is expected during capture; sibling interfaces and USB device handles are included.

UDP conclusions distinguish `port_conflict`, `bind_failed`, `feedback_missing`, `feedback_stale`, `feedback_fresh` and `invalid_feedback`. A listed UDP socket establishes listener presence; observed packet freshness comes from the running receiver. Simulation, readonly/disabled mode and commissioned arm-only sessions mark hardware dog reception `not_applicable`. The report includes upload destination, source IP, packet-layout and firewall checks. Doctor never binds a UDP port, sends robot commands, opens serial or reads/claims a commissioning profile. Remote arm configurations leave local serial checks `not_applicable`.

To inspect the route and commands, add `--dry-run` to `doctor`, `deploy`, or `view`. This option reads local routes and prints the selected path and commands. It does not log in to or probe the robot.

Windows dry-run output shows exact command arguments as JSON for inspection; it is not a PowerShell script to paste and execute. Run the launcher without `--dry-run` to let Python pass the arguments directly to OpenSSH, including embedded quotes and newlines.

### Select routes for robot Wi-Fi and phone USB networks

On macOS, use the same commands with robot Wi-Fi alone or with robot Wi-Fi and a phone USB hotspot. The launcher selects the connection path. You do not need to add or remove `--jump` for these two network conditions.

On Windows, the launcher uses your existing SSH configuration and system routes. With a phone hotspot and robot Wi-Fi connected together, select the robot gateway with `--jump` if the direct target route goes through the phone. Apply this option to all three commands:

```powershell
py -3 src/launcher.py doctor ysc@192.168.1.103 --jump ysc@192.168.2.1
py -3 src/launcher.py deploy ysc@192.168.1.103 --jump ysc@192.168.2.1
py -3 src/launcher.py view ysc@192.168.1.103 --backend realsense --jump ysc@192.168.2.1
```

The automatic selection details below apply to macOS only.

```bash
python3 src/launcher.py doctor ysc@192.168.1.103
python3 src/launcher.py view ysc@192.168.1.103
# Deploy only when you need the RealSense or V4L2 server described below.
python3 src/launcher.py deploy ysc@192.168.1.103
# Inspect the selected route without a robot connection.
python3 src/launcher.py view ysc@192.168.1.103 --dry-run
```

The launcher uses macOS `route -n get` to read routes to the perception host and robot gateway. It uses `networksetup -listallhardwareports` to find Wi-Fi hardware interfaces. Selection does not depend on the phone model or USB interface number.

| Current route | Selected path |
| --- | --- |
| The target uses a direct robot Wi-Fi link or the Wi-Fi gateway `192.168.2.1`. | Connect directly to the target through SSH. |
| The target uses another interface or gateway, and `192.168.2.1` has a direct Wi-Fi link. | Add `-J ysc@192.168.2.1`. Use robot Wi-Fi when the phone and robot subnets overlap. |
| The target route is missing or cannot be read, but the robot gateway has a confirmed direct Wi-Fi link. | Connect through the confirmed robot gateway. |
| No direct Wi-Fi link to the robot gateway is confirmed, or interface information is missing. | Use the existing direct SSH command. If the target also lacks a direct link, show the reason and manual options. |

The terminal's `Route:` message shows the selected path and related interface or gateway. This message reports a local route decision. It does not confirm SSH connectivity. The `deploy` command uses the same route decision for SSH and SCP. An automatic jump uses the username in the target address. If the target has no username, OpenSSH selects one.

Automatic selection requires macOS and an explicit `192.168.1.*` IPv4 target. Automatic jump selection also requires the default robot gateway, `192.168.2.1`, on a direct Wi-Fi link. The launcher does not identify devices by Wi-Fi name or discover the perception host's IP address. SSH aliases, hostnames, other subnets, and other operating systems use the existing SSH configuration.

Use a manual option to select the robot gateway or a preferred wired interface:

```bash
python3 src/launcher.py view ysc@192.168.1.103 --jump ysc@192.168.2.1
python3 src/launcher.py view ysc@192.168.1.103 --direct
```

The `--jump` and `--direct` options both bypass automatic selection. You cannot use them together. The `--direct` option adds no automatic jump but still uses proxies from the existing SSH configuration. The `--jump` option accepts one SSH alias, hostname, or SSH target such as `ysc@192.168.2.1`. It uses OpenSSH `-J` for SSH and SCP.

The jump host forwards the SSH connection to the perception host, where the camera commands run. The native server defaults to `dist` with real hardware control available. Use `--control-mode readonly` to keep R5 feedback with motion disabled. Both hosts require authentication and host key checks. OpenSSH handles password prompts. The launcher leaves existing routes and SSH configuration in place. [Dev Server guide](DEV_SERVER.md)

The launcher selects the route each time a command starts. After a USB or Wi-Fi connection change, stop the preview. Then run the command again.

### Use an existing ROS image topic

Use an active RGB image topic when one is available. The perception host must run a camera publisher node. Its ROS environment must contain `web_video_server`. The `view` command starts the preview service and forwards its port. The browser page lists the available image topics.

```bash
python3 src/launcher.py view ysc@192.168.1.103
```

If the host has multiple ROS environments, use `--ros-setup` to select the camera node's environment. For example, use `--ros-setup /opt/ros/melodic/setup.bash`. You can also select a workspace `setup.bash` that provides the required packages. The launcher uses `ROS_VERSION` to select ROS 1 or ROS 2. After you find the image topic, use `--topic /camera/image_topic` with its actual name.

ROS 1 uses a separate node name for each preview to prevent conflicts with other previews. ROS 2 requests the `sensor_data` subscription profile to support cameras that use best-effort publishing. This requires a `web_video_server` build with `default_qos_profile` support. If an older build shows no image, compare its subscription QoS with the camera's publishing QoS. A topic in the list does not confirm that the preview works.

ROS mode uses the page from `web_video_server`. That page can keep the last frame when the publisher stops. Use `rostopic hz` or `ros2 topic hz` to check whether new frames arrive. This repository's stream failure messages and automatic frame hiding apply to RealSense and V4L2.

If `web_video_server` is missing, use this procedure on the perception host:

1. Load the ROS environment that matches the camera node.
2. Check the configured package sources with `apt-cache policy "ros-${ROS_DISTRO}-web-video-server"`.
3. If a candidate version is available, install it with `sudo apt install "ros-${ROS_DISTRO}-web-video-server"`.

Older environments, such as Melodic or Foxy, might have no candidate package. In that case, use a build that matches the vendor system or the RealSense method below. Dependencies in newer upstream code can exceed what an older system image supports.

### Read RealSense directly

You can read RGB frames directly when the camera is not in use. Python 3 on the perception host must import `pyrealsense2`, `numpy`, and `cv2`. The vendor manual states that the system image includes native librealsense. Use `doctor` to check whether the Python bindings are available.

```bash
python3 src/launcher.py deploy ysc@192.168.1.103
python3 src/launcher.py view ysc@192.168.1.103 --backend realsense
```

The `deploy` command uploads the server, control modules, fixed arm helper and viewer pages to `~/lite3-dev-server/` and its `web/` and `remote/` subdirectories on the perception host, then runs a remote installer to enable the [systemd boot service](#boot-service). It rejects symlinked source paths and does not copy unrelated files or Git-ignored content. Defaults are 640 x 480 pixels, 15 fps, and JPEG quality 70. Use `deploy --width`, `--height`, `--fps`, and `--quality` to save boot settings. If multiple RealSense cameras are connected, use `deploy --serial` to select one. Matching options on `view` apply only if it starts a new detached server.

To show a depth preview beside RGB, add `--depth`. The preview uses false colors. Do not use these colors as distance measurements.

### Read RGB through V4L2

Use V4L2 when the host has OpenCV with V4L2 support but lacks `pyrealsense2` or `web_video_server`. First, check the device and its supported formats on the perception host. Do not assume that `/dev/video0` is the RGB device node. This example uses a confirmed RGB device at `/dev/video4`:

```bash
v4l2-ctl --list-devices
v4l2-ctl --device /dev/video4 --list-formats-ext
fuser /dev/video4
```

Before you deploy the server, confirm these conditions:

- The node provides RGB frames.
- The device supports the selected resolution and frame rate.
- No ROS camera node, vendor viewer, or other program uses the device.

If the camera already publishes ROS images, use the existing topic. Do not stop vendor services for a preview.

From the client computer, run these commands in order:

```bash
python3 src/launcher.py deploy ysc@192.168.1.103 --backend v4l2 --device /dev/video4
python3 src/launcher.py view ysc@192.168.1.103 --backend v4l2 --device /dev/video4
```

The launcher selects the connection path with the rules above. Set `--device` to a confirmed `/dev/videoN` or `/dev/v4l/by-id/name` path. Device numbers can change after a restart or a USB connection change. V4L2 mode provides RGB only. It does not support `--depth` or `--serial`.

Use `--width`, `--height`, `--fps`, and `--quality` to request settings that the device supports. Check the actual image resolution and frame rate after the stream starts.

### Switch camera sources

After deploying the current version, start the RealSense or V4L2 viewer with either command above. Use the **Source** dropdown to select a detected camera. Entries show the current device node and camera name, such as `/dev/video4 — D435i RGB` or `/dev/video6 — Arm camera (SXW USB Camera)`. These numbers are examples; the server detects the current numbering. Use **Refresh** to scan again after connecting a camera.

Local discovery offers supported D435i RGB and SXW USB Camera capture nodes. Auxiliary metadata, depth, infrared and unrelated camera nodes are excluded. Identifiable RGB ports remain in the dropdown when unavailable, with disabled options and the reason shown both in the option and below the controls. For example, a camera held by `realsense-viewer` shows the owning process when it is visible to the server. Release the camera in that application, or correct the reported connection/permission problem, then use **Refresh** to enable the option again. Rejecting an unavailable selection leaves the current capture running.

Discovery queries capabilities and reads Linux process descriptors without starting capture or stopping other applications. The server ignores its own descriptors so shared viewers can keep using its capture. An external handle on a camera's video interfaces or USB device is conservatively treated as in use. If capability probing fails, sysfs must identify the RGB capture interface; unidentified nodes stay excluded. Process inspection depends on Linux permissions, and another application can acquire a camera after a refresh, so capture may still report a later error.

One camera streams at a time. Selecting another source clears the old image and snapshot, stops the previous capture, and starts the selected camera. The HTTP server and SSH tunnels stay open, so viewer URLs remain valid. Selection affects every viewer connected to this server. If a driver blocks during capture shutdown, the server refuses to open a second camera; stop and restart the shared server after resolving the device problem.

The SXW arm camera uses V4L2 with MJPG input. A V4L2 session uses YUYV for D435i RGB. A session launched with `--backend realsense` uses the RealSense SDK for D435i and restores its original `--depth` setting when switching back to D435i. The dropdown applies to this server's RealSense/V4L2 viewer. ROS mode uses the external topic viewer.

If a selection fails, read the source error and check the connection, permissions, camera usage, and requested resolution/frame rate. Use **Retry camera** to restart the selected camera after a capture failure, or refresh the list and select another camera. Explicit `--device` paths remain supported for starting V4L2; the web selection API accepts only IDs from the detected catalog, not arbitrary device paths.

### Python camera SDK

Install the SDK from this checkout with `python -m pip install .` (Python 3.8+). `CameraClient` uses the existing camera HTTP endpoints and standard-library networking. It connects directly to the configured HTTP(S) origin, with redirects and environment proxies disabled. You can use the LAN address below or an existing tunnel at `http://127.0.0.1:18080`.

```python
from pathlib import Path
from lite3_dev_server import CameraClient, CameraFrameUnavailable

camera = CameraClient("http://192.168.1.103:8080")
health = camera.health()
print(health["status"], health["frame_age_s"], health["error"])

try:
    jpeg = camera.snapshot()
except CameraFrameUnavailable as error:
    print(error.health["status"], error.health["error"])
else:
    Path("snapshot.jpg").write_bytes(jpeg)
```

`health()` returns `CameraHealth` even when the service responds with HTTP 503. Its `status` is `starting` while awaiting the first frame, `live` for a usable frame, `stale` after frame expiry, or `error` for a capture failure. It also reports `source`, nullable `source_id`, nullable `frame_age_s`, nullable `error`, and `generation`. Frame age measures elapsed time at the server; it does not establish end-to-end image latency.

When `snapshot()` or `stream()` initially receives that 503 health response, it raises `CameraFrameUnavailable`. The exception carries the full typed `health` and `status == 503`. Other request, protocol, parsing and transport failures raise `CameraClientError`, with an HTTP `status` when available. The SDK validates known JSON fields and preserves additive fields.

```python
try:
    with camera.stream() as frames:
        for jpeg in frames:
            # Pass these bytes to your application's decoder or image processor.
            Path("latest.jpg").write_bytes(jpeg)
            break
except CameraFrameUnavailable as error:
    print(error.health["status"], error.health["error"])
```

`CameraStream` yields each JPEG as `bytes`. Exiting `with` releases the response connection, including early breaks and application exceptions. Explicit `close()` is also available. A server multipart terminator ends iteration and closes the stream; capture failure, frame expiry, and source switching can each cause this. Inspect `health()` afterward and open another stream when your application is ready. Truncated/malformed streams raise `CameraClientError` and close the response. The SDK leaves retry timing to the application.

`timeout` defaults to 5 seconds and accepts finite values above zero through 10 seconds. It bounds individual blocking socket operations, including stream reads. `max_frame_bytes` defaults to 16 MiB and limits both snapshots and each MJPEG frame. JSON replies and per-frame headers are also bounded. The SDK returns the original JPEG bytes and requires no image-decoding dependency.

Source selection is a separate, explicit request:

```python
from lite3_dev_server import CameraClientError

catalog = camera.sources()  # CameraSourceCatalog, containing CameraSource entries
for source in catalog["sources"]:
    print(source["id"], source["label"], source["available"], source["error"])

def select_camera(source_id: str) -> None:
    try:
        selected = camera.select_source(source_id)  # Use an ID from this catalog.
        print(selected["selected"])
    except CameraClientError as error:
        print(error.status, str(error))  # Includes the server's unavailable-device reason.
```

**Calling `select_source()` changes the shared capture for every viewer connected to that service.** Capture starts asynchronously; inspect health or handle `CameraFrameUnavailable` while the selected camera starts. Catalog availability is a point-in-time observation, and the server checks it again when selecting. Reading sources, health, snapshots or streams never selects a source or acquires a robot-control lease.

### Enable the Lite3 wide-angle camera

Add the motion host's IPv4 address with `--wide-angle-host` to make its vendor camera stream selectable in a RealSense or V4L2 session. The decoder runs on the Linux perception host and needs OpenCV with the FFmpeg backend and RTSP capture timeout support. Keep the vendor publisher running on the motion host. The perception host reads its TCP RTSP stream on port 8554 at `/test`.

```bash
python3 src/launcher.py deploy ysc@192.168.1.103 --backend realsense --wide-angle-host 192.168.1.120
python3 src/launcher.py view ysc@192.168.1.103 --backend realsense --wide-angle-host 192.168.1.120
```

The server starts with the configured local camera. Select **Lite3 wide-angle camera** from **Source**. The preview uses the vendor stream's resolution and frame rate and the server's JPEG quality. If the motion host or stream is unavailable, the source stays visible with a reason. Use **Refresh** after the publisher recovers. If decoding fails after selection, use **Retry camera** or switch to another source.

The option is off by default and is independent of `--dog-host`. A new detached session started by `view` uses the supplied option. An existing session uses its current configuration. For the boot service, the option saved by `deploy` takes effect at the next boot or explicit service restart.

## Shared server lifecycle

The RealSense/V4L2 server trusts the LAN and listens on all IPv4 interfaces (`0.0.0.0`) by default. Once it is running, any device that can reach the perception host can open `http://192.168.1.103:8080/`. All native endpoints work without a token, URL fragment, browser session storage or Authorization header. Source selection uses POST with a JSON body; requests with an Origin header must match the current origin.

Alternatively, run `view` on each client. It forwards that computer's `127.0.0.1:18080` to the perception host's `127.0.0.1:8080`. With `--no-open`, open the printed URL manually. A control-capable server must report matching environment, simulation, control mode and R5 diagnostics settings before reuse. Every native listener must advertise the current camera and development protocol headers. If no server is listening, the first client starts a detached server. Camera selection and capture settings are shared. A reuse request keeps the server's current configuration. Use different `--local-port` values for multiple launcher processes on one computer. Clients on separate computers can all use the default local port.

Press Ctrl+C (or Ctrl+Break on Windows) to close just that client's tunnel. Other clients keep watching, and the native server remains running even with no viewers. Force-ending the launcher in Task Manager bypasses tunnel cleanup. ROS mode retains its foreground lifecycle: Ctrl+C also stops the ROS preview process it launched.

### Connect to an existing service

Use `view --existing-only` for a service whose lifecycle is managed separately, such as `lite3-dev-server.service`:

```bash
# Open the control console through the existing automatic SSH route.
python3 src/launcher.py view ysc@192.168.1.103 --existing-only --console

# Select a jump host and ports; stdout contains the URL for an SDK client.
python3 src/launcher.py view ysc@192.168.1.103 --existing-only --url-only \
  --jump ysc@192.168.2.1 --local-port 18081 --remote-port 8081
```

`--direct` uses the existing SSH configuration; `--jump` and automatic macOS routing work as usual. The launcher runs `ssh -N -T` with a loopback forward and verifies `/healthz` through it. Routing and authentication follow the SSH configuration; this mode uses a dedicated foreground connection so cleanup releases the forward. All remote service files, settings and processes remain under their existing management. The connection requires HTTP 200 or 503 with both `X-Lite3-Camera-Protocol: 1` and `X-Lite3-Dev-Protocol: 1`. A camera outage can return 503 while the console and control API remain usable. Redirects and incompatible protocol responses fail the check.

Connection setup and readiness have a 15-second timeout. Use `--wait-timeout 30` for a longer SSH login or service restart (valid range: 1–300 seconds). While the remote listener is unavailable, the launcher retries the HTTP check. Expiry or an SSH failure exits with a nonzero status and cleans up the local tunnel. A protocol mismatch fails immediately. Run the command again after resolving the reported problem.

This mode accepts connection options and rejects server launch options, including `--backend`, `--env`, `--simulate`, `--control-mode`, camera capture options and R5 settings. The running service's configuration determines the available controls. `--no-open` verifies readiness and prints the viewer URL. `--url-only` requires `--existing-only`, suppresses browser opening and prints one URL to stdout after verification; diagnostics go to stderr. The process stays running to maintain the tunnel, so read or copy the URL while it runs instead of waiting for command substitution to finish. `--console` selects `/control`; omit it when producing a base URL for `DevClient`.

`--dry-run` prints the SSH command and URL without connecting or verifying the listener. With `--url-only --dry-run`, the inspection command goes to stderr and the prospective URL goes to stdout. Ctrl+C closes only the local tunnel; the existing service continues running.

### Boot service

Each `deploy` installs `/etc/systemd/system/lite3-dev-server.service`, reloads systemd, and enables it for every boot. Installation needs a running systemd host and root or sudo access. The SSH terminal allows a sudo password prompt. The service runs as the deployment account, with its home directory, camera permissions, and the Python interpreter found during deployment. Dependencies must be installed for that interpreter and account; the service does not source interactive shell startup files.

The installation directory is `~/lite3-dev-server/`. When upgrading an older installation, `deploy` enables `lite3-dev-server.service`, then disables autostart for the legacy `lite3-camera-server.service` if it is installed. The legacy unit file, old `~/lite3-camera-server/` directory and running processes remain in place. The generated unit declares a systemd conflict with the legacy service. The launcher's restart command reports an active legacy service and waits for the operator to stop it explicitly. If a detached server was started from the old directory, inspect its PID file under `~/lite3-camera-server/.runtime/` and stop that process before starting the new service.

Review any systemd drop-in overrides for the legacy service and reapply the settings you need under `lite3-dev-server.service` before starting it. Drop-ins are tied to the unit name and stay with the legacy unit during deployment.

Deployment leaves current processes running. On first installation, the service starts at the next boot. On redeployment, the new code and boot settings apply at the next boot or explicit restart. Deploying again replaces the service's configured account, backend, port, capture options and control configuration. Include the desired environment and control flags on each deployment. The defaults are `--env dist` and `--control-mode hardware`. On startup, the native service opens the configured camera. Browser selections apply to the running process. ROS `web_video_server` has its own lifecycle.

For a stable V4L2 boot device, use the camera's confirmed RGB `/dev/v4l/by-id/NAME` link instead of a `/dev/videoN` number that can change after reboot:

```bash
# On the client; replace NAME with the host's actual RGB device link.
python3 src/launcher.py deploy ysc@192.168.1.103 --backend v4l2 --device /dev/v4l/by-id/NAME

# On the client:
python3 src/launcher.py service status ysc@192.168.1.103
python3 src/launcher.py service logs ysc@192.168.1.103 --lines 100
python3 src/launcher.py service restart ysc@192.168.1.103
```

`service status` shows loaded/enabled/active state, the main PID, unit path, drop-ins and startup command for both unit names. It also reports TCP listeners and their process commands, plus the HTTP build and environment. `service logs` prints the latest 50 journal entries from the current boot across both units; change this with `--lines`. The host needs `systemctl`, `journalctl`, `ss` (iproute2), and `ps`. The commands use the existing SSH routing, `--jump`, `--direct`, and `--dry-run`. Sudo uses the SSH terminal to read the journal and inspect all socket owners. On Windows, replace `python3` with `py -3`.

To use the service immediately, run `service restart`. It first checks for an active legacy unit and port conflicts, then requests a restart and waits up to 30 seconds for a fresh systemd process to own the port and serve `/api/status`. Set `--timeout SECONDS` to change the deadline. A service restart interrupts viewers and control sessions; existing pages reconnect at the same URL. For a non-default deployment port, supply the matching `--remote-port` to status and restart.

When a conflict is reported, inspect its PID, command and unit before deciding how to release it. Stop a legacy unit on the host with `sudo systemctl stop lite3-camera-server.service`. Use the detached-process instructions below for a server started by `view`. Run `service restart` again after resolving the conflict. The launcher leaves conflicting processes in place. A failed restart prints the systemd state, listener details and recent journal to help locate startup, permission or binding errors. The service's existing systemd retry policy continues after a verification timeout.

`deploy` prints `Deployed build: sha256:...`, computed from the uploaded runtime files and source provenance. To require that version, pass the full identifier to `service restart --expect-build sha256:...`. The server captures its `X-Lite3-Build` header at startup, so a running process retains its identity after a new deployment. Boot settings and device dependencies are separate from the code identifier and remain visible through status. An explicitly requested build that differs or is absent returns failure; older services without a build header can still be verified by omitting this option. Restart uses `/api/status`, so `--backend none` and temporary camera failures work even when `/healthz` returns 503.

Direct host commands remain available: `systemctl status lite3-dev-server.service`, `journalctl -u lite3-dev-server.service -b -f`, and `sudo systemctl restart lite3-dev-server.service`.

Use `view --existing-only` to connect during a service restart. It waits for the listener within `--wait-timeout` and leaves startup to systemd. Ordinary native `view` retains its detached fallback: wait for the service's HTTP listener before using that mode during a restart. If a detached process claims the port first, stop it so the service can bind on its next retry.

The service retries camera initialization every five seconds so a USB camera that becomes available after boot can recover. It also reopens V4L2 or wide-angle capture after a read failure. RealSense frame-read errors retain the existing SDK pipeline and retry frame reads. HTTP can remain available with `/healthz` returning 503 while capture is unavailable. A crashed server process is restarted by systemd. Use `systemctl is-active` to check the process and `/healthz` and the live image to check frame delivery.

Use `sudo systemctl stop lite3-dev-server.service` to stop it until the next boot, or `sudo systemctl disable --now lite3-dev-server.service` to stop it and disable autostart. Running `deploy` enables it again. A later ordinary native `view` can still start a detached server if the service is stopped; `view --existing-only` waits and fails when the listener remains unavailable. Service logs are in the journal; service processes do not use the launcher's `.runtime/*.pid` files.

### Detached and foreground servers

For servers started by `view`, background logs and PID files are under `~/lite3-dev-server/.runtime/` on the perception host. To release the camera or switch to the boot service, explicitly stop the detached server. This interrupts all its viewers. For the default port, run on the perception host:

```bash
cat ~/lite3-dev-server/.runtime/8080.pid
# Inspect that PID and confirm it is this dev server before sending SIGTERM:
ps -p <PID> -o pid,args
kill -TERM <PID>
```

Use the PID and log filenames matching `--remote-port`. A manually launched server has no launcher PID file; stop it in its own terminal. After the process exits, run `deploy` if updating code, then start the service or run `view` again with the desired options. Reload browser pages after an update. Native `view` requires both current protocol headers and refuses to reuse or replace a listener that fails the check.

For a foreground server, run `python3 ~/lite3-dev-server/server.py --source realsense` on the host. Add `--bind 127.0.0.1` to restrict it to SSH tunnel access, or bind to a particular LAN address. Its terminal owns the server lifetime; stopping it disconnects all viewers.

## Find faults

The RealSense and V4L2 pages check the incoming video data. If the stream ends, the page hides the last frame and reconnects. The page does the same if no decodable frame arrives for approximately 2.5 seconds. Camera resolution depends on the selected device and options.

| Symptom | Checks and actions |
| --- | --- |
| SSH cannot connect | Check the computer's network and the perception host's IP address. Check access to the SSH port. On Windows with two network links, try the explicit `--jump` described above. |
| Windows cannot find Python, ssh, or scp | Use `py -3` or a Python 3 executable on PATH. Install Windows OpenSSH Client and reopen PowerShell. |
| A port is in use | For the local port, use `--local-port 18081`. For the perception host's port, use `--remote-port 8081`. |
| The ROS page has no image or keeps the same frame | Check that `--ros-setup` matches the camera node's environment. In that environment, use `rostopic list` and `rostopic hz`, or `ros2 topic list` and `ros2 topic hz`. Confirm that the image topic receives new frames. |
| RealSense reports a busy device | Check for a ROS camera node or `realsense-viewer` that uses the camera. If ROS publishes images, use ROS mode. |
| RealSense Python bindings are missing | Use `doctor` to check the Python version and missing modules. The bindings must match the host architecture, Python version, and vendor SDK. |
| V4L2 shows no image | Confirm that `--device` is an RGB node and no other program uses it. Check that OpenCV supports V4L2. Select a resolution and frame rate that the device supports. |
| V4L2 ignores requested settings or stops capture | OpenCV requests settings, but the device can select other values. Check the actual resolution and frame rate. After a capture failure, correct device conflicts or connection faults. Then use **Retry camera**, or stop the shared server and run `view` again. |
| The local page returns HTTP 502 | Check whether the browser proxy intercepts `127.0.0.1`. The launcher's readiness check bypasses proxies. Use `curl --noproxy '*' http://127.0.0.1:18080/` to check the page. Alternatively, use `--no-proxy-server` only in a temporary Chrome test session. Keep the system proxy settings unchanged. |
| The launcher reports missing or unsupported protocol headers | Check that the port belongs to this service. Stop it, deploy the current version, then run `view` again. |
| Video is delayed or stops briefly | For RealSense or V4L2, restart the shared server with `--quality 55` or a lower supported resolution or frame rate. You can also repeat the test on the vendor's specified wired network. For both backends, `/healthz` shows whether the server received a recent frame. |

Offline tests use simulated camera input to check the browser and video server. Hardware checks covered D435i/SXW source switching and restoration of the false-color depth preview. Distance calibration, USB unplug recovery, long-duration operation and video delay require separate tests.
