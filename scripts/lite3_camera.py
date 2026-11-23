#!/usr/bin/env python3
"""Mac camera viewer: SSH + browser, with no local ROS installation."""
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


SERVER = Path(__file__).resolve().with_name("lite3_camera_server.py")
REMOTE_SERVER = '"$HOME/robo-comp-camera/lite3_camera_server.py"'
SSH_OPTIONS = ["-o", "ConnectTimeout=5", "-o", "ServerAliveInterval=15",
               "-o", "ServerAliveCountMax=3"]
ROBOT_NETWORK = ipaddress.IPv4Network("192.168.1.0/24")
ROBOT_GATEWAY = "192.168.2.1"

# Executed only by doctor, on the perception host. No camera is opened.
DOCTOR = r'''
import glob, json, os, platform, shlex, subprocess, sys
def check(label, argv):
    print("\n" + label, flush=True)
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, timeout=10)
        print(result.stdout[:16000].strip() or "(no output)", flush=True)
        print("exit=" + str(result.returncode), flush=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), flush=True)
print("Host: " + platform.platform(), flush=True)
print("Python: " + sys.version.split()[0], flush=True)
check("OS", ["cat", "/etc/os-release"])
for module in ("numpy", "cv2", "pyrealsense2"):
    check("Python binding: " + module, [sys.executable, "-c",
          "import " + module + "; print(getattr(" + module + ", '__version__', 'import OK'))"])
print("Video nodes: " + json.dumps(glob.glob("/dev/video*") + glob.glob("/dev/v4l/by-id/*")), flush=True)
check("Camera services (read only)", ["systemctl", "is-active", "realsense", "realsense_ros2"])
setups = sorted(glob.glob("/opt/ros/*/setup.bash"))
print("ROS setups: " + json.dumps(setups), flush=True)
for setup in setups:
    prefix = "source " + shlex.quote(setup) + "; "
    check("ROS environment: " + setup, ["bash", "-lc", prefix +
          "printf 'ROS_VERSION=%s ROS_DISTRO=%s ROS_MASTER_URI=%s ROS_DOMAIN_ID=%s\\n' "
          '"$ROS_VERSION" "$ROS_DISTRO" "$ROS_MASTER_URI" "$ROS_DOMAIN_ID"'])
    check("web_video_server: " + setup, ["bash", "-lc", prefix +
          'if [ "$ROS_VERSION" = 1 ]; then exec rospack find web_video_server; '
          'else exec ros2 pkg prefix web_video_server; fi'])
    check("Image topic candidates (use actual names): " + setup, ["bash", "-lc", prefix +
          'if [ "$ROS_VERSION" = 1 ]; then exec rostopic list -v; '
          'else exec ros2 topic list -t --no-daemon; fi'])
'''


def target(value):
    if not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9_][A-Za-z0-9_.-]*", value):
        raise argparse.ArgumentTypeError("use an SSH alias, hostname, or user@IPv4; no shell characters")
    return value


def video_device(value):
    if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9_][A-Za-z0-9_.:+-]*)", value):
        raise argparse.ArgumentTypeError("use a verified /dev/videoN or /dev/v4l/by-id/name device")
    return value


def bounded_int(low, high):
    def parse(value):
        number = int(value)
        if not low <= number <= high:
            raise argparse.ArgumentTypeError("must be between {} and {}".format(low, high))
        return number
    return parse


def shell_command(script):
    return "bash -lc " + shlex.quote(script)


