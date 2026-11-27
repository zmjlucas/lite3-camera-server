# Lite3 Dev Server

Dev Server centralizes camera streaming, Lite3 UDP commands and R5 serial feedback. Scripts and the browser call one HTTP service; SSH is used for deployment, tunnels and an optional persistent arm helper. No caller can upload arbitrary programs or send arbitrary robot packets through this API.

The camera viewer remains at `/`; the control console is `/control`. Existing deployment directories and `lite3-camera-server.service` remain in use so an upgrade does not create a competing camera owner. ROS mode still uses the external camera-only `web_video_server`.

## Default robot connection

| Setting | Default |
| --- | --- |
| Username | `ysc` |
| Password | `'` (one ASCII single quote) |
| Perception host | `192.168.1.103` |
| Motion control (UDP) | `192.168.1.120:43893` |

Use `ysc@192.168.1.103` for SSH. Enter `'` at SSH or sudo password prompts. The camera page is `http://192.168.1.103:8080/`, and the control console is `http://192.168.1.103:8080/control`. All dev server endpoints are available without authentication. The server assumes a secure, trusted network with no public internet access. Do not add authentication to the dev server.

## Run locally in simulation

Python 3.8+ is recommended for development. No camera, SSH, serial package or robot is needed:

```bash
python3 src/server.py --source none --bind 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/control` and acquire control. Simulation is the default and never opens a motion UDP socket, SSH session or serial port. The console and SDK can use the API as soon as the server is ready.

The console provides camera snapshots plus a link to the full streaming/source-selection viewer. It distinguishes simulated targets, sent commands and observed arm feedback. Camera failure does not disable the control API.

## Deploy on the perception host

Existing camera commands remain valid. New deployment captures and uploads the server, control modules, fixed arm helper and both pages. It installs/enables the existing system service without interrupting the running process.

Deploy from the checkout:

```bash
python3 src/client.py deploy ysc@192.168.1.103 \
  --backend v4l2 --device /dev/v4l/by-id/CONFIRMED_RGB_CAMERA \
  --control-mode simulation
```

Use the confirmed camera path in place of the example. Use `--backend none` for a service without camera capture. Deployment writes the current launch options to the service unit. For an existing installation, deploy again and check local systemd overrides for obsolete options. Once ready to replace the current process, use the existing service lifecycle:

```bash
sudo systemctl restart lite3-camera-server.service
journalctl -u lite3-camera-server.service -b
```

