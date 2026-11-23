# Contributing

Submit issues with steps to reproduce the problem. Keep each pull request limited to a defined change. Development requires macOS or Linux and Python 3.8 or later. The local demo and unit tests need no third-party Python packages.

## Development procedure

1. Create a branch from `main`, such as `feat/camera-status` or `fix/stream-timeout`.
2. Keep `scripts/lite3_camera.py` and `scripts/lite3_camera_server.py` directly executable. The server must remain deployable as one file.
3. For behavior changes, add tests for the changed behavior. For documentation and configuration changes, run the applicable checks.
4. Before you commit, run `make check`.
5. If you change the browser page or MJPEG behavior, run `make browser-test`.
6. In the pull request, describe the change and its test results. State whether you used real hardware.

To select a Python interpreter, use `make check PYTHON=/path/to/python3`. The browser test uses a temporary profile and a random loopback port. It does not use your existing browser profile.

The launcher must use Python 3.8-compatible syntax. The server must use Python 3.6-compatible syntax. The `make check` command checks Python 3.8 syntax in the scripts and tests. CI also runs `make syntax-legacy` with Python 3.8 to check the server against Python 3.6 syntax. A syntax check does not confirm runtime operation on an older Python version.

Keep camera imports inside the functions that need them. This lets the demo and tests run without camera dependencies.

## Test limits

CI checks Linux, macOS, Python compatibility, and playback in Chrome. It does not run hardware tests. Report synthetic frames, simulated devices, and real camera results separately.

Four `native-depth` tests in `tests/test_lite3_camera_server.py` specify an unimplemented feature. Each has an explicit `unittest.skip` reason. Do not count skipped tests as passed tests. After you implement the feature, remove its skip markers.

Use only camera devices that the operator authorizes. Do not stop vendor services to show a camera preview. Do not send motion commands. Use an existing ROS image topic when one is available.

In hardware records, include the environment, device, commands, results, and unverified behavior. Do not commit passwords, private keys, or images from the test site.

## Writing rules

Use English for documentation, comments, help text, messages, and contribution templates. Apply [ASD-STE100 Simplified Technical English](https://www.asd-ste100.org/assets/files/ASD-STE100_ISSUE9.pdf) and the no-ai-slop editing rules.

- Use dictionary words with their approved meanings and parts of speech.
- Use technical nouns and verbs as permitted by the standard.
- Use short sentences and active voice.
- Keep instructions to 20 words per sentence and descriptions to 25 words per sentence.
- Give one instruction per sentence.
- Use the same term for the same item.
- Use direct verbs. Remove filler, repeated claims, and decorative emphasis.
- Keep technical names, commands, API fields, and measurements accurate.
- Keep license text unchanged.

Use these technical terms consistently:

| Term | Meaning |
| --- | --- |
| Perception host | The computer on the robot that provides camera access |
| Launcher | `scripts/lite3_camera.py`, which runs commands and starts the SSH tunnel |
| Server | `scripts/lite3_camera_server.py`, which captures frames and serves HTTP responses |
| Viewer | The browser page that shows the video stream |
| Backend | The camera access method: ROS, RealSense, or V4L2 |
| Frame age | The time since the server captured the latest frame |
| Video delay | The time from camera capture to display in the browser |

## Commit rules

Use [Conventional Commits](https://www.conventionalcommits.org/en/v1.0.0/) for commit messages. Examples:

```text
feat: add camera status indicator
fix: release video device on shutdown
docs: clarify V4L2 setup
ci: expand Python compatibility checks
```

The `.superpowers/**` directory contains local work files. Keep these files ignored. Do not commit them or include them in a pull request. Do not create `docs/superpowers/`.

Save design specifications in `.superpowers/specs/`. Save implementation plans in `.superpowers/plans/`. Before each commit, inspect `git diff --cached --name-only`. If the list contains `.superpowers/**` or `docs/superpowers/**`, remove those paths from staging. Do not use `git add -f` to bypass these rules.
