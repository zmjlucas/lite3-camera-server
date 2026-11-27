"""Check client commands with portable SSH/SCP substitutes; no robot connections."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "src/client.py"
RUNNER = ROOT / "tests/fixtures/client_runner.py"


class CameraCommandsTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "The camera launcher script is missing")
        spec = importlib.util.spec_from_file_location("camera_cli", SCRIPT)
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)
        original_popen = subprocess.Popen

        def start(command, *args, **kwargs):
            if command[0] in ("ssh", "scp"):
                command = [sys.executable, str(RUNNER), "--ssh-stub"] + list(command)
            return original_popen(command, *args, **kwargs)

        substitute = patch.object(self.cli.subprocess, "Popen", start)
        substitute.start()
        self.addCleanup(substitute.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.log = self.directory / "calls.jsonl"
        self.env = dict(os.environ, CAMERA_TEST_LOG=str(self.log), PYTHONDONTWRITEBYTECODE="1")

    def run_cli(self, *args, exit_code=0):
        args = list(args)
        if args[0] == "view" and "--dry-run" not in args and "--local-port" not in args:
            args.extend(["--local-port", str(self.free_port())])
        env = dict(self.env, CAMERA_TEST_EXIT=str(exit_code))
        return subprocess.run([sys.executable, str(RUNNER), str(SCRIPT)] + args, env=env,
                              capture_output=True, text=True, timeout=8)

    def assert_process_stopped(self, pid):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, pid)  # SYNCHRONIZE
            if not handle:
                self.assertEqual(ctypes.get_last_error(), 87)  # Process no longer exists.
                return
            try:
                self.assertEqual(kernel.WaitForSingleObject(handle, 0), 0, "SSH child still running")
            finally:
                kernel.CloseHandle(handle)
        else:
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)

    def symlink(self, path, target):
        try:
            path.symlink_to(target)
        except OSError as error:
            if os.name == "nt" and error.winerror == 1314:
                self.skipTest("Windows account lacks symlink privilege; junction tests run separately")
            raise

    @staticmethod
    def free_port():
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_dry_run_performs_no_network_commands(self):
        result = self.run_cli("view", "ysc@192.168.1.103", "--backend", "realsense", "--depth", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.calls(), [])
        self.assertIn("127.0.0.1:18080:127.0.0.1:8080", result.stdout)
        self.assertIn("--depth", result.stdout)

    def test_bad_targets_rejected_before_process_launch(self):
        for target in ("host;touch /tmp/x", "user@host$(id)", "-oProxyCommand=id", "user name@host"):
            result = self.run_cli("doctor", "--", target)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_optional_jump_reaches_all_remote_commands(self):
        cases = (("doctor", [], ["ssh"]),
                 ("deploy", [], ["ssh"] + ["scp"] * 6 + ["ssh"]),
                 ("view", ["--backend", "ros", "--no-open"], ["ssh"]),
                 ("view", ["--backend", "realsense", "--no-open"], ["ssh"]),
                 ("view", ["--backend", "v4l2", "--device", "/dev/video4", "--no-open"], ["ssh"]))
        for jump in (None, "ysc@192.168.2.1", "robot-gateway"):
            for command, options, executables in cases:
                with self.subTest(jump=jump, command=command, options=options):
                    previous = len(self.calls())
                    jump_options = [] if jump is None else ["--jump", jump]
                    result = self.run_cli(command, "ysc@192.168.1.103", *options, *jump_options)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    calls = self.calls()[previous:]
                    self.assertEqual([call[0] for call in calls], executables)
                    for call in calls:
                        if jump is None:
                            self.assertNotIn("-J", call)
                        else:
                            self.assertEqual(call.count("-J"), 1)
                            self.assertEqual(call[call.index("-J") + 1], jump)
                            destination = ("ysc@192.168.1.103" if call[0] == "ssh" else
                                           call[-1])
                            self.assertLess(call.index("-J"), call.index(destination))
                        self.assertNotIn("StrictHostKeyChecking=no", call)

    def test_bad_jump_rejected_before_process_launch(self):
        for command in ("doctor", "deploy", "view"):
            for jump in ("", "host;touch /tmp/x", "user@host$(id)", "-oProxyCommand=id",
                         "user name@host", "user@host:22", "first,second"):
                with self.subTest(command=command, jump=jump):
                    options = ["--no-open", "--local-port", "18080"] if command == "view" else []
                    result = self.run_cli(command, "robot", "--jump=" + jump, *options)
                    self.assertNotEqual(result.returncode, 0)
                    self.assertIn("argument --jump", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_deploy_creates_directory_then_copies_server_and_runtime_assets(self):
        result = self.run_cli("deploy", "robot")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], ["ssh"] + ["scp"] * 6 + ["ssh"])
        self.assertIn("mkdir -p", calls[0][-1])
        for command in calls[1:]:
            self.assertNotIn("-r", command)
        self.assertEqual(Path(calls[1][-2]).name, "server.py")
        self.assertEqual(calls[1][-1], "robot:lite3-camera-server/")
        self.assertEqual(Path(calls[2][-2]).name, "camera.html")
        self.assertEqual(calls[2][-1], "robot:lite3-camera-server/web/")
        self.assertFalse(Path(calls[1][-2]).exists(), "Temporary deployment files must be removed")
        self.assertIn("-tt", calls[-1])  # Allow sudo to prompt on the perception host.
        import shlex
        install = shlex.split(calls[-1][-1])
        self.assertEqual(install[:2], ["bash", "-lc"])
        self.assertEqual(install[5:], ["--source", "realsense", "--port", "8080",
                                      "--width", "640", "--height", "480",
                                      "--fps", "15", "--quality", "70"])

    def test_deploy_passes_v4l2_boot_settings_to_installer(self):
        import shlex
        result = self.run_cli("deploy", "robot", "--backend", "v4l2", "--device", "/dev/video4",
                              "--remote-port", "8081", "--width", "1280", "--fps", "10")
        self.assertEqual(result.returncode, 0, result.stderr)
        install = shlex.split(self.calls()[-1][-1])
        self.assertEqual(install[5:], ["--source", "v4l2", "--port", "8081",
                                      "--width", "1280", "--height", "480",
                                      "--fps", "10", "--quality", "70", "--device", "/dev/video4"])

    def test_deploy_rejects_invalid_service_settings_before_connecting(self):
        for options in (("--backend", "ros"), ("--backend", "v4l2"),
                        ("--backend", "v4l2", "--device", "/dev/video4", "--depth"),
                        ("--device", "/dev/video4"), ("--remote-port", "0")):
            result = self.run_cli("deploy", "robot", *options)
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    def test_native_view_prints_plain_local_url(self):
        port = self.free_port()
        result = self.run_cli("view", "robot", "--backend", "realsense", "--no-open", "--local-port", str(port))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Viewer: http://127.0.0.1:{}/".format(port), result.stdout)
        self.assertNotIn("#token=", result.stdout)

    def test_native_view_does_not_require_a_session_handshake(self):
        self.env["CAMERA_TEST_BANNER"] = "http://127.0.0.1:8080/"
        result = self.run_cli("view", "robot", "--backend", "realsense", "--no-open")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_deploy_failure_prevents_copy(self):
        result = self.run_cli("deploy", "robot", exit_code=23)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(len(self.calls()), 1)

    def test_failed_upload_prevents_service_installation(self):
        with patch.object(self.cli.subprocess, "call", side_effect=[0, 0, 23]) as remote:
            self.assertEqual(self.cli.main(["deploy", "robot", "--direct"]), 23)
        self.assertEqual(remote.call_count, 3)

    def test_service_installation_failure_reaches_caller(self):
        with patch.object(self.cli.subprocess, "call", side_effect=[0] * 7 + [23]) as remote:
            self.assertEqual(self.cli.main(["deploy", "robot", "--direct"]), 23)
        self.assertEqual(remote.call_count, 8)

    def test_deploy_disambiguates_single_letter_ssh_alias_from_windows_drive(self):
        result = self.run_cli("deploy", "r", "--direct")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual(calls[0][-2], "r")
        self.assertEqual([command[-1] for command in calls if command[0] == "scp"],
                         ["[r]:lite3-camera-server/", "[r]:lite3-camera-server/web/",
                          "[r]:lite3-camera-server/", "[r]:lite3-camera-server/",
                          "[r]:lite3-camera-server/remote/", "[r]:lite3-camera-server/web/"])

    def test_deploy_refuses_symlinked_assets_before_remote_commands(self):
        import contextlib
        import io
        from unittest.mock import patch
        server = self.directory.resolve() / "server.py"
        page = self.directory.resolve() / "camera.html"
        private = self.directory.resolve() / "private.txt"
        server.write_text("runtime")
        private.write_text("synthetic-secret")
        self.symlink(page, private)
        bundle = ((server, "lite3-camera-server/"), (page, "lite3-camera-server/web/"))
        with patch.object(self.cli, "SERVER_BUNDLE", bundle), patch.dict(os.environ, self.env), \
                contextlib.redirect_stderr(io.StringIO()):
            result = self.cli.main(["deploy", "robot", "--direct"])
        self.assertNotEqual(result, 0)
        self.assertEqual(self.calls(), [])

    def test_deploy_uses_captured_files_if_sources_are_replaced_during_ssh_login(self):
        from unittest.mock import patch
        root = self.directory.resolve()
        server, page = root / "server.py", root / "camera.html"
        private = root / "private.txt"
        server.write_text("original-server")
        page.write_text("original-page")
        private.write_text("synthetic-secret")
        copies = root / "copied.jsonl"
        self.env["CAMERA_TEST_COPY_LOG"] = str(copies)
        bundle = ((server, "lite3-camera-server/"), (page, "lite3-camera-server/web/"))
        original_call = self.cli.subprocess.call

        def replace_after_prepare(command):
            code = original_call(command)
            if command[0] == "ssh":
                page.unlink()
                self.symlink(page, private)
                server.write_text("changed-server")
            return code

        with patch.object(self.cli, "SERVER_BUNDLE", bundle), patch.dict(os.environ, self.env), \
                patch.object(self.cli.subprocess, "call", replace_after_prepare):
            self.assertEqual(self.cli.main(["deploy", "robot", "--direct"]), 0)
        self.assertEqual([json.loads(line) for line in copies.read_text().splitlines()],
                         ["original-server", "original-page"])

    def test_deploy_uploads_only_allowlisted_files_beside_ignored_content(self):
        from unittest.mock import patch
        root = self.directory.resolve() / "runtime"
        (root / "web").mkdir(parents=True)
        server = root / "server.py"
        page = root / "web/camera.html"
        server.write_text("runtime")
        page.write_text("viewer")
        (root / "web/.env").write_text("synthetic-secret")
        (root / "web/private.jpg").write_bytes(b"private-photo")
        bundle = ((server, "lite3-camera-server/"), (page, "lite3-camera-server/web/"))
        with patch.object(self.cli, "SERVER_BUNDLE", bundle), patch.dict(os.environ, self.env):
            self.assertEqual(self.cli.main(["deploy", "robot", "--direct"]), 0)
        copies = [cmd for cmd in self.calls() if cmd[0] == "scp"]
        self.assertEqual([Path(cmd[-2]).name for cmd in copies], ["server.py", "camera.html"])
        self.assertTrue(all("-r" not in cmd for cmd in copies))

    def test_doctor_uses_login_environment_without_deploy(self):
        import shlex
        result = self.run_cli("doctor", "robot")
        self.assertEqual(result.returncode, 0, result.stderr)
        command, = self.calls()
        self.assertEqual(command[0], "ssh")
        self.assertEqual(shlex.split(command[-1])[:2], ["bash", "-lc"])

    def test_view_uses_loopback_pty_and_preserves_ssh_verification(self):
        port = self.free_port()
        result = self.run_cli("view", "robot", "--backend", "realsense", "--no-open", "--local-port", str(port), "--remote-port", "8081", "--serial", "abc; id")
        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.calls()
        self.assertIn("-tt", call)
        self.assertIn("127.0.0.1:{}:127.0.0.1:8081".format(port), call)
        self.assertIn("ExitOnForwardFailure=yes", call)
        self.assertNotIn("StrictHostKeyChecking=no", call)
        # Parse both levels of shell quotes. The serial number must remain one argument.
        import shlex
        remote = shlex.split(call[-1])
        self.assertEqual(remote[:2], ["bash", "-lc"])
        self.assertEqual(remote[-2:], ["--serial", "abc; id"])
        self.assertIn('"$@"', remote[2])
        self.assertNotIn("43893", call[-1])

    def test_v4l2_view_forwards_explicit_device_and_capture_options(self):
        import shlex
        result = self.run_cli("view", "robot", "--backend", "v4l2", "--device", "/dev/video4",
                              "--width", "320", "--height", "240", "--fps", "10", "--quality", "55",
                              "--remote-port", "8081", "--no-open")
        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.calls()
        remote = shlex.split(call[-1])
        self.assertEqual(remote[:2], ["bash", "-lc"])
        self.assertEqual(remote[5], "8081")
        self.assertEqual(remote[6:], ["--source", "v4l2", "--port", "8081", "--width", "320",
                                      "--height", "240", "--fps", "10", "--quality", "55",
                                      "--device", "/dev/video4"])
        self.assertNotIn("43893", remote[2])

    def test_v4l2_requires_device_and_rejects_other_backend_options(self):
        cases = (("--backend", "v4l2"),
                 ("--backend", "v4l2", "--device", "/dev/video4", "--depth"),
                 ("--backend", "v4l2", "--device", "/dev/video4", "--serial", "123"),
                 ("--backend", "v4l2", "--device", "/dev/video4", "--topic", "/camera/image"),
                 ("--backend", "v4l2", "--device", "/dev/video4", "--ros-setup", "/opt/ros/setup.bash"),
                 ("--backend", "ros", "--device", "/dev/video4"),
                 ("--backend", "realsense", "--device", "/dev/video4"))
        for options in cases:
            with self.subTest(options=options):
                result = self.run_cli("view", "robot", *options, "--no-open", "--local-port", "18080")
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("invalid choice", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_v4l2_rejects_invalid_device_forms_before_process_launch(self):
        for device in ("0", "video4", "/tmp/video4", "/dev/video4;id", "/dev/v4l/by-id/../video4", ""):
            with self.subTest(device=device):
                result = self.run_cli("view", "robot", "--backend", "v4l2", "--device=" + device,
                                      "--no-open", "--local-port", "18080")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("argument --device", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_ssh_exit_status_reaches_caller(self):
        result = self.run_cli("view", "robot", "--no-open", exit_code=42)
        self.assertEqual(result.returncode, 42)

    def test_ros_command_uses_sourced_version_without_python_rospy(self):
        result = self.run_cli("view", "robot", "--ros-setup", "/opt/ros/melodic/setup.bash", "--no-open")
        self.assertEqual(result.returncode, 0, result.stderr)
        command = self.calls()[0][-1]
        self.assertIn("ROS_VERSION", command)
        self.assertIn("rosrun", command)
        self.assertIn("ros2", command)
        self.assertNotIn("import rospy", command)

    def test_invalid_ports_and_backend_options_do_not_launch(self):
        for args in (("--local-port", "0"), ("--remote-port", "65536"), ("--quality", "0"), ("--depth",), ("--fps", "nan")):
            result = self.run_cli("view", "robot", *args, "--no-open")
            self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])

    @unittest.skipIf(os.name == "nt", "Remote Linux Bash integration runs on POSIX CI")
    def test_ros_remote_shell_executes_matching_binary_with_loopback_args(self):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        for version, binary, expected in ((1, "rosrun", ["web_video_server", "web_video_server", "_address:=127.0.0.1", "_port:=" + str(port)]),
                                          (2, "ros2", ["run", "web_video_server", "web_video_server", "--ros-args", "-p", "address:=127.0.0.1", "-p", "port:=" + str(port), "-p", "default_qos_profile:=sensor_data"])):
            for name in ("rospack", "rosrun", "ros2"):
                path = self.directory / name
                path.write_text(
                    "#!" + sys.executable + "\nimport json, os, sys\n"
                    "with open(os.environ['CAMERA_TEST_LOG'], 'a', encoding='utf-8') as log:\n"
                    "    log.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + '\\n')\n",
                    encoding="utf-8")
                path.chmod(0o755)
            setup = self.directory / "setup with spaces.bash"
            import shlex
            template = (ROOT / "tests/fixtures/ros_setup.sh").read_text()
            setup.write_text(template.replace("__ROS_VERSION__", str(version))
                             .replace("__BIN_DIRECTORY__", shlex.quote(str(self.directory))))
            args = self.cli.parser().parse_args(["view", "robot", "--ros-setup", str(setup), "--remote-port", str(port)])
            # Run the remote shell string locally. All ROS commands use test substitutes.
            result = subprocess.run(["/bin/sh", "-c", self.cli.ros_script(args)],
                                    env=self.env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = self.calls()[-1]
            if version == 1:
                self.assertRegex(actual[-1], r"^__name:=lite3_camera_view_[0-9]+$")
                actual = actual[:-1]
            self.assertEqual(actual, [binary] + expected)

    @unittest.skipIf(os.name == "nt", "Remote Linux Bash integration runs on POSIX CI")
    def test_ros_diagnostics_source_setup_without_passing_launcher_arguments(self):
        import shlex
        setup = self.directory / "setup with spaces.bash"
        template = (ROOT / "tests/fixtures/ros_setup.sh").read_text()
        for version in (1, 2):
            with self.subTest(version=version):
                setup.write_text(template.replace("__ROS_VERSION__", str(version))
                                 .replace("__BIN_DIRECTORY__", shlex.quote(str(self.directory))))
                command = self.cli.remote_shell("diagnose_ros.sh", str(setup), "environment")
                result = subprocess.run(["/bin/sh", "-c", command], env=self.env,
                                        capture_output=True, text=True, timeout=5)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("ROS_VERSION=" + str(version), result.stdout)

    @unittest.skipIf(os.name == "nt", "Windows terminate() force-kills; portable interrupt cleanup is tested separately")
    def test_sigterm_stops_owned_ssh_process(self):
        self.assert_sigterm_stops_owned_ssh_process("ros")

    @unittest.skipIf(os.name == "nt", "Windows terminate() force-kills; portable interrupt cleanup is tested separately")
    def test_sigterm_stops_native_client_ssh_process(self):
        self.assert_sigterm_stops_owned_ssh_process("realsense")

    def assert_sigterm_stops_owned_ssh_process(self, backend):
        import signal
        pid_file = self.directory / "child.pid"
        env = dict(self.env, CAMERA_TEST_PID_FILE=str(pid_file))
        process = subprocess.Popen([sys.executable, str(RUNNER), str(SCRIPT), "view", "robot", "--backend", backend,
                                    "--no-open", "--local-port", str(self.free_port())],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child_pid = None
        try:
            deadline = time.monotonic() + 3
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "The SSH test substitute did not start")
            child_pid = int(pid_file.read_text())
            process.terminate()
            process.wait(timeout=7)
            with self.assertRaises(ProcessLookupError):
                os.kill(child_pid, 0)
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()
            if child_pid:
                try:
                    os.kill(child_pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_occupied_local_port_fails_before_ssh_or_browser(self):
        import socket
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            port = listener.getsockname()[1]
            result = self.run_cli("view", "robot", "--local-port", str(port), "--no-open")
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.calls(), [])
        self.assertIn("--local-port", result.stderr)

    def test_local_readiness_ignores_environment_proxy(self):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        import threading
        from unittest.mock import patch

        class RejectProxy(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_error(502, "Local test proxy rejects requests")

            def log_message(self, *args):
                pass

        with ThreadingHTTPServer(("127.0.0.1", 0), RejectProxy) as proxy:
            worker = threading.Thread(target=proxy.serve_forever, daemon=True)
            worker.start()
            port = self.free_port()
            url = "http://127.0.0.1:{}/".format(port)
            # Start the viewer only after run_foreground checks the free port.
            server_code = ("import time\n"
                           "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
                           "class Handler(BaseHTTPRequestHandler):\n"
                           "    def do_GET(self):\n"
                           "        self.send_response(200); self.end_headers()\n"
                           "    def log_message(self, *args): pass\n"
                           "with HTTPServer(('127.0.0.1', " + str(port) + "), Handler) as server:\n"
                           "    server.timeout = 0.05\n"
                           "    deadline = time.monotonic() + 1\n"
                           "    while time.monotonic() < deadline: server.handle_request()\n")
            proxy_url = "http://127.0.0.1:{}".format(proxy.server_port)
            proxy_env = {"http_proxy": proxy_url, "HTTP_PROXY": proxy_url,
                         "no_proxy": "", "NO_PROXY": ""}
            opened = []
            try:
                with patch.dict(os.environ, proxy_env), patch.object(self.cli.webbrowser, "open", opened.append):
                    result = self.cli.run_foreground([sys.executable, "-c", server_code], url, True)
                self.assertEqual(result, 0)
                self.assertEqual(opened, [url], "The proxy intercepted the local readiness check")
            finally:
                proxy.shutdown()
                worker.join(timeout=2)

    def test_native_browser_waits_for_camera_protocol(self):
        import contextlib
        import http.client
        import io
        port = self.free_port()
        url = "http://127.0.0.1:{}/".format(port)
        # The tunnel is initially an unrelated HTTP 200 listener, then the native camera.
        code = ("import time\nfrom http.server import BaseHTTPRequestHandler, HTTPServer\n"
                "ready = time.monotonic() + 0.7\n"
                "class Handler(BaseHTTPRequestHandler):\n"
                "    def do_GET(self):\n"
                "        self.send_response(200)\n"
                "        if time.monotonic() >= ready: self.send_header('X-Lite3-Camera-Protocol', '1')\n"
                "        self.end_headers()\n"
                "    def log_message(self, *args): pass\n"
                "HTTPServer(('127.0.0.1', " + str(port) + "), Handler).serve_forever()\n")
        original = subprocess.Popen
        def spawn(command, *args, **kwargs):
            return original([sys.executable, "-c", code], *args, **kwargs)
        opened = []
        def open_viewer(viewer_url):
            opened.append(viewer_url)
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
            try:
                connection.request("GET", "/")
                self.assertEqual(connection.getresponse().getheader("X-Lite3-Camera-Protocol"), "1",
                                 "The browser opened an unrelated HTTP service")
            finally:
                connection.close()
            raise KeyboardInterrupt
        with patch.object(self.cli.subprocess, "Popen", spawn), \
                patch.object(self.cli.webbrowser, "open", open_viewer), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(self.cli.main(["view", "robot", "--backend", "realsense", "--direct",
                                           "--local-port", str(port)]), 130)
        self.assertEqual(opened, [url])

    def test_ros_topic_url_preserves_slashes_for_legacy_http_parser(self):
        result = self.run_cli("view", "robot", "--topic", "/camera/color/image_raw", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("topic=/camera/color/image_raw&", result.stdout)

    def test_native_browser_opens_bare_url_and_reads_frames(self):
        import contextlib
        import http.client
        import io
        from unittest.mock import patch
        from urllib.parse import urlsplit
        port = self.free_port()
        url = "http://127.0.0.1:{}/".format(port)
        command = [sys.executable, "-u", str(ROOT / "tests/fixtures/camera_server.py"),
                   str(ROOT / "src/server.py"), "--source", "v4l2", "--device", "/dev/video4",
                   "--port", str(port)]
        opened = []
        output = io.StringIO()

        def open_viewer(session_url):
            opened.append(session_url)
            address = urlsplit(session_url)
            self.assertEqual(address.fragment, "")
            conn = http.client.HTTPConnection("127.0.0.1", address.port, timeout=2)
            try:
                conn.request("GET", "/snapshot.jpg")
                response = conn.getresponse()
                self.assertEqual(response.status, 200)
                self.assertEqual(response.read(), (ROOT / "tests/fixtures/camera-frame.jpg").read_bytes())
            finally:
                conn.close()
            raise KeyboardInterrupt  # The user closes the foreground session after opening it.

        with patch.object(self.cli.webbrowser, "open", open_viewer), contextlib.redirect_stdout(output):
            self.assertEqual(self.cli.run_foreground(command, url, True, native=True), 130)
        self.assertEqual(len(opened), 1)
        self.assertEqual(opened[0], url)
        self.assertNotIn("#token=", output.getvalue())


if __name__ == "__main__":
    unittest.main()
