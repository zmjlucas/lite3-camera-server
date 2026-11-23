# View the Lite3 camera in a Mac browser

The Mac connects to the perception host through SSH. The browser shows the camera stream through this connection. The Mac needs Python 3.8 or later, OpenSSH, and a browser. The camera commands do not send motion commands.

Run these commands from the repository root. The examples use the vendor's default perception host, `ysc@192.168.1.103`. Replace this account and IP address with the values for your robot.

## Run the local demo

```bash
python3 scripts/lite3_camera.py demo
```

The browser opens `http://127.0.0.1:18080/` and shows synthetic frames that change. To open the address manually, add `--no-open`. To stop the demo, press Ctrl+C in the terminal.

The demo uses a fixed resolution of 160 x 120 pixels to check video playback. Camera resolution depends on the selected device and options. The RealSense, V4L2, and demo pages check the incoming video data. If the stream ends, the page hides the last frame and reconnects. The page does the same if no decodable frame arrives for approximately 2.5 seconds.

## Connect to the robot

1. Connect the Mac to the robot's Wi-Fi or the specified wired network.
2. Before the first SSH connection, check the host key.
3. Check the camera interfaces on the perception host:

```bash
python3 scripts/lite3_camera.py doctor ysc@192.168.1.103
```

The `doctor` command reads system details, Python dependencies, ROS environments, camera service status, and video device information. It does not open a camera. To inspect the route and commands, add `--dry-run` to `doctor`, `deploy`, or `view`. This option reads local routes and prints the selected path and commands. It does not log in to or probe the robot.

### Select routes for robot Wi-Fi and phone USB networks

Use the same commands with robot Wi-Fi alone or with robot Wi-Fi and a phone USB hotspot. The launcher selects the connection path. You do not need to add or remove `--jump` for these two network conditions.

