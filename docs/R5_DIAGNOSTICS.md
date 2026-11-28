# R5 diagnostics

The default hardware arm path provides explicit read-only feedback. Current hardware verification covers read-only feedback with the motion profile disabled. Physical jog response, gripper travel, holding and stopping remain unverified.

## Passive status and live sampling

`GET /api/status` and `DevClient.status()` expose `arm.connected`, `arm.reconnect_required`,
`arm.commissioning` and `arm.cached_feedback`. These queries inspect in-memory state and
leave serial/SSH unopened and the commissioning nonce untouched. Lease expiry and
watchdog cancellation continue to run.

`connected` records a successful feedback exchange on the current transport. A cold
gateway reports `false` until an explicit read succeeds. It is a local connection
snapshot; a subsequent read can detect a controller or worker failure.
`reconnect_required` identifies a latched read/transport fault. Calling
`arm_reconnect()` clears that latch, retires the transport and keeps diagnostics disarmed.

`commissioning` contains `state`, `profile_loaded` and `nonce_consumed`:

| State | Meaning |
| --- | --- |
| `unavailable` | This control service has no arm gateway. |
| `simulated` | Joint values are simulation targets. |
| `readonly` | Hardware observations are available; commissioned motion is unavailable. |
| `pending_preflight` | A valid profile is loaded; the five-sample motion preflight has yet to pass. |
| `ready` | A motion preflight passed in this gateway session. Every later jog still runs its own preflight. |
| `recommission_required` | The profile was previously consumed, a reference check failed, a diagnostic fault occurred, or reconnect disarmed the session. |
| `closed` | The gateway or control service has closed. |

`nonce_consumed` records consumption known to this gateway. Another process can claim
the same profile after it was loaded; the atomic claim at the next live read or jog
will detect that conflict. `motion_enabled` retains its admission meaning: a loaded
profile can admit a jog whose preflight subsequently fails. The status snapshot grants
no lease and provides no guarantee of physical readiness.

`cached_feedback` is `null` until a validated observation exists. Afterwards it contains
four measured `position_deg` axes, `observed: true`, `gripper_position_deg: null`,
`source` (`local_serial` or `ssh_serial`), `sample_monotonic`, `age_s` and `fresh`.
The timestamp is the query start on the server's monotonic clock, including worker
startup for the read-only gateway. Use `age_s` across hosts; the timestamp has no wall
clock or controller-clock meaning. `fresh` requires a usable connection and age at
most 0.25 seconds. Faults and reconnect preserve the last sample as historical data.
Reference mismatches can appear in the cache while commissioning reports
`recommission_required`. Freshness describes feedback age and connection state;
commissioning and motion capability must be checked separately.

Hardware `arm.position_deg` stays `null`. Simulation uses that field for five-axis
targets, with `simulated: true` and `cached_feedback: null`.

```python
arm = robot.status()['arm']  # Passive; safe to poll for an existing sample.
commissioning = arm.get('commissioning')
sample = arm.get('cached_feedback')
if sample is not None:
    print(sample['position_deg'], sample['age_s'], sample['fresh'])

# Explicitly requested live observation while the gateway is idle:
observed = robot.arm_state()
```

The first commissioned `arm_state()` call claims the one-use nonce before serial I/O
and checks the measured reference. An idle read leaves the five-sample preflight
pending. Read failures and reference mismatches retain the existing disarming behavior.

## Commissioning record

`--arm-motion-profile PATH` is an explicit opt-in for a separately commissioned device. It requires `--env dev`, `hardware` control mode and real transport to the local Linux serial device. `--simulate` and `--arm-host` are incompatible with the profile. `PATH` refers to a file already on the server host. Profile mode suppresses all dog UDP output, including shutdown stop packets. The service can still provide camera streaming. For routine observation, use the read-only R5 feedback available in `dist`.

The record is a JSON object with these fields. Angle values are finite numbers in degrees. Verify all five commanded targets, all four observations and the gripper reference independently in the same uninterrupted controller session. The server does not infer commanded targets from `T105`, commission the gripper automatically or invoke firmware homing.

| Field | Required value |
| --- | --- |
| `version` | Integer schema version `1` |
| `machine_id` | Current Linux machine ID from `/etc/machine-id` |
| `boot_id` | Current Linux boot ID from `/proc/sys/kernel/random/boot_id` |
| `device` | Exact configured local `--arm-device` path |
| `nonce` | Fresh nonempty identifier for this commissioning record |
| `targets_deg` | Independently verified controller targets `b`, `s`, `e`, `w`, `h` |
| `observed_deg` | Independently verified feedback references `b`, `s`, `e`, `w` |
| `gripper_reference` | Nonempty description of how the current gripper target was established |

Use a fixed, private commissioning directory. The record must be a regular file and both file and directory must be owned by the effective service user or root, with group and world write access disabled. Avoid symlinks for the file and its containing directory. Give the service user permission to create the marker in that directory. Keep the directory and its parents protected from replacement by other users.

