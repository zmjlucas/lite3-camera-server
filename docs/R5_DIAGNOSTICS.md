# R5 diagnostics

The default hardware arm path provides explicit read-only feedback. Current hardware verification covers read-only feedback with the motion profile disabled. Physical jog response, gripper travel, holding and stopping remain unverified.

## Commissioning record

`--arm-motion-profile PATH` is an explicit opt-in for a separately commissioned device. It requires `--control-mode hardware`, the local Linux serial device and no `--arm-host`. `PATH` refers to a file already on the server host. Profile mode suppresses all dog UDP output, including shutdown stop packets. The service can still provide camera streaming. Leave the flag absent for routine observation.

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

Before any submission, inspect `arm.motion_enabled`, `arm.capabilities` and `dog.motion_enabled` in `/api/status`. The diagnostics capability lists supported joints, delta/speed/travel ceilings, `asynchronous: true`, `gripper_feedback: false` and `stop_verified: false`. Readiness can change after that snapshot. Each lease lasts two seconds. A jog admits one operation and returns its status promptly. A second jog is rejected while the gateway is busy.

With an explicitly acquired lease, `DevClient.arm_jog(joint, delta, speed)` returns `ControlStatus`. Use the `operation_id` from `reply['arm']['operation']` to poll `DevClient.arm_operation(operation_id)`. Send `DevClient.heartbeat()` regularly during preflight and motion. The server exposes the active operation or, when idle, its most recent operation. Use `arm_state()` for an explicit feedback query while the gateway is idle.

Call `DevClient.arm_stop()` to request cancellation. This revokes the lease and returns updated `ControlStatus`. Continue polling the operation ID to see whether a hold was submitted and how the operation ended. Release, lease expiry, emergency stop and service shutdown also cancel active diagnostics. A `cancelled` or `fault` result carries a reason when available. Connection and feedback faults disarm diagnostics and require fresh commissioning before another jog. Explicit reconnect can restore read-only access.

The operation record keeps the measured pose samples separate from `targets_written`, which lists target messages fully submitted to the serial port. `hold_status` records `not_needed`, `requested`, `written` or `unavailable`. After cancellation, lease expiry or close, the gateway may submit one bounded base or elbow hold within its cancellation window if the previous write and fresh feedback remain credible. A partial write or invalid feedback prevents another write. `observed_stationary` describes sampled joints, and `stop_verified` stays `false` even when a hold was submitted. The controller can continue pursuing the last submitted target if the process dies.

`T1051` feedback has no query nonce or controller timestamp. The gateway flushes buffered input, checks local query timing and compares repeated samples. Delayed valid-looking feedback can still look like the response to a fresh query. Retain a physical stop procedure. Physical motion and stopping require measurement in a separate hardware acceptance session.
