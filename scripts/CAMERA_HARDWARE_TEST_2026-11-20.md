# Lite3 camera hardware check: 2026-11-20

This check covered the camera, network, and video display. It did not run motion scripts or send motion, stand, gait, or arm commands. It did not start, stop, or restart vendor control services. It did not upgrade the system or install dependencies.

## Connection and environment

- The Mac used `192.168.2.47` on the robot's Wi-Fi. An iPhone USB network was also active. Direct traffic to `192.168.1.*` used the USB default gateway.
- SSH connected through `ysc@192.168.2.1` to the perception host at `ysc@192.168.1.103`. The check did not change Mac routes.
- The perception host was a Jetson Xavier NX with aarch64, Ubuntu 20.04.6, and Python 3.8.10.
- The host already had numpy 1.23.1, OpenCV 4.9.0, FFmpeg, v4l2-ctl, and the native RealSense SDK.
- Python had no `pyrealsense2`. Neither ROS Foxy nor Noetic had `web_video_server`. Foxy listed a point cloud topic but no usable RGB image topic. The Noetic ROS master was unreachable.
- The vendor services `realsense` and `realsense_ros2` were both `inactive`. Before capture, `fuser` found no process that used the video devices.
- RealSense tools identified a D435i with firmware `05.13.00.50`. The `v4l2-ctl` output confirmed that `/dev/video4` was the RGB node. It supported YUYV at 640 x 480 pixels and 15 fps. This node number applies only to the device check on that date.

## Measured results

1. OpenCV captured 30 frames from the confirmed RGB node. Each image had a shape of 640 x 480 x 3. Capture took 2.552 seconds, including startup. The driver reported 15 fps. The capture process then released the device.
2. The session deployed only `~/robo-comp-camera/lite3_camera_server.py`. It used `--source v4l2 --device /dev/video4 --width 640 --height 480 --fps 15 --quality 70`. The HTTP server listened only on the perception host's loopback address, `127.0.0.1:8080`. SSH forwarded this port to `127.0.0.1:18080` on the Mac.
3. Three health checks returned `source=v4l2, status=live`. Frame age samples were 0.027, 0.049, and 0.001 seconds. Frame age measures the time since capture at the server. It does not measure video delay from the camera to the browser.
4. The Mac received 122 frames during 8.055 seconds. All 122 JPEG images had different content. The receive rate was approximately 15.15 fps. The mean JPEG size was 31,532 bytes. FFprobe decoded the images and confirmed a resolution of 640 x 480 pixels.
5. Local Chrome displayed the live camera image with status `Live`. Ten samples over two seconds each showed a different rendered frame. The session also opened the preview URL in the user's Chrome browser.
6. After the preview stopped, `fuser /dev/video4` found no process that used the device. A connection to remote port 8080 returned `connection refused`. Both vendor camera services remained `inactive`. These results confirmed device release and preview shutdown for this session.
7. The implementation added `--jump`, the V4L2 backend, and local HTTP checks that bypass proxies. All 32 offline regression tests passed. The updated proxy check also read the live camera health response on the Mac.

The session did not use NoMachine. It used V4L2 RGB. It did not verify depth preview, recovery after physical USB disconnection, extended Wi-Fi operation, or video delay.

## Repeat the procedure

Run this command from the repository root. Complete SSH authentication when the terminal requests it.

```bash
python3 scripts/lite3_camera.py view ysc@192.168.1.103 \
  --jump ysc@192.168.2.1 \
  --backend v4l2 --device /dev/video4
```

The session deployed the server before this check. If you change the device or server code, first run:

```bash
python3 scripts/lite3_camera.py deploy ysc@192.168.1.103 --jump ysc@192.168.2.1
```

If the test preview still uses port 18080, stop it before you run these commands. For a preview that uses the launcher, press Ctrl+C in its terminal to stop it.

The assistant left a temporary SSH preview connection open for the browser during this session. Closing the browser tab stops playback only. To stop that camera service and tunnel, run this command on the Mac. This command applies only to the temporary connection from this session.

```bash
ssh -S /private/tmp/lite3-camera-perception-20261120.sock -O exit ysc@192.168.1.103
```

The Mac's system web proxy caused Python's default HTTP client to return HTTP 502 for loopback requests. The launcher now bypasses proxies for local readiness checks. It does not change the system proxy settings.
