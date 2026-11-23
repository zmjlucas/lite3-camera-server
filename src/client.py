#!/usr/bin/env python3
"""View the Lite3 camera in a Mac browser through SSH. The Mac does not need ROS."""
import argparse
import ipaddress
from pathlib import Path
import re
import shlex
import signal
import socket
import subprocess
import sys
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import ProxyHandler, build_opener
import webbrowser


SERVER = Path(__file__).resolve().with_name("server.py")
REMOTE_SCRIPTS = SERVER.parent / "remote"
SERVER_BUNDLE = (SERVER, SERVER.parent / "web")
SSH_OPTIONS = ["-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3"]
ROBOT_NETWORK = ipaddress.IPv4Network("192.168.1.0/24")
ROBOT_GATEWAY = "192.168.2.1"


def target(value):
    if not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        raise argparse.ArgumentTypeError("use an SSH alias, hostname, or user@IPv4. Do not include shell characters.")
    return value


def video_device(value):
    if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9_][A-Za-z0-9_.:+-]*)", value):
        raise argparse.ArgumentTypeError("use a confirmed device path: /dev/videoN or /dev/v4l/by-id/name")
    return value


def bounded_int(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError("must be between {} and {}".format(low, high))
        return number
    return parse


def quoted_command(arguments):
    return " ".join(shlex.quote(str(argument)) for argument in arguments)


def remote_source(name):
    return (REMOTE_SCRIPTS / name).read_text(encoding="utf-8")


def remote_shell(name, *arguments):
    return quoted_command(["bash", "-lc", remote_source(name), "lite3-camera", *arguments])


def macos_route(address):
    """Read the kernel's IPv4 route. Do not send packets or change the route."""
    try:
        result = subprocess.run(["route", "-n", "get", address],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    fields = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if separator:
            fields[key.strip()] = value.strip()
    if fields.get("route to") != address or not fields.get("destination"):
        return None
    flags = set(fields.get("flags", "").strip("<>").split(","))
    if not fields.get("interface") or "UP" not in flags or flags & {"REJECT", "BLACKHOLE"}:
        return None
    if "GATEWAY" in flags and not fields.get("gateway"):
        return None
    fields["on_link"] = "GATEWAY" not in flags
    return fields


def macos_wifi_interfaces():
    """Find Wi-Fi hardware interfaces. USB network interfaces also use enN names."""
    try:
        result = subprocess.run(["networksetup", "-listallhardwareports"],
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                                text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return set()
    if result.returncode:
        return set()
    interfaces = set()
    hardware_port = None
    for line in result.stdout.splitlines():
        key, separator, value = line.partition(":")
        if not line.strip():
            hardware_port = None
        elif separator and key.strip() == "Hardware Port":
            hardware_port = value.strip()
        elif separator and key.strip() == "Device" and hardware_port in ("Wi-Fi", "AirPort"):
            if re.fullmatch(r"en[0-9]+", value.strip()):
                interfaces.add(value.strip())
    return interfaces


def resolve_jump(args):
    """Select one path for SSH and SCP per command. Use manual options when supplied."""
    if args.jump or args.direct:
        return args.jump
    if sys.platform != "darwin":
        return None
    username, separator, host = args.target.rpartition("@")
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        # Let OpenSSH resolve aliases and DNS names with its existing proxy configuration.
        return None
    if address not in ROBOT_NETWORK:
        return None

    destination = macos_route(str(address))
    gateway = macos_route(ROBOT_GATEWAY)
    robot_link = (gateway is not None and gateway["on_link"] and
                  gateway["interface"] in macos_wifi_interfaces())
    path = "route not available"
    if destination:
        path = "{} via {}".format(destination["interface"],
                                 "on-link" if destination["on_link"] else destination["gateway"])
        # A phone can use the same subnet as the perception host.
        # If robot Wi-Fi is available, use it instead of a local link on another network interface.
        direct_link = (destination["on_link"] and
                       (not robot_link or destination["interface"] == gateway["interface"]))
        if direct_link or (robot_link and
                destination.get("gateway") == ROBOT_GATEWAY and
                destination["interface"] == gateway["interface"]):
            print("Route: direct to {} ({})".format(host, path), file=sys.stderr)
            return None
    if robot_link:
        jump = (username + separator if separator else "") + ROBOT_GATEWAY
        print("Route: {} ({}). SSH jump host: {} on {}".format(
            host, path, jump, gateway["interface"]), file=sys.stderr)
        return jump
    print("Route: direct to {} ({}). The launcher did not find a robot gateway on a local Wi-Fi link. "
          "Check the robot Wi-Fi connection. To select the route manually, use --jump USER@HOST or --direct.".format(host, path),
          file=sys.stderr)
    return None


def ssh_options(jump=None):
    return SSH_OPTIONS + (["-J", jump] if jump else [])


def ssh_command(host, command, jump=None):
    return ["ssh"] + ssh_options(jump) + [host, command]


def show_command(command):
    print(" ".join(shlex.quote(arg) for arg in command), flush=True)


def ros_script(args):
    return remote_shell("view_ros.sh", args.remote_port, args.ros_setup or "",
                        remote_source("check_camera_port.py"))


def camera_options(args):
    values = ["--width", str(args.width or 640), "--height", str(args.height or 480),
              "--fps", str(args.fps or 15), "--quality", str(args.quality or 70)]
    if args.depth:
        values.append("--depth")
    if args.serial:
        values.extend(["--serial", args.serial])
    if args.device:
        values.extend(["--device", args.device])
    return values


def real_script(args):
    options = ["--source", args.backend, "--port", str(args.remote_port)] + camera_options(args)
    return remote_shell("view_camera.sh", remote_source("check_camera_port.py"),
                        args.remote_port, *options)


def run_foreground(command, url, open_browser):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", urlsplit(url).port))
            listener.listen(1)
        except OSError as exc:
            raise OSError("Cannot use the local camera port. Select another port with --local-port: " + str(exc)) from exc
    print("Viewer: " + url + "\nKeep this terminal open. To stop this session, press Ctrl+C.", flush=True)
    process = subprocess.Popen(command)
    # Readiness checks use the loopback tunnel and bypass HTTP proxies.
    opener = build_opener(ProxyHandler({}))
    opened = not open_browser
    start = time.monotonic()
    previous_handlers = {}
    def stop_session(signum, frame):
        raise KeyboardInterrupt
    for signum in (signal.SIGTERM, signal.SIGHUP):
        previous_handlers[signum] = signal.signal(signum, stop_session)
    try:
        while process.poll() is None:
            if not opened:
                try:
                    with opener.open(url, timeout=0.4) as response:
                        ready = response.status == 200
                    if ready and process.poll() is None:
                        webbrowser.open(url)
                        opened = True
                except OSError:
                    pass
                if not opened and time.monotonic() - start > 60:
                    print("The launcher did not open the browser. Complete SSH login. Then open the viewer URL.", flush=True)
                    opened = True
            time.sleep(0.15)
        return process.returncode if process.returncode >= 0 else 128 - process.returncode
    except KeyboardInterrupt:
        return 130
    finally:
        for signum, handler in previous_handlers.items():
            signal.signal(signum, handler)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    for name, help_text in (("doctor", "check the host environment through SSH without changes"),
                            ("deploy", "copy the camera server to the perception host"),
                            ("view", "start the remote camera server and SSH tunnel")):
        command = commands.add_parser(name, help=help_text)
        command.add_argument("target", type=target, help="SSH alias or user@perception-host")
        routing = command.add_mutually_exclusive_group()
        routing.add_argument("--jump", type=target,
                             help="set the SSH jump host (default: automatic selection on macOS for 192.168.1.*)")
        routing.add_argument("--direct", action="store_true",
                             help="use the existing SSH configuration without automatic route selection")
        command.add_argument("--dry-run", action="store_true",
                             help="print commands without a connection. The launcher can read local routes.")
        if name == "view":
            command.add_argument("--local-port", type=bounded_int(1, 65535), default=18080)
            command.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
            command.add_argument("--backend", choices=("ros", "realsense", "v4l2"), default="ros")
            command.add_argument("--remote-port", type=bounded_int(1, 65535), default=8080)
            command.add_argument("--ros-setup", help="absolute path to the ROS or workspace setup.bash on the perception host")
            command.add_argument("--topic", help="ROS image topic to show (default: show the topic list)")
            command.add_argument("--depth", action="store_true", help="show a RealSense depth preview with false colors")
            command.add_argument("--serial", help="RealSense serial number")
            command.add_argument("--device", type=video_device, help="confirmed V4L2 RGB device path on the perception host")
            command.add_argument("--width", type=bounded_int(1, 4096))
            command.add_argument("--height", type=bounded_int(1, 2160))
            command.add_argument("--fps", type=bounded_int(1, 60))
            command.add_argument("--quality", type=bounded_int(1, 95))
    return result


def main(argv=None):
    cli = parser()
    args = cli.parse_args(argv)
    if args.command == "view":
        if args.backend == "ros" and any((args.width, args.height, args.fps, args.quality)):
            cli.error("--width, --height, --fps, and --quality require --backend realsense or v4l2")
        if args.backend != "realsense" and (args.depth or args.serial):
            cli.error("--depth and --serial require --backend realsense")
        if args.backend != "ros" and (args.topic or args.ros_setup):
            cli.error("--topic and --ros-setup require --backend ros")
        if args.backend == "v4l2" and not args.device:
            cli.error("--backend v4l2 requires --device with a confirmed RGB device path")
        if args.backend != "v4l2" and args.device:
            cli.error("--device requires --backend v4l2")
        if args.ros_setup and not args.ros_setup.startswith("/"):
            cli.error("--ros-setup must be an absolute path on the perception host")
    try:
        jump = resolve_jump(args)
        if args.command == "doctor":
            diagnostic = remote_shell("run_doctor.sh", remote_source("camera_doctor.py"),
                                      remote_source("diagnose_ros.sh"), remote_source("probe_python_module.py"))
            command = ssh_command(args.target, diagnostic, jump)
            if args.dry_run:
                show_command(command)
                return 0
            return subprocess.call(command)
        if args.command == "deploy":
            commands = [ssh_command(args.target, remote_shell("prepare_deploy.sh"), jump),
                        ["scp"] + ssh_options(jump) + ["-r"] + [str(path) for path in SERVER_BUNDLE] +
                        [args.target + ":lite3-camera-server/"]]
            for command in commands:
                if args.dry_run:
                    show_command(command)
                else:
                    code = subprocess.call(command)
                    if code:
                        return code
            return 0
        url = "http://127.0.0.1:{}/".format(args.local_port)
        if args.backend == "ros" and args.topic:
            url += "stream_viewer?" + urlencode({"topic": args.topic, "type": "mjpeg", "width": 640, "height": 480, "quality": 70}, safe="/")
        remote = ros_script(args) if args.backend == "ros" else real_script(args)
        command = ["ssh"] + ssh_options(jump) + ["-tt", "-o", "ExitOnForwardFailure=yes", "-L",
                   "127.0.0.1:{}:127.0.0.1:{}".format(args.local_port, args.remote_port),
                   args.target, remote]
        if args.dry_run:
            show_command(command)
            print("Viewer: " + url)
            return 0
        return run_foreground(command, url, not args.no_open)
    except KeyboardInterrupt:
        return 130
    except OSError as exc:
        print("Camera command failed: " + str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