The server records a nonce claim in `.r5-used-<nonce-SHA-256>` alongside the profile before the first commissioned serial read or jog. An explicit reconnect also attempts to spend a pending profile and leaves motion disarmed. A used record cannot arm a new server instance in that directory. Moving, copying or restoring the record without its marker bypasses this check. Keep the record and marker together in the same protected commissioning directory. Renewing or reacquiring a lease uses the original travel anchors for the running gateway. If the controller resets or command history becomes uncertain, discontinue diagnostics and establish new references in a separate commissioning session.

### Local profile helper

From a repository checkout on the Linux arm host, run the helper as the intended service
user. Create a private directory first, then generate a new template:

```bash
mkdir -m 700 -p "$HOME/r5-commissioning"
python3 scripts/arm_profile.py template "$HOME/r5-commissioning/profile.json" \
  --device /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
```

The helper fills the current machine ID, boot ID, exact device path and a fresh nonce.
It creates a mode-0600 file and refuses to overwrite an existing path. All five target
angles, four observed reference angles and the gripper reference start as `null`.
Fill them only after independent operator confirmation in the same uninterrupted
controller session. Then validate the completed file:

```bash
python3 scripts/arm_profile.py check "$HOME/r5-commissioning/profile.json" \
  --device /dev/serial/by-id/usb-1a86_USB_Serial-if00-port0
```

`check` reuses the server's profile validation and checks local identity, boot session,
device metadata, device access permissions and the ability to create a marker in the
profile directory. It rejects consumed profiles. Both commands inspect files and
metadata without opening the device, sampling feedback or creating a nonce marker.
A successful check reports `pending_preflight`. Operator reference confirmation and
the server's action-time serial preflight remain required. Replacing a nonce alone
cannot establish a new commissioning session.

## Diagnostic limits

| Limit | Value |
| --- | --- |
| Supported jog axes | Base `b`, elbow `e`, gripper `h` |
| Absolute jog magnitude | At least 0.1°. At most 0.5° for `b` and `h`, or 2° for `e` |
| Requested speed | Greater than zero and at most 1°/s |
| Operation budget | `3 s + abs(delta) / speed`, at most 8 s |
| Travel from the commissioned target | At most ±3° per joint across all operations and leases |
| Travel from the observed reference | At most ±3° per measured joint |
| Target-to-observation reference difference | At most 1° on `b`, `s` and `w`, and 5° on `e` |

The commissioned targets must also fit the absolute bounds of ±180° for `b`, ±90° for `s`, `e` and `w`, and ±45° for `h`. Preflight checks five observations against the separate measured references before any target write. Each target message keeps the other joints at their established targets. The base and elbow advance in increments up to 0.25° with a bounded lead over credible feedback. The gripper receives at most one advancing target and reports `commanded_unverified` if the four observable joints pass the subsequent checks. Its `gripper_position_deg` stays `null`. The requested speed and travel limits are software checks. Actual speed, gripper motion and physical response have yet to be measured.

The `robo-comp` 180° rotation remains outside this diagnostic envelope. The bounded API does not complete a hardware migration for that workflow.

## Operation lifecycle

Before any submission, check that `/api/status` reports `environment: "dev"` and inspect `arm.motion_enabled`, `arm.capabilities` and `dog.motion_enabled`. The diagnostics capability lists supported joints, delta/speed/travel ceilings, `asynchronous: true`, `gripper_feedback: false` and `stop_verified: false`. Readiness can change after that snapshot. Each lease lasts two seconds. A jog admits one operation and returns its status promptly. A second jog is rejected while the gateway is busy.

With an explicitly acquired lease, `DevClient.arm_jog(joint, delta, speed)` returns `ControlStatus`. Use the `operation_id` from `reply['arm']['operation']` to poll `DevClient.arm_operation(operation_id)`. Send `DevClient.heartbeat()` regularly during preflight and motion. The server exposes the active operation or, when idle, its most recent operation. Use `arm_state()` for an explicit feedback query while the gateway is idle.

Call `DevClient.arm_stop()` to request cancellation. This revokes the lease and returns updated `ControlStatus`. Continue polling the operation ID to see whether a hold was submitted and how the operation ended. Release, lease expiry, emergency stop and service shutdown also cancel active diagnostics. A `cancelled` or `fault` result carries a reason when available. Connection and feedback faults disarm diagnostics and require fresh commissioning before another jog. Explicit reconnect can restore read-only access.

The operation record keeps the measured pose samples separate from `targets_written`, which lists target messages fully submitted to the serial port. `hold_status` records `not_needed`, `requested`, `written` or `unavailable`. After cancellation, lease expiry or close, the gateway may submit one bounded base or elbow hold within its cancellation window if the previous write and fresh feedback remain credible. A partial write or invalid feedback prevents another write. `observed_stationary` describes sampled joints, and `stop_verified` stays `false` even when a hold was submitted. The controller can continue pursuing the last submitted target if the process dies.

`T1051` feedback has no query nonce or controller timestamp. The gateway flushes buffered input, checks local query timing and compares repeated samples. Delayed valid-looking feedback can still look like the response to a fresh query. Retain a physical stop procedure. Physical motion and stopping require measurement in a separate hardware acceptance session.
