# Lite3 Dev Server

Dev Server centralizes camera streaming, Lite3 UDP commands and R5 serial feedback. Scripts and the browser call one HTTP service; SSH is used for deployment, tunnels and an optional persistent arm helper. No caller can upload arbitrary programs or send arbitrary robot packets through this API.

The camera viewer remains at `/`; the control console is `/control`. Existing deployment directories and `lite3-camera-server.service` remain in use so an upgrade does not create a competing camera owner. ROS mode still uses the external camera-only `web_video_server`.

## Run locally in simulation

Python 3.8+ is recommended for development. No camera, SSH, serial package or robot is needed:

```bash
# Generate this once; keep the file outside the checkout.
python3 -c 'import os,secrets; p=os.path.expanduser("~/.lite3-dev-token"); fd=os.open(p,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(fd,(secrets.token_urlsafe(32)+"\n").encode()); os.close(fd)'
python3 src/server.py --source none --bind 127.0.0.1 --port 8080 \
  --control-token-file "$HOME/.lite3-dev-token"
```

Open `http://127.0.0.1:8080/control`, enter the file's token and acquire control. Simulation is the default and never opens a motion UDP socket, SSH session or serial port. Without a configured token file, status and cameras work but control mutations are unavailable. Token files contain 32–128 URL-safe letters/digits/underscore/hyphen; POSIX file permissions must exclude group/other access. The browser retains the token only in page memory.

The console provides camera snapshots plus a link to the full streaming/source-selection viewer. It distinguishes simulated targets, sent commands and observed arm feedback. Camera failure does not disable the control API.

## Deploy on the perception host

Existing camera commands remain valid. New deployment captures and uploads the server, control modules, fixed arm helper and both pages. It installs/enables the existing system service without interrupting the running process.

Create a private token file on the perception host using the generation command above, then:

```bash
python3 src/client.py deploy ysc@192.168.1.103 \
  --backend v4l2 --device /dev/v4l/by-id/CONFIRMED_RGB_CAMERA \
  --control-mode simulation --control-token-file /home/ysc/.lite3-dev-token
```

Use the confirmed camera path in place of the example. Use `--backend none` for a service without camera capture. The token value is never uploaded or placed in systemd arguments. Once ready to replace the current process, use the existing service lifecycle:

```bash
sudo systemctl restart lite3-camera-server.service
journalctl -u lite3-camera-server.service -b
```

