# Lite3 Dev Server

Dev Server centralizes camera streaming, Lite3 UDP commands and R5 serial feedback. Scripts and the browser call one HTTP service; SSH is used for deployment, tunnels and an optional persistent arm helper. No caller can upload arbitrary programs or send arbitrary robot packets through this API.

The camera viewer is at `/`; the control console is `/control`. The service runs from `~/lite3-dev-server/` as `lite3-dev-server.service`. ROS mode uses the external camera-only `web_video_server`.

## Default robot connection

| Setting | Default |
| --- | --- |
| Username | `ysc` |
| Password | `'` (one ASCII single quote) |
| Perception host | `192.168.1.103` |
| Motion control (UDP) | `192.168.1.120:43893` |
| Dog state listener (UDP on the server host) | `43897` |

Use `ysc@192.168.1.103` for SSH. Enter `'` at SSH or sudo password prompts. The camera page is `http://192.168.1.103:8080/`, and the control console is `http://192.168.1.103:8080/control`. All dev server endpoints are available without authentication. The server assumes a secure, trusted network with no public internet access. Do not add authentication to the dev server.

## Environments and control modes

The server and native `view` and `deploy` commands default to `--env dist` with `--control-mode hardware`. That configuration sends real Lite3 commands and offers read-only R5 feedback. Use `--env dev` for engineering work. Dev also uses real transport unless you add `--simulate`. Simulation and commissioned R5 diagnostics are dev features.

Set `--control-mode readonly` to use real camera capture and R5 feedback with motion disabled. Use `--control-mode disabled` to disable the control service. Both environments support these settings. `--simulate` requires `dev` and `hardware` mode.

## Run locally in simulation

Python 3.8+ is recommended for development. No camera, SSH, serial package or robot is needed:

```bash
python3 src/server.py --env dev --simulate --source none --bind 127.0.0.1 --port 8080
```

Open `http://127.0.0.1:8080/control` and acquire control. Simulation runs without a motion UDP socket, arm SSH session or serial port. The console and SDK can use the API as soon as the server is ready.

The console labels the environment separately from control mode. It distinguishes simulated targets, sent commands and observed feedback. Dog action controls show the current action and feedback readiness. Simulation supplies explicitly marked simulated dog feedback. The console shows arm jog widgets when dev simulation or commissioned diagnostics are available. Camera previews link to the full streaming and source-selection viewer. Camera failure does not disable the control API.

## Deploy on the perception host

Deployment captures and uploads the server, control modules, fixed arm helper and browser assets. It installs/enables the system service without interrupting the running process.

Deploy from the checkout:

```bash
python3 src/launcher.py deploy ysc@192.168.1.103 \
  --backend v4l2 --device /dev/v4l/by-id/CONFIRMED_RGB_CAMERA \
  --env dist
```

Use the confirmed camera path in place of the example. Use `--backend none` for a service without camera capture. Deployment writes the current launch options to the service unit. For an existing installation, deploy again and check local systemd overrides for obsolete options. Once ready to replace the current process, use the existing service lifecycle:

```bash
sudo systemctl restart lite3-dev-server.service
journalctl -u lite3-dev-server.service -b
```