```bash
python3 scripts/lite3_camera.py doctor ysc@192.168.1.103
python3 scripts/lite3_camera.py view ysc@192.168.1.103
# Deploy only when you need the RealSense or V4L2 server described below.
python3 scripts/lite3_camera.py deploy ysc@192.168.1.103
# Inspect the selected route without a robot connection.
python3 scripts/lite3_camera.py view ysc@192.168.1.103 --dry-run
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

Use a manual option for another network layout, a preferred wired interface, or a different gateway account:

```bash
python3 scripts/lite3_camera.py view ysc@192.168.1.103 --jump otheruser@192.168.2.1
python3 scripts/lite3_camera.py view ysc@192.168.1.103 --direct
```

The `--jump` and `--direct` options both bypass automatic selection. You cannot use them together. The `--direct` option adds no automatic jump but still uses proxies from the existing SSH configuration. The `--jump` option accepts one SSH alias, hostname, or `user@IPv4` address. It uses OpenSSH `-J` for SSH and SCP.

The jump host only forwards the SSH connection. Camera commands still run on the perception host. The launcher does not start a motion controller. Both hosts require authentication and host key checks. The launcher does not save passwords or change routes or SSH configuration.

The launcher selects the route each time a command starts. After a USB or Wi-Fi connection change, stop the preview. Then run the command again. Offline route samples cover both network conditions. Each condition still requires a test on the real network.

### Use an existing ROS image topic

Use an active RGB image topic when one is available. The perception host must run a camera publisher node. Its ROS environment must contain `web_video_server`. The `view` command starts the preview service and forwards its port. The browser page lists the available image topics.

```bash
python3 scripts/lite3_camera.py view ysc@192.168.1.103
```

If the host has multiple ROS environments, use `--ros-setup` to select the camera node's environment. For example, use `--ros-setup /opt/ros/melodic/setup.bash`. You can also select a workspace `setup.bash` that provides the required packages. The launcher uses `ROS_VERSION` to select ROS 1 or ROS 2. After you find the image topic, use `--topic /camera/image_topic` with its actual name.

ROS 1 uses a separate node name for each preview to prevent conflicts with other previews. ROS 2 requests the `sensor_data` subscription profile to support cameras that use best-effort publishing. This requires a `web_video_server` build with `default_qos_profile` support. If an older build shows no image, compare its subscription QoS with the camera's publishing QoS. A topic in the list does not confirm that the preview works.

ROS mode uses the page from `web_video_server`. That page can keep the last frame when the publisher stops. Use `rostopic hz` or `ros2 topic hz` to check whether new frames arrive. This repository's stream failure messages and automatic frame hiding apply to RealSense, V4L2, and the local demo.

If `web_video_server` is missing, use this procedure on the perception host:

1. Load the ROS environment that matches the camera node.
2. Check the configured package sources with `apt-cache policy "ros-${ROS_DISTRO}-web-video-server"`.
3. If a candidate version is available, install it with `sudo apt install "ros-${ROS_DISTRO}-web-video-server"`.

Older environments, such as Melodic or Foxy, might have no candidate package. In that case, use a build that matches the vendor system or the RealSense method below. Dependencies in newer upstream code can exceed what an older system image supports.

### Read RealSense directly

You can read RGB frames directly when the camera is not in use. Python 3 on the perception host must import `pyrealsense2`, `numpy`, and `cv2`. The vendor manual states that the system image includes native librealsense. Use `doctor` to check whether the Python bindings are available.

```bash
python3 scripts/lite3_camera.py deploy ysc@192.168.1.103
python3 scripts/lite3_camera.py view ysc@192.168.1.103 --backend realsense
```

The `deploy` command uploads the server script, viewer page, and demo assets to `~/robo-comp-camera/` on the perception host. Defaults are 640 x 480 pixels, 15 fps, and JPEG quality 70. To change these values, use `--width`, `--height`, `--fps`, and `--quality`. If multiple cameras are connected, use `--serial` to select one.

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

From the Mac, run these commands in order:

```bash
python3 scripts/lite3_camera.py deploy ysc@192.168.1.103
python3 scripts/lite3_camera.py view ysc@192.168.1.103 --backend v4l2 --device /dev/video4
```

The launcher selects the connection path with the rules above. Set `--device` to a confirmed `/dev/videoN` or `/dev/v4l/by-id/name` path. Device numbers can change after a restart or a USB connection change. V4L2 mode provides RGB only. It does not support `--depth` or `--serial`.

Use `--width`, `--height`, `--fps`, and `--quality` to request settings that the device supports. Check the actual image resolution and frame rate after the stream starts.

## Stop the preview and find faults

By default, `view` forwards the Mac's `127.0.0.1:18080` to the perception host's `127.0.0.1:8080` through SSH. To open the printed URL manually, add `--no-open`. To stop the preview service and tunnel, press Ctrl+C in the terminal that runs `view`.

| Symptom | Checks and actions |
| --- | --- |
| SSH cannot connect | Check the Mac's network and the perception host's IP address. Check access to the SSH port. |
| A port is in use | For the local port, use `--local-port 18081`. For the perception host's port, use `--remote-port 8081`. |
| The ROS page has no image or keeps the same frame | Check that `--ros-setup` matches the camera node's environment. In that environment, use `rostopic list` and `rostopic hz`, or `ros2 topic list` and `ros2 topic hz`. Confirm that the image topic receives new frames. |
| RealSense reports a busy device | Check for a ROS camera node or `realsense-viewer` that uses the camera. If ROS publishes images, use ROS mode. |
| RealSense Python bindings are missing | Use `doctor` to check the Python version and missing modules. The bindings must match the host architecture, Python version, and vendor SDK. |
| V4L2 shows no image | Confirm that `--device` is an RGB node and no other program uses it. Check that OpenCV supports V4L2. Select a resolution and frame rate that the device supports. |
| V4L2 ignores requested settings or stops capture | OpenCV requests settings, but the device can select other values. Check the actual resolution and frame rate. After a capture failure, correct device conflicts or connection faults. Then run `view` again. |
| The local page returns HTTP 502 | Check whether the browser proxy intercepts `127.0.0.1`. The launcher's readiness check bypasses proxies. Use `curl --noproxy '*' http://127.0.0.1:18080/healthz` to check the server. Alternatively, use `--no-proxy-server` only in a temporary Chrome test session. Keep the system proxy settings unchanged. |
| Video is delayed or stops briefly | For RealSense or V4L2, try `--quality 55` or a lower supported resolution or frame rate. You can also repeat the test on the vendor's specified wired network. For RealSense, V4L2, and the demo, `/healthz` shows whether the server received a recent frame. |

The local demo checks only the browser and video server on the Mac. Depth preview and video delay require separate tests.

## Offline test record: 2026-11-20

On that date, the Mac passed 32 unit tests and a separate Chrome test. Chrome decoded and displayed JPEG frames. It hid the last frame and reconnected after a normal stream end and a data transfer timeout. The tests covered jump hosts, V4L2 arguments, and readiness checks that bypass local proxies.

The server passed a Python 3.6 syntax check. The launcher passed a Python 3.8 syntax check. The offline tests ran with Python 3.13 on the Mac.

```bash
PYTHONDONTWRITEBYTECODE=1 python3 -m unittest discover -s tests -v
# Optional: use a temporary Chrome profile for the browser test.
# The test does not use your existing profile or connect to a robot.
PYTHONDONTWRITEBYTECODE=1 python3 tests/camera_browser_smoke.py
```

The browser test requires Chrome or another Chromium browser. To select a browser, use `--browser /absolute/path/to/browser`. The test uses a random local port and can run at the same time as the demo.

Vendor references: [network configuration](https://developer.deeprobotics.cn/docs/lite3/guide/software/network/), [camera interface](https://developer.deeprobotics.cn/docs/lite3/interfaces/device/camera/), and [camera example](https://developer.deeprobotics.cn/docs/lite3/examples/software/camera-example/). See [web_video_server](https://github.com/RobotWebTools/web_video_server) for ROS preview and snapshot parameters.
