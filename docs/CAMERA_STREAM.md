# View the Lite3 camera from Windows or macOS

The computer connects to the Linux perception host through SSH. The browser shows the camera stream through this connection. The Windows or macOS client needs Python 3.8 or later, OpenSSH (`ssh` and `scp`), and a browser. The camera commands do not send motion commands. Camera drivers, ROS and Bash run on the perception host.

Run these commands from the repository root. Account names and addresses in examples are placeholders, not a record of any deployed robot. Replace them with the values for your robot.

Examples below use `python3` for macOS/Linux. In Windows PowerShell, replace it with `py -3` or your installed `python` command. No local Bash or WSL is required. Confirm `Get-Command ssh, scp` succeeds; otherwise install **OpenSSH Client** in Windows Optional Features and reopen the terminal. See [Microsoft's instructions](https://learn.microsoft.com/en-us/windows-server/administration/openssh/openssh_install_firstuse).

```powershell
py -3 src/client.py doctor user@192.168.1.100
py -3 src/client.py deploy user@192.168.1.100
py -3 src/client.py view user@192.168.1.100 --backend realsense
```

Deployment refuses symbolic links in source paths and, on Windows, all reparse points including junctions. Use a normal local checkout if a redirected or cloud-managed folder is rejected. Capture files are read before SSH login and only allowlisted runtime assets are uploaded. The service now also provides the [development control API and console](DEV_SERVER.md); camera endpoints remain compatible.

## Connect to the robot

1. Connect the computer to the robot's Wi-Fi or the specified wired network.
2. Before the first SSH connection, check the host key.
3. Check the camera interfaces on the perception host:

```bash
python3 src/client.py doctor user@192.168.1.100
```

The `doctor` command reads system details, Python dependencies, ROS environments, camera service status, and video device information. It does not open a camera. To inspect the route and commands, add `--dry-run` to `doctor`, `deploy`, or `view`. This option reads local routes and prints the selected path and commands. It does not log in to or probe the robot.

Windows dry-run output shows exact command arguments as JSON for inspection; it is not a PowerShell script to paste and execute. Run the launcher without `--dry-run` to let Python pass the arguments directly to OpenSSH, including embedded quotes and newlines.

### Select routes for robot Wi-Fi and phone USB networks

On macOS, use the same commands with robot Wi-Fi alone or with robot Wi-Fi and a phone USB hotspot. The launcher selects the connection path. You do not need to add or remove `--jump` for these two network conditions.

On Windows, the launcher uses your existing SSH configuration and system routes. With a phone hotspot and robot Wi-Fi connected together, select the robot gateway with `--jump` if the direct target route goes through the phone. Apply this option to all three commands:

```powershell
py -3 src/client.py doctor user@192.168.1.100 --jump user@192.168.2.1
py -3 src/client.py deploy user@192.168.1.100 --jump user@192.168.2.1
py -3 src/client.py view user@192.168.1.100 --backend realsense --jump user@192.168.2.1
```

The automatic selection details below apply to macOS only.

```bash
python3 src/client.py doctor user@192.168.1.100
python3 src/client.py view user@192.168.1.100
# Deploy only when you need the RealSense or V4L2 server described below.
python3 src/client.py deploy user@192.168.1.100
# Inspect the selected route without a robot connection.
python3 src/client.py view user@192.168.1.100 --dry-run
```

The launcher uses macOS `route -n get` to read routes to the perception host and robot gateway. It uses `networksetup -listallhardwareports` to find Wi-Fi hardware interfaces. Selection does not depend on the phone model or USB interface number.

| Current route | Selected path |
| --- | --- |
| The target uses a direct robot Wi-Fi link or the Wi-Fi gateway `192.168.2.1`. | Connect directly to the target through SSH. |
| The target uses another interface or gateway, and `192.168.2.1` has a direct Wi-Fi link. | Add `-J user@192.168.2.1`. Use robot Wi-Fi when the phone and robot subnets overlap. |
| The target route is missing or cannot be read, but the robot gateway has a confirmed direct Wi-Fi link. | Connect through the confirmed robot gateway. |
| No direct Wi-Fi link to the robot gateway is confirmed, or interface information is missing. | Use the existing direct SSH command. If the target also lacks a direct link, show the reason and manual options. |

The terminal's `Route:` message shows the selected path and related interface or gateway. This message reports a local route decision. It does not confirm SSH connectivity. The `deploy` command uses the same route decision for SSH and SCP. An automatic jump uses the username in the target address. If the target has no username, OpenSSH selects one.

Automatic selection requires macOS and an explicit `192.168.1.*` IPv4 target. Automatic jump selection also requires the default robot gateway, `192.168.2.1`, on a direct Wi-Fi link. The launcher does not identify devices by Wi-Fi name or discover the perception host's IP address. SSH aliases, hostnames, other subnets, and other operating systems use the existing SSH configuration.

Use a manual option for another network layout, a preferred wired interface, or a different gateway account:

```bash
python3 src/client.py view user@192.168.1.100 --jump otheruser@192.168.2.1
python3 src/client.py view user@192.168.1.100 --direct
```

The `--jump` and `--direct` options both bypass automatic selection. You cannot use them together. The `--direct` option adds no automatic jump but still uses proxies from the existing SSH configuration. The `--jump` option accepts one SSH alias, hostname, or `user@IPv4` address. It uses OpenSSH `-J` for SSH and SCP.

The jump host only forwards the SSH connection. Camera commands still run on the perception host. Control defaults to simulation; hardware communication requires the explicit settings in the [Dev Server guide](DEV_SERVER.md). Both hosts require authentication and host key checks. The launcher does not save passwords or change routes or SSH configuration.

The launcher selects the route each time a command starts. After a USB or Wi-Fi connection change, stop the preview. Then run the command again. Offline route samples cover both network conditions. Each condition still requires a test on the real network.

### Use an existing ROS image topic

Use an active RGB image topic when one is available. The perception host must run a camera publisher node. Its ROS environment must contain `web_video_server`. The `view` command starts the preview service and forwards its port. The browser page lists the available image topics.

```bash
python3 src/client.py view user@192.168.1.100
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
python3 src/client.py deploy user@192.168.1.100
python3 src/client.py view user@192.168.1.100 --backend realsense
```

The `deploy` command uploads the server, control modules, fixed arm helper and viewer pages to `~/lite3-camera-server/` and its `web/` and `remote/` subdirectories on the perception host, then runs a remote installer to enable the [systemd boot service](#boot-service). It rejects symlinked source paths and does not copy unrelated files or Git-ignored content. Defaults are 640 x 480 pixels, 15 fps, and JPEG quality 70. Use `deploy --width`, `--height`, `--fps`, and `--quality` to save boot settings. If multiple RealSense cameras are connected, use `deploy --serial` to select one. Matching options on `view` apply only if it starts a new detached server.

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
python3 src/client.py deploy user@192.168.1.100 --backend v4l2 --device /dev/video4
python3 src/client.py view user@192.168.1.100 --backend v4l2 --device /dev/video4
```

The launcher selects the connection path with the rules above. Set `--device` to a confirmed `/dev/videoN` or `/dev/v4l/by-id/name` path. Device numbers can change after a restart or a USB connection change. V4L2 mode provides RGB only. It does not support `--depth` or `--serial`.

Use `--width`, `--height`, `--fps`, and `--quality` to request settings that the device supports. Check the actual image resolution and frame rate after the stream starts.

### Switch between the D435i and arm camera

After deploying the current version, start the RealSense or V4L2 viewer with either command above. Use the **Source** dropdown to select a detected camera. Entries show the current device node and camera name, such as `/dev/video4 — D435i RGB` or `/dev/video6 — Arm camera (SXW USB Camera)`. These numbers are examples; the server detects the current numbering. Use **Refresh** to scan again after connecting a camera.

Only supported D435i RGB and SXW USB Camera capture nodes are offered. Auxiliary metadata, depth, infrared and unrelated camera nodes are excluded. Identifiable RGB ports remain in the dropdown when unavailable, with disabled options and the reason shown both in the option and below the controls. For example, a camera held by `realsense-viewer` shows the owning process when it is visible to the server. Release the camera in that application, or correct the reported connection/permission problem, then use **Refresh** to enable the option again. Rejecting an unavailable selection leaves the current capture running.

Discovery queries capabilities and reads Linux process descriptors without starting capture or stopping other applications. The server ignores its own descriptors so shared viewers can keep using its capture. An external handle on a camera's video interfaces or USB device is conservatively treated as in use. If capability probing fails, sysfs must identify the RGB capture interface; unidentified nodes stay excluded. Process inspection depends on Linux permissions, and another application can acquire a camera after a refresh, so capture may still report a later error.

One camera streams at a time. Selecting another source clears the old image and snapshot, stops the previous capture, and starts the selected camera. The HTTP server and SSH tunnels stay open, so viewer URLs remain valid. Selection affects every viewer connected to this server. If a driver blocks during capture shutdown, the server refuses to open a second camera; stop and restart the shared server after resolving the device problem.

The SXW arm camera uses V4L2 with MJPG input. A V4L2 session uses YUYV for D435i RGB. A session launched with `--backend realsense` uses the RealSense SDK for D435i and restores its original `--depth` setting when switching back from the arm camera. Depth preview is not available on SXW. The dropdown applies to this server's RealSense/V4L2 viewer; ROS mode continues to use the external topic viewer.

If a selection fails, read the source error and check the connection, permissions, camera usage, and requested resolution/frame rate. Use **Retry camera** to restart the selected camera after a capture failure, or refresh the list and select another camera. Explicit `--device` paths remain supported for starting V4L2; the web selection API accepts only IDs from the detected catalog, not arbitrary device paths.

## Shared server lifecycle

The RealSense/V4L2 server trusts the LAN and listens on all IPv4 interfaces (`0.0.0.0`) by default. Once it is running, any device that can reach the perception host can open `http://PERCEPTION_HOST:8080/`. All native endpoints work without a token, URL fragment, browser session storage or Authorization header. Source selection uses POST with a JSON body; requests with an Origin header must match the current origin.

Alternatively, run `view` on each client. It forwards that computer's `127.0.0.1:18080` to the perception host's `127.0.0.1:8080`. With `--no-open`, open the printed URL manually. Native clients reuse a listening boot service or detached server. If no server is listening, the first client starts a detached server. Camera selection and capture settings are shared. New `view` arguments do not reconfigure an existing server. Use different `--local-port` values for multiple launcher processes on one computer; clients on separate computers can all use the default local port.

Press Ctrl+C (or Ctrl+Break on Windows) to close just that client's tunnel. Other clients keep watching, and the native server remains running even with no viewers. Force-ending the launcher in Task Manager bypasses tunnel cleanup. ROS mode retains its upstream authentication and foreground lifecycle: Ctrl+C also stops the ROS preview process it launched.

### Boot service

Each `deploy` installs `/etc/systemd/system/lite3-camera-server.service`, reloads systemd, and enables it for every boot. Installation needs a running systemd host and root or sudo access. The SSH terminal allows a sudo password prompt. The service runs as the deployment account, with its home directory, camera permissions, and the Python interpreter found during deployment. Dependencies must be installed for that interpreter and account; the service does not source interactive shell startup files.

Deployment leaves current processes running. On first installation, the service starts at the next boot; on redeployment, the new code and boot settings apply at the next boot or explicit restart. There is one service per host: deploying again replaces its configured account, backend, port, capture options and control configuration. Include the desired control flags on each deployment; omitted flags return to the default simulation mode. It does not remember camera selections made in the browser. ROS `web_video_server` is not managed by this service.

For a stable V4L2 boot device, use the camera's confirmed RGB `/dev/v4l/by-id/NAME` link instead of a `/dev/videoN` number that can change after reboot:

```bash
# On the client; replace NAME with the host's actual RGB device link.
python3 src/client.py deploy user@192.168.1.100 --backend v4l2 --device /dev/v4l/by-id/NAME

# On the perception host:
systemctl is-enabled lite3-camera-server.service
systemctl status lite3-camera-server.service
journalctl -u lite3-camera-server.service -b -f
```

To use the service immediately, first stop any detached or foreground camera server using the instructions below, then run `sudo systemctl restart lite3-camera-server.service`. Do not run both processes against the same camera or port. Subsequent service restarts use the same command. A service restart interrupts all viewers; existing pages reconnect at the same URL.

Wait for the service's HTTP listener before launching a new `view` during a restart. If `view` starts its detached fallback first, stop that detached process so the service can claim the port on its next retry.

The service retries camera initialization every five seconds so a USB camera that becomes available after boot can recover. It also reopens V4L2 capture after a read failure; RealSense frame-read errors retain the existing SDK pipeline and retry frame reads. HTTP can remain available with `/healthz` returning 503 while capture is unavailable. A crashed server process is restarted by systemd. `systemctl is-active` checks the process, not whether frames are arriving; check `/healthz` and the live image too. Delayed USB and actual robot reboot behavior still need hardware acceptance.

Use `sudo systemctl stop lite3-camera-server.service` to stop it until the next boot, or `sudo systemctl disable --now lite3-camera-server.service` to stop it and disable autostart. Running `deploy` enables it again. A later `view` can still start a detached server if the service is stopped. Service logs are in the journal; service processes do not use the launcher's `.runtime/*.pid` files.

### Detached and foreground servers

For servers started by `view`, background logs and PID files are under `~/lite3-camera-server/.runtime/` on the perception host. To release the camera or switch to the boot service, explicitly stop the detached server. This interrupts all its viewers. For the default port, run on the perception host:

```bash
cat ~/lite3-camera-server/.runtime/8080.pid
# Inspect that PID and confirm it is this camera server before sending SIGTERM:
ps -p <PID> -o pid,args
kill -TERM <PID>
```

Use the PID and log filenames matching `--remote-port`. A manually launched server has no launcher PID file; stop it in its own terminal. After the old process exits, run `deploy` if updating code, then start the service or run `view` again with the desired options. Existing pages reconnect at the same URL. If upgrading from a version that requires session tokens, stop the old foreground server before starting the new one. An incompatible listener is never silently replaced.

For a foreground server, run `python3 ~/lite3-camera-server/server.py --source realsense` on the host. Add `--bind 127.0.0.1` to restrict it to SSH tunnel access, or bind to a particular LAN address. Its terminal owns the server lifetime; stopping it disconnects all viewers.

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
| An API returns HTTP 401 or the launcher reports an incompatible server | Stop the old server, deploy the current version, then run `view` again. Check that the port belongs to this camera service. |
| Video is delayed or stops briefly | For RealSense or V4L2, restart the shared server with `--quality 55` or a lower supported resolution or frame rate. You can also repeat the test on the vendor's specified wired network. For both backends, `/healthz` shows whether the server received a recent frame. |

Offline tests use simulated camera input to check the browser and video server. Hardware checks covered D435i/SXW source switching and restoration of the false-color depth preview. Distance calibration, USB unplug recovery, long-duration operation and video delay require separate tests.
