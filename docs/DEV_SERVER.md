# Lite3 Dev Server

Dev Server centralizes camera streaming, Lite3 UDP commands and R5 serial feedback. Scripts and the browser call one HTTP service; SSH is used for deployment, tunnels and an optional persistent arm helper. No caller can upload arbitrary programs or send arbitrary robot packets through this API.

The camera viewer is at `/`; the control console is `/control`. The service runs from `~/lite3-camera-server/` as `lite3-camera-server.service`. ROS mode uses the external camera-only `web_video_server`.

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

Deployment captures and uploads the server, control modules, fixed arm helper and browser assets. It installs/enables the system service without interrupting the running process.

Deploy from the checkout:

```bash
python3 src/launcher.py deploy ysc@192.168.1.103 \
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
python3 src/launcher.py view ysc@192.168.1.103 --backend v4l2 \
  --device /dev/v4l/by-id/CONFIRMED_RGB_CAMERA --console
```

Use `--jump ysc@192.168.2.1` if needed, or automatic macOS route selection. This opens the console through the local SSH tunnel. Closing this tunnel does not stop the shared server; motion lease/command expiry still applies. `view` reuses existing camera **and control** settings; changing launch options never reconfigures a running service. Every native `view` requires the current camera and development protocol headers from the listener.

## Hardware capabilities

For deployment acceptance with **no motion output**, use `--control-mode readonly`. This mode provides real camera streams and explicit R5 feedback reads, but rejects every control/ownership/motion action and never creates a dog UDP socket, including on shutdown. Check `/api/status` reports `readonly` after explicitly restarting the service; a reused server keeps its old settings. Serial opening can still affect a controller's reset lines, so use the verified DTR/RTS configuration and an unoccupied device. The service needs access to the serial device group (on Ubuntu, a service-only `SupplementaryGroups=dialout` drop-in can provide it without changing the login account).

Real dog output requires `--control-mode hardware`. The default motion target is `192.168.1.120:43893`; use `--dog-host 192.168.1.120` to set the same host explicitly. The default serial device is `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`; `--arm-device` selects a confirmed serial node. The service account needs serial permissions and pySerial; the service does not run interactive sudo.

R5 is attached by USB to the perception host. Normally the server owns serial locally, avoiding per-command SSH. If the server runs on another Linux host and needs remote serial access to the perception host, configure `--arm-host ysc@192.168.1.103` with verified OpenSSH configuration/known hosts and noninteractive authentication. One fixed persistent helper handles serialized queries. Opening serial is explicit via arm state retrieval; no background scan or automatic reconnect opens it. A failed session requires explicit `arm/reconnect` before another read. DTR/RTS are cleared before opening; opening may still reset a controller, so confirm its behavior on the device. Exclusive serial locking does not prevent unrelated software that ignores locks.

**Hardware R5 motion is disabled by default.** `--arm-motion-profile PATH` opts a local arm into bounded diagnostics in hardware mode and disables all dog UDP output. It requires a one-use commissioning record for the local Linux host, boot and serial device. The SSH gateway remains read-only. Current hardware verification covers read-only feedback. [R5 diagnostics guide](R5_DIAGNOSTICS.md)

The read-only R5 helper sends `{"T":105}` and validates `T=1051` replies. The gateway converts `b`, `s`, `e` and `w` feedback from radians to degrees. `gripper_position_deg` is always `null` for hardware feedback. Simulation exposes target values for all five joints.

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

Software emergency stop latches motion and requests cancellation. Dog hardware mode attempts the bounded zero-velocity and stationary burst. In the arm-only diagnostics configuration, cancellation may submit one bounded base or elbow hold if command history and feedback are still credible. OS stalls, process termination, power loss or controller failure can defeat software stopping. Retain the physical stop procedure and clear operating space. Command TTL is a server deadline. Physical stopping remains unverified.

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
| `/api/arm/jog` | `{"lease":"...","seq":3,"joint":"e","delta":1,"speed":2}` for simulation. Commissioned hardware diagnostics use an explicit speed at or below 1°/s and return control status with an asynchronous operation. |
| `/api/arm/state` | `{}`; explicit hardware serial query or simulated state |
| `/api/arm/reconnect` | `{}`; clear a failed read session and leave the transport unopened until the next read. Diagnostics require fresh commissioning after reconnect. |
| `/api/arm/operation` | `{"id":"<operation_id>"}`; read the active or latest matching hardware operation without a lease |
| `/api/arm/stop` | `{}` for an observer stop, or `{"lease":"..."}` for the current owner. Requests cancellation, revokes the shared lease and returns control status. |

