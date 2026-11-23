"""Exercise Mac commands with local fake SSH/SCP; never connect to a robot."""
import json
import importlib.util
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/lite3_camera.py"


class CameraCommandsTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "Mac camera launcher has not been implemented")
        spec = importlib.util.spec_from_file_location("camera_cli", SCRIPT)
        self.cli = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.cli)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.log = self.directory / "calls.jsonl"
        executable = ("#!" + sys.executable + "\n"
                      "import json, os, sys, time\n"
                      "with open(os.environ['CAMERA_TEST_LOG'], 'a') as f:\n"
                      "    f.write(json.dumps([os.path.basename(sys.argv[0])] + sys.argv[1:]) + '\\n')\n"
                      "if os.environ.get('CAMERA_TEST_PID_FILE'):\n"
                      "    open(os.environ['CAMERA_TEST_PID_FILE'], 'w').write(str(os.getpid()))\n"
                      "    time.sleep(30)\n"
                      "sys.exit(int(os.environ.get('CAMERA_TEST_EXIT', '0')))\n")
        for name in ("ssh", "scp"):
            path = self.directory / name
            path.write_text(executable)
            path.chmod(0o755)
        self.env = dict(os.environ, PATH=str(self.directory) + os.pathsep + os.environ["PATH"],
                        CAMERA_TEST_LOG=str(self.log), PYTHONDONTWRITEBYTECODE="1")

    def run_cli(self, *args, exit_code=0):
        args = list(args)
        if args[0] == "view" and "--dry-run" not in args and "--local-port" not in args:
            args.extend(["--local-port", str(self.free_port())])
        env = dict(self.env, CAMERA_TEST_EXIT=str(exit_code))
        return subprocess.run([sys.executable, str(SCRIPT)] + args, env=env,
                              capture_output=True, text=True, timeout=8)

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
                 ("deploy", [], ["ssh", "scp"]),
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
                                           "ysc@192.168.1.103:robo-comp-camera/lite3_camera_server.py")
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

    def test_deploy_creates_directory_then_copies_only_camera_server(self):
        result = self.run_cli("deploy", "robot")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertEqual([call[0] for call in calls], ["ssh", "scp"])
        self.assertIn("mkdir -p", calls[0][-1])
        self.assertTrue(calls[1][-2].endswith("scripts/lite3_camera_server.py"))
        self.assertEqual(calls[1][-1], "robot:robo-comp-camera/lite3_camera_server.py")

    def test_deploy_failure_prevents_copy(self):
        result = self.run_cli("deploy", "robot", exit_code=23)
        self.assertEqual(result.returncode, 23)
        self.assertEqual(len(self.calls()), 1)

    def test_view_uses_loopback_pty_and_preserves_ssh_verification(self):
        result = self.run_cli("view", "robot", "--backend", "realsense", "--no-open", "--local-port", "18081", "--remote-port", "8081", "--serial", "abc; id")
        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.calls()
        self.assertIn("-tt", call)
        self.assertIn("127.0.0.1:18081:127.0.0.1:8081", call)
        self.assertIn("ExitOnForwardFailure=yes", call)
        self.assertNotIn("StrictHostKeyChecking=no", call)
        # Parse both quoting layers. The serial must remain one argument.
        import shlex
        remote = shlex.split(call[-1])
        self.assertEqual(remote[:2], ["bash", "-lc"])
        self.assertIn("'abc; id'", remote[2])
        self.assertNotIn("43893", remote[2])

    def test_v4l2_view_forwards_explicit_device_and_capture_options(self):
        import shlex
        result = self.run_cli("view", "robot", "--backend", "v4l2", "--device", "/dev/video4",
                              "--width", "320", "--height", "240", "--fps", "10", "--quality", "55",
                              "--remote-port", "8081", "--no-open")
        self.assertEqual(result.returncode, 0, result.stderr)
        call, = self.calls()
        remote = shlex.split(call[-1])
        self.assertEqual(remote[:2], ["bash", "-lc"])
        capture = shlex.split(remote[2].splitlines()[-1])
        self.assertEqual(capture[:4], ["exec", "python3", "-u", "$HOME/robo-comp-camera/lite3_camera_server.py"])
        self.assertEqual(capture[4:], ["--source", "v4l2", "--port", "8081", "--width", "320",
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

    def test_v4l2_rejects_unverified_device_forms_before_process_launch(self):
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

    def test_ros_remote_shell_executes_matching_binary_with_loopback_args(self):
        import socket
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        for version, binary, expected in ((1, "rosrun", ["web_video_server", "web_video_server", "_address:=127.0.0.1", "_port:=" + str(port)]),
                                          (2, "ros2", ["run", "web_video_server", "web_video_server", "--ros-args", "-p", "address:=127.0.0.1", "-p", "port:=" + str(port), "-p", "default_qos_profile:=sensor_data"])):
            for name in ("rospack", "rosrun", "ros2"):
                path = self.directory / name
                path.write_text((self.directory / "ssh").read_text())
                path.chmod(0o755)
            setup = self.directory / "setup with spaces.bash"
            import shlex
            setup.write_text("export ROS_VERSION={}\nexport PATH={}:$PATH\n".format(version, shlex.quote(str(self.directory))))
            args = self.cli.parser().parse_args(["view", "robot", "--ros-setup", str(setup), "--remote-port", str(port)])
            # Execute the exact remote shell string locally; all ROS commands are fakes.
            result = subprocess.run(["/bin/sh", "-c", self.cli.shell_command(self.cli.ros_script(args))],
                                    env=self.env, capture_output=True, text=True, timeout=5)
            self.assertEqual(result.returncode, 0, result.stderr)
            actual = self.calls()[-1]
            if version == 1:
                self.assertRegex(actual[-1], r"^__name:=lite3_camera_view_[0-9]+$")
                actual = actual[:-1]
            self.assertEqual(actual, [binary] + expected)

    def test_sigterm_stops_owned_ssh_process(self):
        import signal
        pid_file = self.directory / "child.pid"
        env = dict(self.env, CAMERA_TEST_PID_FILE=str(pid_file))
        process = subprocess.Popen([sys.executable, str(SCRIPT), "view", "robot", "--no-open", "--local-port", str(self.free_port())],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        child_pid = None
        try:
            deadline = time.monotonic() + 3
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(pid_file.exists(), "fake SSH never started")
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
                self.assertEqual(opened, [url], "local viewer readiness was intercepted by the proxy")
            finally:
                proxy.shutdown()
                worker.join(timeout=2)

    def test_ros_topic_url_preserves_slashes_for_legacy_http_parser(self):
        result = self.run_cli("view", "robot", "--topic", "/camera/color/image_raw", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("topic=/camera/color/image_raw&", result.stdout)


if __name__ == "__main__":
    unittest.main()