def macos_route(address):
    """Read the kernel's IPv4 route without sending packets or changing it."""
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
    """Identify Wi-Fi hardware; USB tether interfaces also have enN names."""
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
    """Choose one SSH/SCP path per invocation, respecting explicit overrides."""
    if args.jump or args.direct:
        return args.jump
    if sys.platform != "darwin":
        return None
    username, separator, host = args.target.rpartition("@")
    try:
        address = ipaddress.IPv4Address(host)
    except ValueError:
        # Leave SSH aliases and DNS names to OpenSSH (including its proxy config).
        return None
    if address not in ROBOT_NETWORK:
        return None

    destination = macos_route(str(address))
    gateway = macos_route(ROBOT_GATEWAY)
    robot_link = (gateway is not None and gateway["on_link"] and
                  gateway["interface"] in macos_wifi_interfaces())
    path = "route unavailable"
    if destination:
        path = "{} via {}".format(destination["interface"],
                                 "on-link" if destination["on_link"] else destination["gateway"])
        # A phone can advertise the same subnet as the perception host. When
        # robot Wi-Fi is present, prefer it over an on-link route on another NIC.
        direct_link = (destination["on_link"] and
                       (not robot_link or destination["interface"] == gateway["interface"]))
        if direct_link or (robot_link and
                destination.get("gateway") == ROBOT_GATEWAY and
                destination["interface"] == gateway["interface"]):
            print("Route: direct to {} ({})".format(host, path), file=sys.stderr)
            return None
    if robot_link:
        jump = (username + separator if separator else "") + ROBOT_GATEWAY
        print("Route: {} ({}); using SSH jump {} on {}".format(
            host, path, jump, gateway["interface"]), file=sys.stderr)
        return jump
    print("Route: direct to {} ({}); no on-link robot Wi-Fi gateway detected. "
          "Check robot Wi-Fi or use --jump USER@HOST / --direct.".format(host, path),
          file=sys.stderr)
    return None


def ssh_options(jump=None):
    return SSH_OPTIONS + (["-J", jump] if jump else [])


def ssh_command(host, script, jump=None):
    return ["ssh"] + ssh_options(jump) + [host, shell_command(script)]


def show_command(command):
    print(" ".join(shlex.quote(arg) for arg in command), flush=True)


def remote_preflight(port):
    # Check before launching, so an existing server isn't mistaken for this session.
    code = ("import socket, sys\n"
            "s=socket.socket()\n"
            "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
            "try:\n"
            "    s.bind(('127.0.0.1', {})); s.listen(1)\n".format(port) +
            "except OSError as exc:\n"
            "    sys.exit('Remote camera port unavailable; choose --remote-port: ' + str(exc))\n"
            "finally:\n"
            "    s.close()")
    return "python3 -c " + shlex.quote(code) + "\n"


def ros_script(args):
    if args.ros_setup:
        setup = "setup=" + shlex.quote(args.ros_setup)
    else:
        setup = '''shopt -s nullglob
setups=(/opt/ros/*/setup.bash)
if [ "${#setups[@]}" != 1 ]; then
  printf '%s\n' 'Cannot select ROS environment. Use --ros-setup /absolute/path/setup.bash.' >&2
  exit 2
fi
setup="${setups[0]}"'''
    return "set -e\n" + remote_preflight(args.remote_port) + setup + '''
if [ ! -r "$setup" ]; then printf 'Cannot read ROS setup: %s\n' "$setup" >&2; exit 2; fi
source "$setup"
case "$ROS_VERSION" in
  1)
    rospack find web_video_server >/dev/null || {
      printf '%s\n' 'Missing web_video_server for this ROS 1 environment; see CAMERA_STREAM.md.' >&2
      exit 2
    }
    exec rosrun web_video_server web_video_server _address:=127.0.0.1 _port:=''' + str(args.remote_port) + ''' __name:=lite3_camera_view_$$ ;;
  2)
    ros2 pkg prefix web_video_server >/dev/null || {
      printf '%s\n' 'Missing web_video_server for this ROS 2 environment; see CAMERA_STREAM.md.' >&2
      exit 2
    }
    exec ros2 run web_video_server web_video_server --ros-args -p address:=127.0.0.1 -p port:=''' + str(args.remote_port) + ''' -p default_qos_profile:=sensor_data ;;
  *) printf '%s\n' 'ROS_VERSION must be 1 or 2 after sourcing the setup.' >&2; exit 2 ;;
esac'''


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
    return ("set -e\n" + remote_preflight(args.remote_port) +
            "if [ ! -f " + REMOTE_SERVER + " ]; then "
            "printf '%s\\n' 'Camera server missing. Run the deploy command first.' >&2; exit 2; fi\n"
            "exec python3 -u " + REMOTE_SERVER + " " + " ".join(shlex.quote(x) for x in options))