A manually started detached server must first be stopped using the existing [camera lifecycle guide](CAMERA_STREAM.md#shared-server-lifecycle); do not start two instances against the same hardware. The new code does not migrate a live process automatically.

From the development computer:

```bash
python3 src/launcher.py view ysc@192.168.1.103 --backend v4l2 \
  --device /dev/v4l/by-id/CONFIRMED_RGB_CAMERA --console
```

Use `--jump ysc@192.168.2.1` if needed, or existing automatic macOS route selection. This opens the console through the local SSH tunnel. Closing this tunnel does not stop the shared server. Lease and command expiry still apply. `view` requires a control-capable listener to report the requested environment, simulation setting, control mode and R5 diagnostics setting. Restart with the requested settings or select a different server port when those settings differ. A reused server keeps its camera configuration. Every native view requires the current camera and development protocol headers. Deploy and restart a listener that lacks either header.

## Hardware capabilities

For deployment acceptance with **no motion output**, use `--control-mode readonly` with both `deploy` and `view`. This mode provides real camera streams and explicit R5 feedback reads. It rejects ownership and motion actions without opening a dog UDP socket, including on shutdown. Check `/api/status` reports `readonly` after explicitly restarting the service. Serial opening can still affect a controller's reset lines, so use the verified DTR/RTS configuration and an unoccupied device. The service needs access to the serial device group (on Ubuntu, a service-only `SupplementaryGroups=dialout` drop-in can provide it without changing the login account).

Real dog output is available with the default `hardware` control mode in `dist` and in `dev` when using real transport without an arm commissioning profile. The default motion target is `192.168.1.120:43893`. Use `--dog-host 192.168.1.120` to set the same host explicitly. The default serial device is `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`. Use `--arm-device` to select a confirmed serial node. The service account needs serial permissions and pySerial and runs without interactive sudo.

Timed dog actions additionally require measured state uploads. The service owns a passive UDP listener on `--dog-state-port 43897` and accepts state packets only from the configured `--dog-host`. Configure the robot's upload destination to the server host and this UDP port, with the matching route and firewall access. Changing the listener port also requires changing the robot's upload destination. The listener sends no subscription or configuration commands to the robot.

An existing ROS receiver may already own UDP port 43897. The service uses an exclusive listener; an occupied port leaves predefined actions unavailable and exposes the reason in `dog.feedback.reason`. Existing raw stand, velocity and stop controls remain available with their established restrictions. Allocate an unused port and configure the robot to send there, or explicitly stop the competing receiver as part of deployment. The camera and HTTP service continue operating when the feedback listener cannot start.

The decoder accepts the modern 208-byte `RobotState` payload in a 220-byte `0x0901` upload packet. Its layout follows the pinned upstream [Lite3_ROS protocol.hpp](https://github.com/DeepRoboticsLab/Lite3_ROS/blob/e4ab5dc1c7f3ff46274162a1ef302253936578aa/src/transfer/include/protocol.hpp#L29-L55). Other packet layouts cannot establish action readiness. Confirm the robot's firmware layout and upload configuration during deployment; hardware acceptance remains outstanding.

R5 is attached by USB to the perception host. Normally the server owns serial locally, avoiding per-command SSH. If the server runs on another Linux host and needs remote serial access to the perception host, configure `--arm-host ysc@192.168.1.103` with verified OpenSSH configuration/known hosts and noninteractive authentication. One fixed persistent helper handles serialized queries. Opening serial is explicit via arm state retrieval; no background scan or automatic reconnect opens it. A failed session requires explicit `arm/reconnect` before another read. DTR/RTS are cleared before opening; opening may still reset a controller, so confirm its behavior on the device. Exclusive serial locking does not prevent unrelated software that ignores locks.

**Hardware R5 motion is available only in commissioned dev sessions.** `--env dev --arm-motion-profile PATH` opts a local arm into bounded diagnostics in `hardware` mode and disables all dog UDP output. It requires real transport and a one-use commissioning record for the local Linux host, boot and serial device. The SSH gateway provides read-only feedback. [R5 diagnostics guide](R5_DIAGNOSTICS.md)

The read-only R5 helper sends `{"T":105}` and validates `T=1051` replies. The gateway converts `b`, `s`, `e` and `w` feedback from radians to degrees. `gripper_position_deg` is always `null` for hardware feedback. Simulation exposes target values for all five joints.

## Control lifecycle and limits

1. Acquire the sole control lease. It expires after two seconds without heartbeat.
2. Explicitly prepare the dog using `dog/stand`. It sends the stock explicit stand sequence and waits about 5.6 seconds while the caller renews the lease. `dog.phase == "ready"` records completion of that command sequence. A predefined action also requires fresh measured standing feedback.
3. Start one predefined action, or send bounded velocities with increasing sequence numbers and a short TTL (at most 0.5 seconds). Renewing a lease leaves both the action duration and velocity TTL unchanged.
4. Stop/release, expired lease or command, communication failure and service shutdown cancel pending motion and stand stages. A stop revokes the lease, so subsequent movement requires reacquiring and preparing again.
5. Software emergency stop latches and rejects motion. Explicit reset with the currently observed `safety_revision` clears the latch; it does not reacquire control or resume movement.

| Field | Server limit | Meaning |
| --- | --- | --- |
| `vx` | ±0.2 m/s | Native forward velocity |
| `vy` | ±0.1 m/s | Native lateral velocity |
| `yaw` | ±0.3 rad/s | Native yaw rate |
| `ttl` | >0, ≤0.5 s | Command lifetime using server monotonic time |
| Action translation `speed` | >0, ≤0.2 m/s | Positive speed for forward/backward |
| Action turn `speed` | >0, ≤0.3 rad/s | Positive speed for left/right turns |
| Action `duration` | >0, ≤10 s | Monotonic lifetime from the first velocity output |
| Arm `b` | ±180° | Simulated base target |
| Arm `s`, `e`, `w` | ±90° | Simulated shoulder/elbow/wrist target |
| Arm `h` | ±45° | Simulated gripper target |
| Arm `delta` | ±2° per request | Simulated jog increment |
| Arm `speed` | >0, ≤5°/s | Simulation request bound |

Limits reject out-of-range inputs, boolean values, NaN/infinity, unknown fields/actions, stale sequences and wrong/expired leases. Raw lateral/yaw values retain the native sign convention. Predefined actions map `forward` to positive `vx`, `backward` to negative `vx`, `turn_left` to negative native `yaw`, and `turn_right` to positive native `yaw`. The yaw mapping follows the sign conversion in the pinned upstream [Lite3_ROS Jetson2Motion.cpp](https://github.com/DeepRoboticsLab/Lite3_ROS/blob/e4ab5dc1c7f3ff46274162a1ef302253936578aa/src/transfer/src/Jetson2Motion.cpp#L383-L386).

### Predefined dog actions

The server runs `forward`, `backward`, `turn_left` and `turn_right` asynchronously through its shared motion writer. Start requires a current lease, an increasing sequence, completed explicit stand preparation and valid feedback at most 0.5 seconds old. The measured state must indicate stable low-speed standing with `basic_state=6`, `gait_state=0`, and `motion_state=0`. `dog.actions_enabled` exposes readiness for a new action. `dog.feedback` reports whether state was observed or simulated, its freshness and age, the measured state codes, standing/motion readiness and an unavailable reason.

While an action runs, the server continues checking fresh suitable feedback and accepts `motion_state` 0 or 1. Missing, stale or unsuitable feedback stops the action. The duration begins with the first velocity output; timing precision follows the existing 20 Hz writer. A second action or raw velocity request cannot replace an active action. Keep sending explicit lease heartbeats until the action ends; lease expiry takes precedence over the requested duration. The SDK and browser never prepare or acquire control automatically for an action.

`dog.action` is null before the first action. Thereafter it contains `id`, `name`, `speed`, `duration`, `phase`, `reason` and `remaining_s`. The phase is `running`, `completed`, `cancelled` or `fault`. Completion runs the bounded stop sequence and revokes ownership. Inspect the terminal phase and reason before proceeding. Further motion requires a new lease and explicit preparation. Reported completion describes the server's timed command lifecycle; physical stopping remains unverified.

Keep the returned action `id` to query it through `POST /api/dog/action/status` after another action starts or control changes hands. The lookup returns the active action or a retained terminal snapshot with the original ID, parameters, phase and reason. The process retains at most 128 terminal records for 300 seconds from each terminal transition, evicting the oldest completions first. Reads leave that retention deadline unchanged. Running actions remain queryable independently of these limits. Unknown, evicted or expired IDs return HTTP 404 with `code: "not_found"`; restarting the process clears history. The global `dog.action` summary continues to show the current or most recent action, including its terminal state after lookup retention expires.

An action lookup requires only the ID and leaves leases, control sequences, action timing and motion unchanged. The writer remains responsible for advancing deadlines and stopping motion. A lookup can briefly report `running` with `remaining_s=0` while the writer processes a due deadline.

Dog sends use the stock little-endian `<III` and `<IIId` formats. An independent worker maintains 20 Hz heartbeat/velocity output. Every hardware dog stop path, including release, expiry and shutdown, repeats ten bounded bursts. Each burst first attempts the vendor's voice cancellation (`0x21010C0A`, value `0`) to end the persistent voice stand action started by `dog/stand`, then heartbeat, zero forward/lateral/yaw and stationary packets. Each packet is attempted independently, including when cancellation fails. There is no UDP acknowledgement; a successful send does not prove execution or physical stop. This service does not send the vendor's separate soft-estop opcode, torque-release or posture-toggle commands.

Software emergency stop latches motion and requests cancellation. Dog hardware mode attempts the bounded zero-velocity and stationary burst. In the arm-only diagnostics configuration, cancellation may submit one bounded base or elbow hold if command history and feedback are still credible. OS stalls, process termination, power loss or controller failure can defeat software stopping. Retain the physical stop procedure and clear operating space. Command TTL is a server deadline. Physical stopping remains unverified.

## HTTP contract

All camera and control endpoints use anonymous trusted-LAN access. `GET /api/status` is public and returns `environment`, `mode`, latch, lease occupancy, dog phase, limits and arm capability. `dog.simulated` and `arm.simulated` identify simulated transport. The lease ID is omitted. Pass the opaque `safety_revision` observed by the operator to reset. Every emergency stop, newly latched transport fault and successful reset changes it, so a delayed reset cannot clear a newer latch. Every control POST requires `Content-Type: application/json`, one bounded Content-Length and an object body. Body limit is 4096 bytes. Browser requests with an Origin must match the server origin.

| POST path | JSON body |
| --- | --- |
| `/api/control/acquire` | `{"owner":"my-script"}`; response includes the private `lease` |
| `/api/control/heartbeat` | `{"lease":"..."}` |
| `/api/control/release` | `{"lease":"..."}` |
| `/api/control/estop` | `{}`; any LAN client can stop |
| `/api/control/reset` | `{"revision":"<observed safety_revision>"}`; stale revisions are rejected |
| `/api/dog/stand` | `{"lease":"...","seq":1}` |
| `/api/dog/velocity` | `{"lease":"...","seq":2,"vx":0.1,"vy":0,"yaw":0,"ttl":0.3}` |
| `/api/dog/action` | `{"lease":"...","seq":2,"name":"forward","speed":0.1,"duration":1.0}`; all fields are required; returns control status immediately |
| `/api/dog/action/status` | `{"id":"<action_id>"}`; ID must be a string of 1–128 characters; returns `DogActionStatus` without a lease or sequence |
| `/api/dog/stop` | `{}` for an observer stop, or current `lease`; a stale supplied lease is rejected |
| `/api/arm/jog` | Dev only. `{"lease":"...","seq":3,"joint":"e","delta":1,"speed":2}` for simulation. Commissioned hardware diagnostics use an explicit speed at or below 1°/s and return control status with an asynchronous operation. |
| `/api/arm/state` | `{}`; explicit hardware serial query or simulated state |
| `/api/arm/reconnect` | `{}`; clear a failed read session and leave the transport unopened until the next read. Diagnostics require fresh commissioning after reconnect. |
| `/api/arm/operation` | `{"id":"<operation_id>"}`; in commissioned dev sessions, read the active or latest matching hardware operation without a lease |
| `/api/arm/stop` | `{}` for an observer stop, or `{"lease":"..."}` for the current owner. Requests cancellation, revokes the shared lease and returns control status. |

Success is HTTP 200; validation 400, origin 403, missing action 404, ownership conflicts 409, too-large body 413, safety latch/capability block 423, transport/unavailable 503. Error bodies contain `error`. POST requests are never automatically retried. A transport failure can mean an operation was accepted but the response was lost; reacquire only after inspecting status and taking appropriate stop action.

Control-service errors also carry an optional stable `code`: `invalid_request`, `not_found`, `conflict`, `locked` or `unavailable`. The HTTP adapter maps these to 400, 404, 409, 423 and 503. Other errors can contain only the `error` string. SDK callers can inspect `DevClientError.status` and use the error message for context.

### Protocol contract

Responses must advertise exactly one `X-Lite3-Dev-Protocol: 1` header. The Python SDK and browser client reject missing, empty, ambiguous or different protocol values. A failed check never replays a command. Update the server, SDK and browser assets together.

The Python SDK validates required response fields, nested objects, discriminators and finite numeric values before returning its typed dictionaries. Additional fields, including additional capability metadata, are retained. Unknown values of existing enums and missing or incorrectly typed required fields raise `DevClientError`; these changes require an updated contract. Action-ID lookups preserve ownership on validation failure. Validation failures from other requests clear ownership for the originating session, while a newer session remains intact. Keep wire-incompatible changes behind a new protocol version and update the SDK, browser client and contract tests together.

`GET /api/status` returns a full `ControlStatus`, including when its mode is `disabled`. Control, dog and arm fields are required. An SDK release version is independent of the HTTP protocol version.

## Python scripts

Install the SDK from the repository root on your development machine. It needs Python 3.8 or later and has no runtime dependencies beyond the standard library.

```bash
python -m pip install .
```

The `lite3_dev_server` package exports `DevClient`, `DevClientError`, the `Environment` and `ControlMode` type aliases, and typed JSON dictionaries for requests and responses. Dog action contracts include `DogActionName`, `DogActionRequest`, `DogActionStatusRequest`, `DogActionStatus` and `DogFeedback`; `DogStatus` includes `feedback`, `action` and `actions_enabled`. Use the installed `lite3_dev_server` package for Python callers. Robot deployment uses the standalone server files.

Use `src/launcher.py` for deployment and tunnels. The console loads its HTTP client from `/static/dev_client.js`; UI timers and local lease state remain in the control page.

The client retains a private lease and serializes motion requests with increasing sequence numbers. Reads, heartbeats and stop/estop use separate HTTP requests and can run concurrently. Requests have bounded timeouts, refuse redirects and never retry motion. Responses and errors are scoped to their originating session. Superseded acquire or motion calls raise `DevClientError`. Call heartbeat explicitly while holding a lease.

`dog_action(name, speed, duration)` returns `ControlStatus` immediately. Convenience methods `forward(speed=0.1, duration=1.0)`, `backward(speed=0.1, duration=1.0)`, `turn_left(speed=0.2, duration=1.0)` and `turn_right(speed=0.2, duration=1.0)` use the same endpoint. Translation speeds are m/s, turn speeds are rad/s, and durations are seconds. Each method accepts a positive speed. The following simulation example keeps the lease alive throughout preparation and execution:

```python
import time
from lite3_dev_server import DevClient, DevClientError

with DevClient('http://127.0.0.1:8080') as robot:
    robot.acquire('offline-demo')
    state = robot.stand()
    deadline = time.monotonic() + 10
    while not state['dog']['actions_enabled']:
        if time.monotonic() >= deadline:
            raise TimeoutError(state['dog']['feedback']['reason'])
        time.sleep(0.2)
        state = robot.heartbeat()

    state = robot.forward(speed=0.1, duration=1.0)
    action = state['dog']['action']
    assert action is not None
    action_id = action['id']
    while action['phase'] == 'running':
        time.sleep(0.1)
        try:
            state = robot.heartbeat()
        except DevClientError as error:
            # Completion can revoke the lease just before this heartbeat arrives.
            latest = robot.status()
            if error.status != 409 or latest['mode'] != 'hardware':
                raise
            ended = latest['dog']['action']
            if ended is None or ended['id'] != action_id or ended['phase'] == 'running':
                raise
            state = latest
        action = state['dog']['action']
        assert action is not None
    if action['phase'] != 'completed':
        raise RuntimeError(action['reason'])
    print(action['phase'])
```

Run this example against the local `--env dev --simulate` server above. Normal context exit releases a held lease. When a status or owned response reports a terminal dog action with `lease_active=false`, the SDK forgets that session's lease, so context cleanup needs no release request. A terminal result matching the action acknowledged in that session also clears its lease when another controller now holds control. An HTTP error from a control request also clears the lease belonging to its originating SDK session. Responses from an earlier session cannot clear a newly acquired lease.

`dog_action_status(action_id)` returns `DogActionStatus` for a known ID, including retained results from an earlier session. It sends one lookup request and leaves the SDK's lease and sequence unchanged on success or error. An expired or unknown ID raises `DevClientError` with `status == 404`. Send heartbeats separately when maintaining control. For example, an observer can inspect the `action_id` saved above:

```python
observer = DevClient('http://127.0.0.1:8080')
result = observer.dog_action_status(action_id)
print(result['id'], result['phase'], result['reason'])
```

If an action completes between the last response and context exit, cleanup may receive HTTP 409 when releasing its revoked lease. It then reads status and accepts completion only for the action ID acknowledged in that lease session. This also works when another controller has acquired control. Cleanup sends only its original lease-scoped release and the confirmation read. Transport failures, unconfirmed completion, cancellation and faults remain errors. An explicit `release()` keeps its usual error behavior.

Check `state = robot.status()` and `state["mode"]` before reading control fields. Every mode returns a full `ControlStatus`. With `hardware` control mode, pass `state["safety_revision"]` to `robot.reset()` after deciding to reset the observed latch. For arm-only simulated development, check `state["environment"] == "dev"` and `state["arm"]["simulated"]` before acquiring a lease and calling `arm_jog('e', 1)`. Then read `arm_state()`. Its `observed` flag identifies actual feedback. Hardware feedback includes four measured angles and `gripper_position_deg` is `None`.

In commissioned arm diagnostics, `arm_jog()` returns `ControlStatus` with `arm.operation`. Keep the operation's `operation_id`, call `arm_operation(id)` to read its `ArmOperation` status and send explicit heartbeats while it runs. `arm_stop()` cancels the operation and releases ownership. Inspect `arm.capabilities` for the diagnostic limits and `arm.motion_enabled` for current readiness. [R5 operation lifecycle](R5_DIAGNOSTICS.md#operation-lifecycle)

## Verification

`python3 -B scripts/check.py --shell` runs offline syntax, unit and integration checks. `make browser-test` includes viewer and dev console behavior. Fakes and loopback cover UDP, serial, SSH and control lifecycle checks.

Dog feedback decoding and predefined actions have offline coverage only. Robot upload configuration, firmware compatibility, physical direction, motion timing and stopping still require hardware acceptance.
For package acceptance, install `.[dev]` in a development virtual environment and run `python -B scripts/check_distribution.py`. It checks SDK types, service return types, a built wheel and an external consumer using the installed package, then runs an HTTP control lifecycle against a copied deployment bundle. `node tests/dev_client_test.js` checks browser command serialization, errors and protocol enforcement. Use Python 3.8–3.12 for `python -B scripts/check.py --device-syntax`; this parses every deployed Python module with the device's Python 3.6 grammar. Device runtime and physical motion acceptance remain separate checks.
