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

After `python -m pip install '.[tools]'`, start the installed command from any directory:

```bash
lite3-sim --port 8080
```

This command uses dev simulation, no camera, and the loopback address `127.0.0.1`. Use `--port 0` to let the operating system choose a free port. The command prints its actual URL. It needs no camera, serial device or SSH. Press Ctrl+C to stop it.

The source entry point also remains available:

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

Use the confirmed camera path in place of the example. Use `--backend none` for a service without camera capture. Deployment writes the current launch options to the service unit. For an existing installation, deploy again and check local systemd overrides for obsolete options. Once ready to replace the current process, use the launcher on the development computer:

```bash
python3 src/launcher.py service status ysc@192.168.1.103
python3 src/launcher.py service restart ysc@192.168.1.103
python3 src/launcher.py service logs ysc@192.168.1.103
```

Restart reports active legacy units and conflicting listeners for you to resolve using the [service lifecycle guide](CAMERA_STREAM.md#boot-service). It verifies systemd and HTTP readiness, including camera-free instances. Add the matching `--remote-port` for custom ports, and use `--expect-build` with the identifier printed by deploy to verify the target code. Deployment leaves the current process running until explicit activation.

From the development computer:

```bash
python3 src/launcher.py view ysc@192.168.1.103 --backend v4l2 \
  --device /dev/v4l/by-id/CONFIRMED_RGB_CAMERA --console
```

Use `--jump ysc@192.168.2.1` if needed, or existing automatic macOS route selection. This opens the console through the local SSH tunnel. Closing this tunnel does not stop the shared server. Lease and command expiry still apply. `view` requires a control-capable listener to report the requested environment, simulation setting, control mode and R5 diagnostics setting. Restart with the requested settings or select a different server port when those settings differ. A reused server keeps its camera configuration. Every native view requires the current camera and development protocol headers. Deploy and restart a listener that lacks either header.

For an already configured service, use `--existing-only` to connect with its current settings:

```bash
python3 src/launcher.py view ysc@192.168.1.103 --existing-only --console

# Keep this command running; use the printed URL from a second terminal or application.
python3 src/launcher.py view ysc@192.168.1.103 --existing-only --url-only \
  --local-port 18081 --remote-port 8080
```

Pass the second command's URL to `DevClient`, for example `DevClient("http://127.0.0.1:18081/")`. The tunnel checks `/healthz` for compatible camera and development protocol headers before printing a URL or opening the console. HTTP 503 is accepted because the control API remains available during camera outages. The check uses the service's existing environment, simulation, control and R5 diagnostics settings. Server launch options are rejected in this mode. `--no-open` keeps the readiness check and prints the usual viewer message; `--url-only` prints just the URL to stdout and skips browser opening. Both retain the foreground tunnel until Ctrl+C. See [timeouts and connection failures](CAMERA_STREAM.md#connect-to-an-existing-service).

## Hardware capabilities

For deployment acceptance with **no motion output**, use `--control-mode readonly` with both `deploy` and `view`. This mode provides real camera streams and explicit R5 feedback reads. It rejects ownership and motion actions without opening a dog UDP socket, including on shutdown. Check `/api/status` reports `readonly` after explicitly restarting the service. Serial opening can still affect a controller's reset lines, so use the verified DTR/RTS configuration and an unoccupied device. The service needs access to the serial device group (on Ubuntu, a service-only `SupplementaryGroups=dialout` drop-in can provide it without changing the login account).

Real dog output is available with the default `hardware` control mode in `dist` and in `dev` when using real transport without an arm commissioning profile. The default motion target is `192.168.1.120:43893`. Use `--dog-host 192.168.1.120` to set the same host explicitly. The default serial device is `/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0`. Use `--arm-device` to select a confirmed serial node. The service account needs serial permissions and pySerial and runs without interactive sudo.

Timed dog actions additionally require measured state uploads. The service owns a passive UDP listener on `--dog-state-port 43897` and accepts state packets only from the configured `--dog-host`. Configure the robot's upload destination to the server host and this UDP port, with the matching route and firewall access. Changing the listener port also requires changing the robot's upload destination. The listener sends no subscription or configuration commands to the robot.

Use `python3 src/launcher.py doctor ysc@192.168.1.103` for a [read-only deployment report](CAMERA_STREAM.md). It distinguishes UDP bind conflicts, missing feedback and stale feedback, and reports bundle versions, RGB ownership and serial permissions. Its `OPTIONS /api/diagnostics` snapshot reads cached control/camera state without expiring a lease, opening serial or consuming a commissioning record. Legacy servers reject OPTIONS without reading control status. The snapshot includes schema version `1` and startup process identity/configuration; it remains available with the camera disabled or unhealthy. Current servers also expose the snapshot through `GET /api/diagnostics`.

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

`dog.capabilities.actions` publishes each supported action's native `axis`, `sign`, `max_speed` and `speed_unit`; `dog.capabilities.max_duration` publishes the duration bound in seconds. These values come from the server's action definitions and velocity limits. `dog.limits` continues to describe the velocity endpoint. Capability metadata describes supported parameters, while `actions_enabled` describes current readiness.

While an action runs, the server continues checking fresh suitable feedback and accepts `motion_state` 0 or 1. Missing, stale or unsuitable feedback stops the action. The duration begins with the first velocity output; timing precision follows the existing 20 Hz writer. A second action or raw velocity request cannot replace an active action. Keep sending lease heartbeats until the action ends; lease expiry takes precedence over the requested duration. The browser and low-level `DevClient` require explicit acquisition, preparation and heartbeats. The opt-in `ManagedClient` handles those steps for each requested action.

`dog.action` is null before the first action. Thereafter it contains `id`, `name`, `speed`, `duration`, `phase`, `reason` and `remaining_s`. The phase is `running`, `completed`, `cancelled` or `fault`. Completion runs the bounded stop sequence and revokes ownership. Inspect the terminal phase and reason before proceeding. Further motion requires a new lease and explicit preparation. Reported completion describes the server's timed command lifecycle; physical stopping remains unverified.

Keep the returned action `id` to query it through `POST /api/dog/action/status` after another action starts or control changes hands. The lookup returns the active action or a retained terminal snapshot with the original ID, parameters, phase and reason. The process retains at most 128 terminal records for 300 seconds from each terminal transition, evicting the oldest completions first. Reads leave that retention deadline unchanged. Running actions remain queryable independently of these limits. Unknown, evicted or expired IDs return HTTP 404 with `code: "not_found"`; restarting the process clears history. The global `dog.action` summary continues to show the current or most recent action, including its terminal state after lookup retention expires.

An action lookup requires only the ID and leaves leases, control sequences, action timing and motion unchanged. The writer remains responsible for advancing deadlines and stopping motion. A lookup can briefly report `running` with `remaining_s=0` while the writer processes a due deadline.

Dog sends use the stock little-endian `<III` and `<IIId` formats. An independent worker maintains 20 Hz heartbeat/velocity output. Every hardware dog stop path, including release, expiry and shutdown, repeats ten bounded bursts. Each burst first attempts the vendor's voice cancellation (`0x21010C0A`, value `0`) to end the persistent voice stand action started by `dog/stand`, then heartbeat, zero forward/lateral/yaw and stationary packets. Each packet is attempted independently, including when cancellation fails. There is no UDP acknowledgement; a successful send does not prove execution or physical stop. This service does not send the vendor's separate soft-estop opcode, torque-release or posture-toggle commands.

Software emergency stop latches motion and requests cancellation. Dog hardware mode attempts the bounded zero-velocity and stationary burst. In the arm-only diagnostics configuration, cancellation may submit one bounded base or elbow hold if command history and feedback are still credible. OS stalls, process termination, power loss or controller failure can defeat software stopping. Retain the physical stop procedure and clear operating space. Command TTL is a server deadline. Physical stopping remains unverified.

## HTTP contract

All camera and control endpoints use anonymous trusted-LAN access. `GET /api/status` is public and returns `environment`, `mode`, latch, lease occupancy, dog phase, limits and arm capability. `dog.simulated` and `arm.simulated` identify simulated transport. The lease ID is omitted. Pass the opaque `safety_revision` observed by the operator to reset. Every emergency stop, newly latched transport fault and successful reset changes it, so a delayed reset cannot clear a newer latch. Every control POST requires `Content-Type: application/json`, one bounded Content-Length and an object body. Body limit is 4096 bytes. Browser requests with an Origin must match the server origin.

Arm status includes `connected`, `reconnect_required`, structured `commissioning` and
`cached_feedback`. Status queries use existing observations and preserve the one-use
nonce. The cache is `null` until a sample exists; samples include source, server monotonic
query time, age in seconds and freshness. Hardware positions remain separate from
simulation targets. Use `arm_state()` for an explicit live sample; its first commissioned
read consumes the nonce and can disarm diagnostics on failure or reference mismatch.
See [R5 status and profile tooling](R5_DIAGNOSTICS.md) for the state values and local helper.

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
| `/api/arm/action` | Dev simulation only. `{"lease":"...","seq":4,"targets":{"b":10,"e":-5},"duration":3}`; absolute degrees and positive seconds (at most 10). Returns control status with `arm.operation` immediately. |
| `/api/arm/state` | `{}`; explicit hardware serial query or simulated state |
| `/api/arm/reconnect` | `{}`; clear a failed read session and leave the transport unopened until the next read. Diagnostics require fresh commissioning after reconnect. |
| `/api/arm/operation` | `{"id":"<operation_id>"}`; read the current/latest matching simulated action or commissioned diagnostic without a lease. Unknown or superseded IDs return 404. |
| `/api/arm/stop` | `{}` for an observer stop, or `{"lease":"..."}` for the current owner. Requests cancellation, revokes the shared lease and returns control status. |

Success is HTTP 200; validation 400, origin 403, missing action 404, ownership conflicts 409, too-large body 413, safety latch/capability block 423, transport/unavailable 503. Error bodies contain `error`. POST requests are never automatically retried. A transport failure can mean an operation was accepted but the response was lost; reacquire only after inspecting status and taking appropriate stop action.

Control-service errors also carry an optional stable `code`: `invalid_request`, `not_found`, `conflict`, `locked` or `unavailable`. The HTTP adapter maps these to 400, 404, 409, 423 and 503. Other errors can contain only the `error` string. SDK callers can inspect `DevClientError.status`, `DevClientError.code` and the error message. A code is retained only when the protocol header and complete error envelope validate; missing, empty or untrusted codes yield `None`. Existing HTTP status and message fallback behavior is preserved.

### Protocol contract

Responses must advertise exactly one `X-Lite3-Dev-Protocol: 1` header. The Python SDK and browser client reject missing, empty, ambiguous or different protocol values. A failed check never replays a command. Update the server, SDK and browser assets together.

The Python SDK validates required response fields, nested objects, discriminators and finite numeric values before returning its typed dictionaries. Additional fields, including additional capability metadata, are retained. Unknown values of existing enums and missing or incorrectly typed required fields raise `DevClientError`; these changes require an updated contract. Action-ID lookups preserve ownership on validation failure. Validation failures from other requests clear ownership for the originating session, while a newer session remains intact. Keep wire-incompatible changes behind a new protocol version and update the SDK, browser client and contract tests together.

The passive arm fields are optional additions to protocol v1 for compatibility with
earlier servers. The SDK validates them when present and exports `ArmCommissioning`,
`ArmCachedFeedback` and `ArmObservationStatus`. Use `.get()` when supporting earlier servers.

`GET /api/status` returns a full `ControlStatus`, including when its mode is `disabled`. Control, dog and arm fields are required. An SDK release version is independent of the HTTP protocol version.

## Control CLI

Installing the SDK with `python -m pip install .` provides `lite3-control`. You can also run `python -m lite3_dev_server`. Both use Python 3.8+ and the standard library. `src/launcher.py` continues to provide deployment and SSH tunnels.

```bash
lite3-control --help
lite3-control --url http://127.0.0.1:8080
lite3-control --url http://127.0.0.1:8080 status
```

The default command reads `/api/status`. It acquires no lease and opens no R5 serial session. The default URL is `http://127.0.0.1:8080`. Common options can appear before or after the subcommand. To check a target explicitly, add `--transport simulated` or `--transport real`; a mismatch fails before acquisition or control requests.

Start the local `--env dev --simulate` server described above, then run a timed action:

```bash
lite3-control --transport simulated dog forward --speed 0.1 --duration 1 --dry-run
lite3-control --transport simulated dog forward --speed 0.1 --duration 1 --execute
lite3-control --transport simulated dog turn_left --speed 0.2 --duration 1 --execute
```

The CLI supports `forward`, `backward`, `turn_left` and `turn_right`. Supply positive speed and duration explicitly. Speed uses m/s for translation and rad/s for turns, with the limits in the table above. An executed command acquires one lease, starts stand preparation, waits for fresh standing readiness, submits one action and waits for its terminal result. Lease heartbeats continue through preparation and execution. Cancellation and faults return failure. Further motion requires a new invocation.

Motion requires both an explicit transport and the full `--execute` flag. Abbreviations such as `--exec` are rejected. `--dry-run` validates arguments and prints the proposed HTTP parameters and lifecycle requests without connecting, even if `--execute` is also supplied. Lease, sequence and operation-ID placeholders describe values that the SDK supplies during execution. Offline validation cannot establish live capabilities, measured readiness or successful physical movement.

R5 commands expose the existing feedback and bounded diagnostic APIs:

```bash
lite3-control --transport simulated arm jog e --delta 1 --speed 2 --execute
lite3-control --url http://127.0.0.1:8080 --transport real arm state
lite3-control --transport real arm operation OPERATION_ID
lite3-control --transport real arm jog e --delta 1 --speed 1 --dry-run
```

`arm state` explicitly requests serial feedback from a real target. Real jogs require the server's commissioned dev diagnostics, supported joint and capability limits, and the full `--execute` flag. The CLI polls an acknowledged hardware operation while renewing its lease. If a stop or expiry revokes the lease, it continues bounded read-only observation of that operation. A `commanded_unverified` result remains labeled that way; `stop_verified` and gripper feedback keep their server values. [Commissioning and hardware limits](R5_DIAGNOSTICS.md)

For recovery after a failed read session, run `lite3-control --transport real arm reconnect`. The next feedback read opens the transport. In a commissioned diagnostics session, reconnect attempts to consume the single-use profile and disarms motion, including when the session is healthy. Further hardware jogs require fresh commissioning.

`dog stop`, `arm stop` and `estop` are explicit observer operations and need no lease or `--execute`. Reset requires a revision obtained from status:

```bash
lite3-control dog stop
lite3-control arm stop
lite3-control estop
lite3-control --transport simulated reset --revision OBSERVED_SAFETY_REVISION --execute
```

Every command except help writes one JSON object to stdout. Success has `ok: true`, `command` and `result`; failures have `ok: false`, `command`, `kind` and `error`. A rejected HTTP request can also include `http_status`. Terminal motion failures include the returned state in `result`. Status and dog actions return `ControlStatus`; simulated jogs return their acknowledged `ControlStatus`, and hardware jogs return the terminal `ArmOperation`. The jog result is the operation snapshot; SDK cleanup then releases any remaining lease. Explicit arm queries return the corresponding SDK response. Private lease IDs are omitted. Diagnostics use stderr.

| Exit code | Meaning |
| --- | --- |
| `0` | Successful query, offline validation, or accepted terminal motion result |
| `2` | Invalid arguments, missing full execution flag, or missing motion transport |
| `1` | Transport mismatch, service rejection, failed/cancelled operation, timeout, or cleanup failure |
| `128 + signal` | Interrupted: SIGINT `130`, SIGTERM `143`, SIGHUP `129` where supported |

SIGINT (Ctrl+C), SIGTERM and supported SIGHUP handlers prevent subsequent commands and enter SDK lease cleanup. An in-flight SDK call returns before cleanup, allowing a successful acquisition to record its lease. Repeated signals leave cleanup running. The CLI restores the previous handlers on exit. Cleanup uses the SDK's lease-scoped release; communication errors can leave server lease expiry responsible for cancellation. JSON includes `cleanup_error` when release fails. Physical stopping retains the service's existing limitations.

`--timeout` sets the SDK's HTTP request budget, default 2 seconds and maximum 10. `--wait-timeout` sets a monotonic deadline for each preparation or operation wait, default 15 seconds and maximum 60. The CLI checks deadlines between calls and after responses; a late preparation response fails before action submission. Connection attempts, TLS, sends and response reads share each request's remaining budget. DNS resolution follows operating-system resolver timeouts. Pending calls can delay CLI deadline checks and signal cleanup. Release and a possible completion-confirmation read use the same SDK timeout. Neither flag extends server motion limits or retries a motion request.

## Release and build identity

`VERSION` declares the SDK release, starting with `0.2.0` for build provenance support. Each release increases this three-part version. The HTTP protocol remains `1` across compatible releases.

Built wheels and source distributions contain `_build.json`. It records the release, full source commit, whether the checkout had local changes, and a deterministic SHA-256 build identifier derived from the artifact's source bytes and provenance. Rebuilding a wheel from an unchanged source distribution preserves this information. Changes to its code or `VERSION` produce a new identity with unknown source provenance. Git is needed at build time to establish a source commit; installed wheels and deployed servers can read their metadata without Git.

Install a newer release with a normal upgrade, from its checkout or wheel:

```bash
python -m pip install --upgrade .
# Or install the wheel built from the desired release:
python -m pip install --upgrade dist/lite3_dev_server-0.2.0-py3-none-any.whl
python -c "import lite3_dev_server as sdk; print(sdk.__version__); print(sdk.BUILD_INFO)"
python -m pip show lite3-dev-server
```

`BUILD_INFO` is an immutable `BuildInfo` tuple with `version`, `source_commit`, `source_dirty`, and `build_id`. Source fields are `None` when provenance is unavailable. A checkout with changes records its base commit and `source_dirty=True`. The content identifier also distinguishes changes within that checkout. SDK and server bundles contain different files and have their own build identifiers.

Every service response produced by the application, including `/api/status`, `/healthz` and application errors, includes:

| Header | Meaning |
| --- | --- |
| `X-Lite3-Release` | Release version |
| `X-Lite3-Build` | `sha256:` followed by the build digest |
| `X-Lite3-Source-Commit` | Full source commit, or `unknown` |
| `X-Lite3-Source-Dirty` | `true`, `false`, or `unknown` |

Read the active listener through the local tunnel:

```bash
curl -sS -D - -o /dev/null http://127.0.0.1:18080/api/status
```

The launcher captures the deployment files and their manifest before connecting over SSH. The server verifies the deployed code and web assets against the manifest and fixes its identity at startup. An incomplete upload or later file modification causes a fresh process to report unknown identity. After deploying updated files, the existing process keeps reporting its original identity. An explicit restart loads the new build; for an installed systemd service, use `lite3-launcher service restart ysc@192.168.1.103 --expect-build sha256:YOUR_DEPLOYED_DIGEST`, then repeat the HTTP check. See [shared server lifecycle](CAMERA_STREAM.md#shared-server-lifecycle) for detached servers.

A source checkout can determine its identity using `VERSION`, Git and the source files at startup. Copies without Git report unknown source fields. Standalone files without a valid build manifest report `unknown` for all four headers. A source run and a deployment can contain different file sets and therefore have different content identifiers.

To prepare a release, update `VERSION` to a value greater than the previous release, run the checks below, and commit the release changes. Build from that clean commit with `python -m build`. Use a matching `v<version>` tag and distribute the wheel and source archive from that build. Every published version identifies one release; increasing `VERSION` makes subsequent releases eligible for `pip install --upgrade`. Development commits sharing a release version still require explicit reinstallation when replacing that same installed version.

## Python scripts

Install the SDK from the repository root on your development machine. It needs Python 3.8 or later and has no runtime dependencies beyond the standard library.

```bash
python -m pip install --upgrade .
```

The `lite3_dev_server` package exports `ManagedClient`, immutable `DogAction` and `ArmJog` values, `ActionResult`, `SequenceResult` and the `ManagedPhase` type alias. It also exports `DevClient`, `DevClientError`, the `Environment` and `ControlMode` type aliases, and typed JSON dictionaries for requests and responses. Dog action contracts include `DogActionName`, `DogActionRequest`, `DogActionStatusRequest`, `DogActionStatus` and `DogFeedback`; `DogStatus` includes `feedback`, `action` and `actions_enabled`. Use the installed `lite3_dev_server` package for Python callers. Robot deployment uses the standalone server files.

Use the installed `lite3-launcher` command or `src/launcher.py` for deployment and tunnels. Both use the bundled server modules, browser assets and remote helpers. The console loads its HTTP client from `/static/dev_client.js`; UI timers and local lease state remain in the control page.

### Managed actions and sequences

`ManagedClient` provides synchronous `run(action, timeout=30.0)` and `run_sequence(actions, policy='completed_only', timeout=60.0)`. Supply the sequence policy explicitly. Each call validates its arguments and target capabilities before acquiring control, then runs within a total monotonic deadline that includes waiting for ownership, preparation and execution. Run these examples against the local `--env dev --simulate` server above:

```python
from lite3_dev_server import DogAction, ManagedClient

with ManagedClient('http://127.0.0.1:8080') as robot:
    result = robot.run(DogAction('forward', speed=0.1, duration=1.0), timeout=15)
    if result.phase != 'completed':
        raise RuntimeError((result.phase, result.reason, result.cleanup_error))
```

The managed client acquires a private lease, prepares the dog, waits for ready state and fresh standing feedback, and renews the lease while polling the acknowledged action ID. After a dog action finishes, it waits for stop recovery. Each subsequent step acquires a fresh lease and prepares again:

```python
from lite3_dev_server import DogAction, ManagedClient

with ManagedClient('http://127.0.0.1:8080') as robot:
    result = robot.run_sequence(
        [DogAction('forward', duration=1.0), DogAction('turn_left', duration=0.5)],
        policy='completed_only',
        timeout=30,
    )
    print(result.phase)
    for step in result.results:
        print(step.action_id, step.phase, step.reason)
```

Only `completed` advances a sequence. `ActionResult.phase` can also be `cancelled`, `fault`, `timed_out`, `unknown` or `commanded_unverified`; all halt the sequence. An action result carries `action_id`, `reason`, the available full `status` and arm `operation` evidence, and any `cleanup_error`. Caller cancellation and deadline expiry describe the managed execution outcome; the preserved server evidence can still show an arm operation stopping. `SequenceResult.results` is the ordered tuple of attempted action results; a capability preflight failure returns `fault` with an empty tuple and a reason. Invalid static arguments raise `ValueError` before network access. Treat a lost or mismatched action ID, uncertain submission or transport failure as an unresolved outcome and inspect the result before deciding what to do next.

Use `ArmJog(joint, delta, speed=1.0)` for simulated targets or commissioned hardware diagnostics. For example, `robot.run(ArmJog('e', 1.0))` changes the simulated elbow target by one degree. Simulation applies the target update synchronously: its result is `completed` with no action ID or asynchronous operation. Sequence preflight checks cumulative simulated joint targets. Hardware arm actions use the advertised joint, speed and travel limits and the existing commissioning envelope; dog output must be isolated. A hardware `commanded_unverified` outcome halts the sequence. Software completion and observed arm feedback leave `stop_verified=false`; physical stopping remains unverified.

The constructor accepts `owner='python-managed-sdk'`, `request_timeout=0.5`, `poll_interval=0.05`, `heartbeat_interval=0.4` and `lease_policy='fail'`. Timing values must be finite and positive; request timeout is at most 1 second, polling and heartbeat intervals at most 0.5 seconds. Resolved connections, TLS and HTTP headers/bodies share each request's budget, including slowly arriving replies. Hostname resolution follows operating-system resolver timeouts; a numeric IP origin avoids that extra wait. Use `lease_policy='wait'` to wait for another controller within the same total deadline. Capabilities and the stop latch are checked again before every step and immediately before submission. The client stops and joins each lease renewal worker before advancing. Heartbeats leave raw velocity TTL unchanged.

`cancel()` signals the active run and can be called from another thread. `close()` cancels and waits for cleanup; exiting the context also closes the client. For a cancellable task:

```python
from concurrent.futures import ThreadPoolExecutor
from lite3_dev_server import DogAction, ManagedClient

with ManagedClient('http://127.0.0.1:8080') as robot:
    with ThreadPoolExecutor(max_workers=1) as workers:
        pending = workers.submit(robot.run, DogAction('forward', duration=5.0))
        try:
            result = pending.result()
        except KeyboardInterrupt:
            robot.cancel()
            result = pending.result()
        print(result.phase, result.cleanup_error)
```

Cleanup uses the saved token for that run, so a stale token cannot stop or release another owner's session. Joining an in-flight heartbeat and attempting release can extend return time past the execution deadline by up to two request timeouts. A heartbeat HTTP 409 permits a read of the same acknowledged ID to confirm terminal completion. Other transport failures halt execution. Commands are submitted once. Resetting a stop latch, reconnecting an arm session and deciding whether to retry an uncertain action remain explicit caller decisions. `KeyboardInterrupt` and other `BaseException` subclasses raised inside a managed run propagate after cleanup.

### Low-level lifecycle control

`DevClient` retains a private lease and serializes motion requests with increasing sequence numbers. Reads, heartbeats and stop/estop use separate HTTP requests and can run concurrently. Requests have bounded timeouts, refuse redirects and never retry motion. Responses and errors are scoped to their originating session. Superseded acquire or motion calls raise `DevClientError`. Call heartbeat while holding a lease, or use `wait_arm_operation()` to renew it during an arm operation wait.

### Target preflight and offline validation

Choose `DevClient(url, target="simulation")` for dev simulation or `DevClient(url, target="hardware")` for real device transport. Both use the service's `hardware` control mode. The SDK checks each requested device's `simulated` field; simulation requires `environment="dev"`, and arm jog always requires dev. Acquisition checks both device transport flags because its lease covers both devices. `DevClient(url)` keeps the existing request lifecycle, with local parameter validation added before motion requests.

With a target selected, acquisition checks status before requesting a lease and checks the returned status again. A rejected acquisition response triggers a release scoped to the returned lease. Each motion call fetches a fresh status, checks that the same local ownership session is still current, and applies the advertised limits before submitting a command. Stops and heartbeats remain independently callable. The server validates every submitted command against its current state.

`robot.preflight(operation)` sends only a status GET. Pass `status=state` to evaluate an already received snapshot without I/O. It returns `PreflightResult` with `ok`, `operation`, `target`, `status` and `reasons`, whose entries contain stable local `code` and readable `message` fields. `parameters={...}` additionally validates the complete motion parameters, excluding lease/sequence. Supported operations are `acquire`, `dog/stand`, `dog/velocity`, `dog/action`, `arm/jog` and `arm/action`.

Preparation and action readiness are separate checks. A disarmed dog with output enabled can start `dog/stand` before standing feedback exists. A timed action requires idle readiness, an owned active lease, enabled actions and fresh standing feedback matching the selected transport. `lease_active` alone cannot establish ownership. Older service responses remain decodable; guarded timed actions require the new `dog.capabilities` metadata.

Common reason codes include `transport_mismatch`, `environment_mismatch`, `control_disabled`, `control_readonly`, `estop_latched`, `control_error`, `capability_unavailable`, `lease_busy`, `lease_required`, `dog_not_disarmed`, `dog_not_ready`, `feedback_unavailable`, `action_running`, `arm_busy`, `stop_in_progress` and `invalid_parameters`. Automatic status/capability blockers raise `PreflightError`, which subclasses `DevClientError` and exposes the decision as `.result`. Its `.status` and `.code` are `None`; local reason codes live in `.result["reasons"]`. Invalid offline motion parameters raise `DevClientError` before any request. Parameter and capability blockers preserve a live local lease. In target mode, fresh status confirming `lease_active=false` clears that session's expired token; reacquisition remains explicit. Public `preflight("acquire")` also blocks an existing or pending local acquisition.

SDK and CLI callers share the exported pure functions `validate_dog_action(name, speed, duration)`, `validate_velocity(vx=0, vy=0, yaw=0, ttl=0.3)` and `validate_arm_jog(joint, delta, speed=2)`. They raise `ValueError` for unsupported directions/joints, booleans, non-finite numbers and out-of-range values. `validate_dog_action` also returns the native velocity mapping. Use their optional `limits`, `capabilities` or `position_deg` arguments to apply service bounds; these may tighten the shipped offline limits. Diagnostic arm capabilities additionally apply the 0.1° minimum and five-second travel limit. `check_preflight(state, operation, target, owns_lease=False, parameters=None)` exposes the same pure readiness checks for CLI consumers. Use `owns_lease=True` to check an acquired response or owned motion; prospective acquisition uses `False`.

```python
from lite3_dev_server import DevClient, validate_dog_action

# Suitable for CLI argument checking before opening a connection.
validate_dog_action("forward", speed=0.1, duration=1.0)

with DevClient("http://127.0.0.1:8080", target="simulation") as robot:
    decision = robot.preflight("acquire")
    print(decision["ok"], decision["reasons"])
```

### Timed action example

`dog_action(name, speed, duration)` returns `ControlStatus` immediately. Convenience methods `forward(speed=0.1, duration=1.0)`, `backward(speed=0.1, duration=1.0)`, `turn_left(speed=0.2, duration=1.0)` and `turn_right(speed=0.2, duration=1.0)` use the same endpoint. Translation speeds are m/s, turn speeds are rad/s, and durations are seconds. Each method accepts a positive speed. The following simulation example keeps the lease alive throughout preparation and execution:

```python
import time
from lite3_dev_server import DevClient, DevClientError

with DevClient('http://127.0.0.1:8080', target='simulation') as robot:
    robot.acquire('offline-demo')
    state = robot.stand()
    deadline = time.monotonic() + 10
    while not robot.preflight('dog/action', status=state)['ok']:
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

In commissioned arm diagnostics, `arm_jog()` returns `ControlStatus` with `arm.operation`. Keep the operation's `operation_id` and call `arm_operation(id)` to read its `ArmOperation` status. Send heartbeats while it runs, or call `wait_arm_operation(id, timeout=10)` to poll and renew your current lease. `arm_stop()` cancels the operation and releases ownership. Inspect `arm.capabilities` for the diagnostic limits and `arm.motion_enabled` for current readiness. [R5 operation lifecycle](R5_DIAGNOSTICS.md#operation-lifecycle)

### Simulated arm actions and waiting

With `--env dev --simulate`, one `arm_action(targets, duration)` call submits absolute joint targets in degrees. `targets` must contain at least one of `b`, `s`, `e`, `w`, `h`. Their respective limits are ±180°, ±90°, ±90°, ±90° and ±45°. The server validates the complete group and the duration before accepting it. Duration must be finite, greater than zero and at most 10 seconds. Invalid members reject the whole action without changing targets or consuming its sequence.

The watchdog linearly advances all supplied targets from their starting values at its 50 ms cadence; omitted axes retain their targets. The accepted response includes `arm.operation.operation_id`. Its `SimulatedArmOperation` reports `kind="simulated_targets"`, `simulated=true`, phase, reason, duration, `elapsed_s`, fractional `progress`, and full `start_position_deg`, `target_position_deg` and `position_deg` maps. These values describe simulated targets. `stop_verified` remains false. `ArmOperation` is the union of this shape and the existing `ArmDiagnosticOperation` shape.

A running action rejects another arm action or manual arm jog with HTTP 409. Arm stop, dog stop, estop, release, lease expiry and shutdown cancel subsequent updates. The latest operation retains its terminal snapshot until another action is accepted. Normal completion retains the lease so another arm action can follow.

```python
from lite3_dev_server import DevClient, DevClientTimeout

with DevClient('http://127.0.0.1:8080') as robot:
    robot.acquire('simulated-arm')
    accepted = robot.arm_action({'b': 10, 'e': -5, 'h': -3}, duration=3)
    operation = accepted['arm']['operation']
    assert operation is not None
    try:
        result = robot.wait_arm_operation(operation['operation_id'], timeout=5)
    except DevClientTimeout as error:
        print('Wait expired:', error.operation_id, error.operation)
        robot.arm_stop()
    else:
        print(result['phase'], result['reason'])
```

`wait_arm_operation(id, timeout=30, poll_interval=0.1, renew_lease=True, heartbeat_interval=0.5)` reads only that ID and returns `completed`, `fault`, `cancelled` or `commanded_unverified` unchanged. A diagnostic gripper result retains `commanded_unverified` and its unknown measured position. Callers should inspect the returned phase. All time arguments must be finite and positive; renewal intervals are limited to one second. An observer can wait without acquiring control, and `renew_lease=False` disables renewal.

Renewal uses only the lease held when waiting begins. A local session change disables further renewal by that wait, which continues observing its original ID. If a heartbeat reports that ownership was revoked, the wait continues observing that ID without renewal. A confirmed cancellation of the operation acknowledged in the current session clears its local lease, allowing normal context cleanup. Observing an older session's cancelled operation preserves a newer lease.

Polling and renewal run synchronously, with sleeps and HTTP socket operations bounded by the remaining wait budget, including repeated receives for slowly delivered headers or bodies. No background renewal survives a return or exception. `DevClientTimeout` subclasses `DevClientError` and `TimeoutError`, and provides `operation_id` plus `operation`, the last validated snapshot for that ID or `None`. A wait timeout retains the local lease for explicit stop/release; the server expires it if no caller continues heartbeats. Other network failures use the existing `DevClientError` rules and can clear the originating session's local lease. Neither waiting nor timeout resubmits motion.

## Simulation in Python tests

The installed `lite3_dev_server_tools.testing` module owns a local server process for each context. Use its URL with the existing SDK in plain Python, unittest or pytest:

```python
from lite3_dev_server import DevClient
from lite3_dev_server_tools.testing import simulated_server

with simulated_server() as url:
    with DevClient(url) as robot:
        assert robot.status()['dog']['simulated']
        robot.acquire('integration-test')
        robot.arm_jog('e', 1)
        assert robot.arm_state()['position_deg']['e'] == 1
```

Each server binds `127.0.0.1` with port zero. The context reads the server's actual URL, then checks `GET /api/status` and the simulation headers before yielding it. `/healthz` describes camera readiness and can return 503 during camera-free simulation. Multiple contexts can run at the same time with separate ports and state. HTTP checks bypass proxy environment settings.

`simulated_server(startup_timeout=10.0, shutdown_timeout=5.0)` accepts finite, positive timeouts in seconds. Startup exit or timeout raises `RuntimeError` with captured stdout and stderr. Context exit terminates the child and waits for it; if the shutdown timeout expires, it kills and reaps the child. Cleanup also runs when the test raises an exception or receives `KeyboardInterrupt`. Close SDK contexts inside the simulation context so their release requests finish before server shutdown.

The context selects dev simulation without camera capture, serial access or SSH. Test the same API parameters and action sequences as your application. Dog stand preparation, leases, heartbeat requirements and action limits still apply.

## Verification

`python3 -B scripts/check.py --shell` runs offline syntax, unit and integration checks. `make browser-test` includes viewer and dev console behavior. Fakes and loopback cover UDP, serial, SSH and control lifecycle checks.

Dog feedback decoding and predefined actions have offline coverage only. Robot upload configuration, firmware compatibility, physical direction, motion timing and stopping still require hardware acceptance.
For package acceptance, install `.[dev]` in a development virtual environment and run `python -B scripts/check_distribution.py`. It checks SDK and context types, service return types, source-distribution and wheel provenance, a normal pip upgrade from a `0.1.0` fixture, and an external typed consumer. An empty environment installs the tools with package-index access disabled. It exercises the installed console commands, launcher dry runs, browser assets, the Python context and the packaged deployment bundle. `node tests/dev_client_test.js` checks browser command serialization, errors and protocol enforcement. Use Python 3.8–3.12 for `python -B scripts/check.py --device-syntax`; this parses every deployed Python module with the device's Python 3.6 grammar. Device runtime and physical motion acceptance remain separate checks.