A manually started detached server must first be stopped using the existing [camera lifecycle guide](CAMERA_STREAM.md#shared-server-lifecycle); do not start two instances against the same hardware. The new code does not migrate a live process automatically. Reboot/startup remains subject to the camera guide's hardware acceptance limits.

From the development computer:

```bash
python3 src/client.py view ysc@192.168.1.103 --backend v4l2 \
  --device /dev/v4l/by-id/CONFIRMED_RGB_CAMERA --console
```

Use `--jump ysc@192.168.2.1` if needed, or existing automatic macOS route selection. This opens the console through the local SSH tunnel. Closing this tunnel does not stop the shared server; motion lease/command expiry still applies. `view` reuses existing camera **and control** settings; changing launch options never reconfigures a running service. `--console` or control flags refuse an old camera-only listener and request an explicit upgrade/restart.

## Hardware capabilities

Real dog output requires `--control-mode hardware --control-token-file /absolute/private/file`. The default motion target is `192.168.1.120:43893`; change `--dog-host` only to the verified motion host. The default serial device is `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`; `--arm-device` selects a confirmed serial node. The service account needs serial permissions and pySerial; the service does not run interactive sudo.

R5 is attached by USB to the perception host. Normally the server owns serial locally, avoiding per-command SSH. If serial is on another Linux host, configure `--arm-host USER@HOST` with verified OpenSSH configuration/known hosts and noninteractive authentication. One fixed persistent helper handles serialized queries. Opening serial is explicit via arm state retrieval; no background scan or automatic reconnect opens it. A failed session requires explicit `arm/reconnect` before another read. DTR/RTS are cleared before opening; opening may still reset a controller, so confirm its behavior on the device. Exclusive serial locking does not prevent unrelated software that ignores locks.

**Hardware R5 motion is currently blocked in code.** The source project's 2026-11-27 test recorded unintended elbow lowering and continued base displacement during a hold attempt. `robo-comp/scripts/r5_rotate_180.py` and `R5_ROTATE_180.md` explicitly suspend execution. This gateway preserves that restriction, with no override switch. Hardware jog fails before opening SSH or serial. Read-only T105 feedback remains available; simulation supports bounded jogs. Enabling live arm movement requires a separately verified firmware-specific target mapping and stop implementation.

The protocol sources are the existing `robo-comp/scripts/lite3_stand_walk.py`, `r5_rotate_180.py`, `r5_elbow_down_ssh.py`, and the local Lite3/R5 vendor manuals. The R5 helper sends only `{"T":105}` and validates `T=1051` replies. `b/s/e/w` feedback is converted from radians to degrees under the observed device profile. The gripper has no reliable position feedback: it is returned as unknown, never replaced with a cached target. These ranges and mappings do not establish collision avoidance.

## Control lifecycle and limits

1. Acquire the sole control lease. It expires after two seconds without heartbeat.
2. Explicitly prepare the dog using `dog/stand`. It sends the stock explicit stand sequence and waits about 5.6 seconds while the lease is renewed. No telemetry confirms that the dog actually stood.
3. Send bounded velocities with increasing sequence numbers and a short TTL (at most 0.5 seconds). Renewing a lease never refreshes a velocity TTL.
4. Stop/release, expired lease or command, communication failure and service shutdown cancel pending motion and stand stages. A stop revokes the lease, so subsequent movement requires reacquiring and preparing again.
5. Software emergency stop latches and rejects motion. Explicit reset clears the latch; it does not reacquire control or resume movement.

| Field | Server limit | Meaning |
| --- | --- | --- |
| `vx` | ±0.2 m/s | Native forward velocity |
| `vy` | ±0.1 m/s | Native lateral velocity; sign needs hardware verification |
| `yaw` | ±0.3 rad/s | Native yaw rate; sign needs hardware verification |
| `ttl` | >0, ≤0.5 s | Command lifetime using server monotonic time |
| Arm `b` | ±180° | Simulated base target |
| Arm `s`, `e`, `w` | ±90° | Simulated shoulder/elbow/wrist target |
| Arm `h` | ±45° | Simulated gripper target |
| Arm `delta` | ±2° per request | Simulated jog increment |
| Arm `speed` | >0, ≤5°/s | Simulation request bound |

Limits reject invalid inputs rather than silently clipping them. Boolean values, NaN/infinity, unknown fields/actions, stale sequences and wrong/expired leases are rejected. Native lateral/yaw signs are deliberately not labeled left/right: existing sources do not establish a consistent convention for these complex velocity commands.

Dog sends use the stock little-endian `<III` and `<IIId` formats. An independent worker maintains 20 Hz heartbeat/velocity output. On stop it attempts zero forward/lateral/yaw and stationary packets independently, repeating a bounded burst. There is no UDP acknowledgement; a successful send does not prove execution or physical stop. This service does not send the vendor's separate soft-estop opcode, torque-release or posture-toggle commands without validation.

**Software emergency stop is a command latch and best-effort dog stop, not a hardware emergency stop.** Arm physical stop is unverified and no guessed hold packet is sent. OS stalls, process kill, power loss, network loss or controller failure can defeat software stopping. Retain the physical remote/stop procedure and clear operating space. Command TTL is a server deadline, not a guaranteed stopping time or distance. No collision sensing, balance telemetry or hardware safety certification is provided.

## HTTP contract

Camera endpoints retain anonymous trusted-LAN access. `GET /api/status` is public and returns mode, latch, lease occupancy, dog phase, limits and arm capability. It never returns credentials or the lease ID. Every control POST requires `Authorization: Bearer TOKEN`, `Content-Type: application/json`, one bounded Content-Length and an object body. Body limit is 4096 bytes. Browser requests with an Origin must match the server origin. Credentials are not accepted in URLs. Use SSH/TLS on networks where bearer tokens could be observed.

| POST path | JSON body |
| --- | --- |
| `/api/control/acquire` | `{"owner":"my-script"}`; response includes the private `lease` |
| `/api/control/heartbeat` | `{"lease":"..."}` |
| `/api/control/release` | `{"lease":"..."}` |
| `/api/control/estop` | `{}`; any authenticated operator can stop |
| `/api/control/reset` | `{}`; clears latch only |
| `/api/dog/stand` | `{"lease":"...","seq":1}` |
| `/api/dog/velocity` | `{"lease":"...","seq":2,"vx":0.1,"vy":0,"yaw":0,"ttl":0.3}` |
| `/api/dog/stop` | `{}` or current lease; clears ownership |
| `/api/arm/jog` | `{"lease":"...","seq":3,"joint":"e","delta":1,"speed":2}`; simulation only |
| `/api/arm/state` | `{}`; explicit hardware serial query or simulated state |
| `/api/arm/reconnect` | `{}`; explicitly clear failed read session, lazy until next read |

Success is HTTP 200; validation 400, auth 401, origin 403, missing action 404, ownership conflicts 409, too-large body 413, safety latch/capability block 423, transport/unavailable 503. Error bodies contain `error`. POST requests are never automatically retried. A transport failure can mean an operation was accepted but the response was lost; reacquire only after inspecting status and taking appropriate stop action.

## Python scripts

The SDK needs only the standard library. Put `src` on your module search path or import `src.dev_client` from the checkout. It retains a private lease and allocates increasing sequence numbers, uses bounded HTTP requests, refuses redirects and does not retry motion. It does not run an automatic heartbeat thread.

```python
from pathlib import Path
import time
from src.dev_client import DevClient

token = (Path.home() / '.lite3-dev-token').read_text().strip()
with DevClient('http://127.0.0.1:8080', token) as robot:
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

Run this against the simulation command above first. Normal context exit releases a held lease. An HTTP error clears the SDK lease instead of replaying requests. For arm-only simulated development, acquire then call `arm_jog('e', 1)` and `arm_state()`; no dog stand is needed.

## Verification

`python3 -B scripts/check.py --shell` runs offline syntax/unit/integration checks; `make browser-test` includes viewer and dev console behavior. Fakes and loopback are used for UDP, serial, SSH and control lifecycle checks. These checks do not establish real dog response, R5 stop behavior, live deployment, reboot recovery or Windows hardware operation. Existing camera hardware acceptance records apply only to their recorded version and date.