A manually started detached server must first be stopped using the existing [camera lifecycle guide](CAMERA_STREAM.md#shared-server-lifecycle); do not start two instances against the same hardware. The new code does not migrate a live process automatically.

From the development computer:

```bash
python3 src/client.py view ysc@192.168.1.103 --backend v4l2 \
  --device /dev/v4l/by-id/CONFIRMED_RGB_CAMERA --console
```

Use `--jump ysc@192.168.2.1` if needed, or existing automatic macOS route selection. This opens the console through the local SSH tunnel. Closing this tunnel does not stop the shared server; motion lease/command expiry still applies. `view` reuses existing camera **and control** settings; changing launch options never reconfigures a running service. `--console` or control flags refuse an old camera-only listener and request an explicit upgrade/restart.

## Hardware capabilities

For deployment acceptance with **no motion output**, use `--control-mode readonly`. This mode provides real camera streams and explicit R5 feedback reads, but rejects every control/ownership/motion action and never creates a dog UDP socket, including on shutdown. Check `/api/status` reports `readonly` after explicitly restarting the service; a reused server keeps its old settings. Serial opening can still affect a controller's reset lines, so use the verified DTR/RTS configuration and an unoccupied device. The service needs access to the serial device group (on Ubuntu, a service-only `SupplementaryGroups=dialout` drop-in can provide it without changing the login account).

Real dog output requires `--control-mode hardware`. The default motion target is `192.168.1.120:43893`; use `--dog-host 192.168.1.120` to set the same host explicitly. The default serial device is `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`; `--arm-device` selects a confirmed serial node. The service account needs serial permissions and pySerial; the service does not run interactive sudo.

R5 is attached by USB to the perception host. Normally the server owns serial locally, avoiding per-command SSH. If the server runs on another Linux host and needs remote serial access to the perception host, configure `--arm-host ysc@192.168.1.103` with verified OpenSSH configuration/known hosts and noninteractive authentication. One fixed persistent helper handles serialized queries. Opening serial is explicit via arm state retrieval; no background scan or automatic reconnect opens it. A failed session requires explicit `arm/reconnect` before another read. DTR/RTS are cleared before opening; opening may still reset a controller, so confirm its behavior on the device. Exclusive serial locking does not prevent unrelated software that ignores locks.

**Hardware R5 motion is disabled.** Hardware jog fails before opening SSH or serial, with no override switch. Read-only T105 feedback is available; simulation supports bounded jogs.

The protocol sources are the existing `robo-comp/scripts/lite3_stand_walk.py`, `r5_rotate_180.py`, `r5_elbow_down_ssh.py`, and the local Lite3/R5 vendor manuals. The R5 helper sends only `{"T":105}` and validates `T=1051` replies. `b/s/e/w` feedback is converted from radians to degrees under the observed device profile. The gripper has no reliable position feedback: it is returned as unknown, never replaced with a cached target. These ranges and mappings do not establish collision avoidance.

## Control lifecycle and limits

1. Acquire the sole control lease. It expires after two seconds without heartbeat.
2. Explicitly prepare the dog using `dog/stand`. It sends the stock explicit stand sequence and waits about 5.6 seconds while the lease is renewed. No telemetry confirms that the dog actually stood.
3. Send bounded velocities with increasing sequence numbers and a short TTL (at most 0.5 seconds). Renewing a lease never refreshes a velocity TTL.
4. Stop/release, expired lease or command, communication failure and service shutdown cancel pending motion and stand stages. A stop revokes the lease, so subsequent movement requires reacquiring and preparing again.
5. Software emergency stop latches and rejects motion. Explicit reset with the currently observed `safety_revision` clears the latch; it does not reacquire control or resume movement.

| Field | Server limit | Meaning |
| --- | --- | --- |
| `vx` | ±0.2 m/s | Native forward velocity |
| `vy` | ±0.1 m/s | Native lateral velocity |
| `yaw` | ±0.3 rad/s | Native yaw rate |
| `ttl` | >0, ≤0.5 s | Command lifetime using server monotonic time |
| Arm `b` | ±180° | Simulated base target |
| Arm `s`, `e`, `w` | ±90° | Simulated shoulder/elbow/wrist target |
| Arm `h` | ±45° | Simulated gripper target |
| Arm `delta` | ±2° per request | Simulated jog increment |
| Arm `speed` | >0, ≤5°/s | Simulation request bound |

Limits reject invalid inputs rather than silently clipping them. Boolean values, NaN/infinity, unknown fields/actions, stale sequences and wrong/expired leases are rejected. Native lateral/yaw values are passed through without left/right labels.

Dog sends use the stock little-endian `<III` and `<IIId` formats. An independent worker maintains 20 Hz heartbeat/velocity output. On stop it attempts zero forward/lateral/yaw and stationary packets independently, repeating a bounded burst. There is no UDP acknowledgement; a successful send does not prove execution or physical stop. This service does not send the vendor's separate soft-estop opcode, torque-release or posture-toggle commands.

**Software emergency stop is a command latch and best-effort dog stop, not a hardware emergency stop.** No arm hold packet is sent. OS stalls, process kill, power loss, network loss or controller failure can defeat software stopping. Retain the physical remote/stop procedure and clear operating space. Command TTL is a server deadline, not a guaranteed stopping time or distance. No collision sensing, balance telemetry or hardware safety certification is provided.

## HTTP contract

All camera and control endpoints use anonymous trusted-LAN access. `GET /api/status` is public and returns mode, latch, lease occupancy, dog phase, limits and arm capability. The lease ID is omitted. It includes an opaque `safety_revision`: pass the value observed by the operator to reset. Every emergency stop, newly latched transport fault and successful reset changes it, so a delayed reset cannot clear a newer latch. Every control POST requires `Content-Type: application/json`, one bounded Content-Length and an object body. Body limit is 4096 bytes. Browser requests with an Origin must match the server origin.

| POST path | JSON body |
| --- | --- |
| `/api/control/acquire` | `{"owner":"my-script"}`; response includes the private `lease` |
| `/api/control/heartbeat` | `{"lease":"..."}` |
| `/api/control/release` | `{"lease":"..."}` |
| `/api/control/estop` | `{}`; any LAN client can stop |
| `/api/control/reset` | `{"revision":"<observed safety_revision>"}`; stale revisions are rejected |
| `/api/dog/stand` | `{"lease":"...","seq":1}` |
| `/api/dog/velocity` | `{"lease":"...","seq":2,"vx":0.1,"vy":0,"yaw":0,"ttl":0.3}` |
| `/api/dog/stop` | `{}` for an observer stop, or current `lease`; a stale supplied lease is rejected |
| `/api/arm/jog` | `{"lease":"...","seq":3,"joint":"e","delta":1,"speed":2}`; simulation only |
| `/api/arm/state` | `{}`; explicit hardware serial query or simulated state |
| `/api/arm/reconnect` | `{}`; explicitly clear failed read session, lazy until next read |

Success is HTTP 200; validation 400, origin 403, missing action 404, ownership conflicts 409, too-large body 413, safety latch/capability block 423, transport/unavailable 503. Error bodies contain `error`. POST requests are never automatically retried. A transport failure can mean an operation was accepted but the response was lost; reacquire only after inspecting status and taking appropriate stop action.

## Python scripts

The SDK needs only the standard library. Put `src` on your module search path or import `src.dev_client` from the checkout. It retains a private lease and serializes motion requests with increasing sequence numbers. Reads, heartbeats and stop/estop use separate HTTP requests and can run concurrently; slow reads cannot block them. Requests have bounded timeouts, refuse redirects and never retry motion. Old responses/errors are scoped to their originating session; superseded acquire/motion calls raise `DevClientError`. It does not run an automatic heartbeat thread.

```python
import time
from src.dev_client import DevClient

with DevClient('http://127.0.0.1:8080') as robot:
    robot.acquire('offline-demo')
    robot.stand()
    deadline = time.monotonic() + 6
    while time.monotonic() < deadline:
        robot.heartbeat()
        time.sleep(0.2)
    for _ in range(10):
        robot.heartbeat()
        robot.velocity(vx=0.1, ttl=0.3)
        time.sleep(0.1)
    robot.stop()
```

Run this against the simulation command above first. Normal context exit releases a held lease. An HTTP error clears its originating SDK lease instead of replaying requests; it cannot clear a newer session. To reset a latch, inspect `state = robot.status()` and explicitly call `robot.reset(state["safety_revision"])` after deciding to reset that observed state. For arm-only simulated development, acquire then call `arm_jog('e', 1)` and `arm_state()`; no dog stand is needed.

## Verification

`python3 -B scripts/check.py --shell` runs offline syntax/unit/integration checks; `make browser-test` includes viewer and dev console behavior. Fakes and loopback are used for UDP, serial, SSH and control lifecycle checks.