def run_foreground(command, url, open_browser):
    with socket.socket() as listener:
        listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            listener.bind(("127.0.0.1", urlsplit(url).port))
            listener.listen(1)
        except OSError as exc:
            raise OSError("Local camera port unavailable; choose --local-port: " + str(exc)) from exc
    print("Viewer: " + url + "\nKeep this terminal open; Ctrl+C stops this session.", flush=True)
    process = subprocess.Popen(command)
    # Readiness checks address our own loopback tunnel, never an HTTP proxy.
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
                    print("Browser was not opened. Finish SSH login and open the viewer URL when ready.", flush=True)
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
    for name, help_text in (("doctor", "read-only SSH environment check"),
                            ("deploy", "copy the camera server to the perception host"),
                            ("view", "start a remote camera viewer and SSH tunnel"),
                            ("demo", "show synthetic frames locally; no robot needed")):
        command = commands.add_parser(name, help=help_text)
        if name != "demo":
            command.add_argument("target", type=target, help="SSH alias or user@perception-host")
            routing = command.add_mutually_exclusive_group()
            routing.add_argument("--jump", type=target,
                                 help="explicit SSH jump host; otherwise auto-select on macOS for 192.168.1.*")
            routing.add_argument("--direct", action="store_true",
                                 help="skip automatic route selection and use existing SSH configuration")
        command.add_argument("--dry-run", action="store_true",
                             help="print commands without connecting; may read local routes")
        if name in ("view", "demo"):
            command.add_argument("--local-port", type=bounded_int(1, 65535), default=18080)
            command.add_argument("--no-open", action="store_true", help="do not open a browser automatically")
        if name == "view":
            command.add_argument("--backend", choices=("ros", "realsense", "v4l2"), default="ros")
            command.add_argument("--remote-port", type=bounded_int(1, 65535), default=8080)
            command.add_argument("--ros-setup", help="absolute remote ROS/overlay setup.bash path")
            command.add_argument("--topic", help="actual ROS image topic; otherwise show topic index")
            command.add_argument("--depth", action="store_true", help="RealSense colorized depth preview")
            command.add_argument("--serial", help="RealSense serial number")
            command.add_argument("--device", type=video_device, help="verified V4L2 RGB device on the perception host")
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
            cli.error("--width/--height/--fps/--quality require --backend realsense or v4l2")
        if args.backend != "realsense" and (args.depth or args.serial):
            cli.error("--depth/--serial require --backend realsense")
        if args.backend != "ros" and (args.topic or args.ros_setup):
            cli.error("--topic/--ros-setup require --backend ros")
        if args.backend == "v4l2" and not args.device:
            cli.error("--backend v4l2 requires --device for a verified RGB video node")
        if args.backend != "v4l2" and args.device:
            cli.error("--device requires --backend v4l2")
        if args.ros_setup and not args.ros_setup.startswith("/"):
            cli.error("--ros-setup must be an absolute path on the perception host")
    try:
        jump = resolve_jump(args) if args.command != "demo" else None
        if args.command == "doctor":
            command = ssh_command(args.target, "python3 - <<'LITE3_CAMERA_DOCTOR'\n" + DOCTOR + "\nLITE3_CAMERA_DOCTOR", jump)
            if args.dry_run:
                show_command(command)
                return 0
            return subprocess.call(command)
        if args.command == "deploy":
            commands = [ssh_command(args.target, 'mkdir -p "$HOME/robo-comp-camera"', jump),
                        ["scp"] + ssh_options(jump) + [str(SERVER), args.target + ":robo-comp-camera/lite3_camera_server.py"]]
            for command in commands:
                if args.dry_run:
                    show_command(command)
                else:
                    code = subprocess.call(command)
                    if code:
                        return code
            return 0
        url = "http://127.0.0.1:{}/".format(args.local_port)
        if args.command == "demo":
            command = [sys.executable, str(SERVER), "--source", "demo", "--port", str(args.local_port)]
        else:
            if args.backend == "ros" and args.topic:
                url += "stream_viewer?" + urlencode({"topic": args.topic, "type": "mjpeg", "width": 640, "height": 480, "quality": 70}, safe="/")
            remote = ros_script(args) if args.backend == "ros" else real_script(args)
            command = ["ssh"] + ssh_options(jump) + ["-tt", "-o", "ExitOnForwardFailure=yes", "-L",
                       "127.0.0.1:{}:127.0.0.1:{}".format(args.local_port, args.remote_port),
                       args.target, shell_command(remote)]
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