Success is HTTP 200; validation 400, origin 403, missing action 404, ownership conflicts 409, too-large body 413, safety latch/capability block 423, transport/unavailable 503. Error bodies contain `error`. POST requests are never automatically retried. A transport failure can mean an operation was accepted but the response was lost; reacquire only after inspecting status and taking appropriate stop action.

Control-service errors also carry an optional stable `code`: `invalid_request`, `not_found`, `conflict`, `locked` or `unavailable`. The HTTP adapter maps these to 400, 404, 409, 423 and 503. Other errors can contain only the `error` string. SDK callers can inspect `DevClientError.status` and use the error message for context.

### Protocol contract

Responses must advertise exactly one `X-Lite3-Dev-Protocol: 1` header. The Python SDK and browser client reject missing, empty, ambiguous or different protocol values. A failed check never replays a command. Update the server, SDK and browser assets together.

The Python SDK validates required response fields, nested objects, discriminators and finite numeric values before returning its typed dictionaries. Additional fields, including additional capability metadata, are retained. Unknown values of existing enums and missing or incorrectly typed required fields raise `DevClientError`; these changes require an updated contract. Validation failures clear ownership for the originating session, while a newer session remains intact. Keep wire-incompatible changes behind a new protocol version and update the SDK, browser client and contract tests together.

`GET /api/status` returns a full `ControlStatus`, including when its mode is `disabled`. Control, dog and arm fields are required. An SDK release version is independent of the HTTP protocol version.

## Python scripts

Install the SDK from the repository root on your development machine. It needs Python 3.8 or later and has no runtime dependencies beyond the standard library.

```bash
python -m pip install .
```

The `lite3_dev_server` package exports `DevClient`, `DevClientError`, and typed JSON dictionaries for requests and responses. Robot deployment uses the standalone server files.

Use `src/launcher.py` for deployment and tunnels. The console loads its HTTP client from `/static/dev_client.js`; UI timers and local lease state remain in the control page.

The client retains a private lease and serializes motion requests with increasing sequence numbers. Reads, heartbeats and stop/estop use separate HTTP requests and can run concurrently. Requests have bounded timeouts, refuse redirects and never retry motion. Responses and errors are scoped to their originating session. Superseded acquire or motion calls raise `DevClientError`. Call heartbeat explicitly while holding a lease.

```python
import time
from lite3_dev_server import DevClient

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

Run this against the simulation command above first. Normal context exit releases a held lease. An HTTP error clears the lease belonging to its originating SDK session.

Use `state = robot.status()` to inspect the mode and capabilities before controlling the robot. In simulation or hardware mode, pass `state["safety_revision"]` to `robot.reset()` after deciding to reset the observed latch. For arm-only simulated development, acquire a lease, call `arm_jog('e', 1)` and read `arm_state()`. The `observed` flag distinguishes hardware feedback from the arm status summary. Hardware feedback includes four measured angles and `gripper_position_deg` is `None`.

In commissioned arm diagnostics, `arm_jog()` returns `ControlStatus` with `arm.operation`. Keep the operation's `operation_id`, call `arm_operation(id)` to read its `ArmOperation` status and send explicit heartbeats while it runs. `arm_stop()` cancels the operation and releases ownership. Inspect `arm.capabilities` for the diagnostic limits and `arm.motion_enabled` for current readiness. [R5 operation lifecycle](R5_DIAGNOSTICS.md#operation-lifecycle)

## Verification

`python3 -B scripts/check.py --shell` runs offline syntax, unit and integration checks. `make browser-test` includes viewer and dev console behavior. Fakes and loopback cover UDP, serial, SSH and control lifecycle checks.

For package acceptance, install `.[dev]` in a development virtual environment and run `python -B scripts/check_distribution.py`. It checks SDK types, service return types, a built wheel and an external consumer using the installed package, then runs an HTTP control lifecycle against a copied deployment bundle. `node tests/dev_client_test.js` checks browser command serialization, errors and protocol enforcement. Use Python 3.8–3.12 for `python -B scripts/check.py --device-syntax`; this parses every deployed Python module with the device's Python 3.6 grammar. Device runtime and physical motion acceptance remain separate checks.
