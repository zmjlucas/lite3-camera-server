#!/usr/bin/env python3
"""Lite3 development server: shared cameras and bounded robot communications."""
import argparse
from contextlib import ExitStack
import os
import re
import signal
import sys

if __package__:
    from . import wide_angle
    from .camera import CaptureController, LatestFrame
    from .http_server import DEFAULT_BIND, CameraHTTPServer
else:
    import wide_angle
    from camera import CaptureController, LatestFrame
    from http_server import DEFAULT_BIND, CameraHTTPServer

STARTUP_SOURCES = ("realsense", "v4l2", "none")


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", choices=STARTUP_SOURCES, default="realsense")
    parser.add_argument("--port", type=int, default=8080, help="HTTP port (use 0 for a free port)")
    parser.add_argument("--bind", default=DEFAULT_BIND, help="listen address (default: all IPv4 interfaces)")
    parser.add_argument("--width", type=int, default=640, help="frame width in pixels for each stream")
    parser.add_argument("--height", type=int, default=480, help="frame height in pixels for each stream")
    parser.add_argument("--fps", type=int, default=15)
    parser.add_argument("--quality", type=int, default=70, help="camera JPEG quality (1 to 100)")
    parser.add_argument("--depth", action="store_true", help="show RealSense depth with false colors beside RGB")
    parser.add_argument("--serial", help="select a RealSense camera by serial number")
    parser.add_argument("--retry-camera", action="store_true",
                        help="retry failed capture every 5 seconds (used by the boot service)")
    parser.add_argument("--device", help="V4L2 RGB device path (required for --source v4l2)")
    parser.add_argument("--wide-angle-host", metavar="IPV4", help="enable the motion host's vendor RTSP camera")
    parser.add_argument("--control-mode", choices=("disabled", "simulation", "readonly", "hardware"), default="simulation")
    parser.add_argument("--dog-host", default="192.168.1.120", help="motion host IPv4 for UDP port 43893")
    parser.add_argument("--arm-device", default="/dev/serial/by-id/usb-1a86_USB_Serial-if00-port0")
    parser.add_argument("--arm-host", help="optional SSH host for the read-only serial gateway")
    parser.add_argument("--arm-motion-profile", metavar="PATH",
                        help="one-use local R5 commissioning record; disables dog output")
    options = parser.parse_args(argv)
    if options.arm_motion_profile is not None:
        if options.control_mode != "hardware" or options.arm_host is not None:
            parser.error("--arm-motion-profile requires hardware mode and a local arm")
        if not options.arm_motion_profile or any(ord(char) < 32 for char in options.arm_motion_profile):
            parser.error("--arm-motion-profile must be a local record path")
    if options.wide_angle_host is not None:
        try:
            wide_angle.validate_host(options.wide_angle_host)
        except ValueError as error:
            parser.error(str(error))
        if options.source not in ("realsense", "v4l2"):
            parser.error("--wide-angle-host requires --source realsense or v4l2")
    if options.source == "v4l2":
        if not re.fullmatch(r"/dev/(?:video[0-9]+|v4l/by-id/[A-Za-z0-9_][A-Za-z0-9_.:+-]*)", options.device or ""):
            parser.error("--source v4l2 requires --device /dev/videoN or /dev/v4l/by-id/NAME")
        if options.depth or options.serial is not None:
            parser.error("--source v4l2 does not support --depth or --serial")
    elif options.device is not None:
        parser.error("--device requires --source v4l2")
    if not 0 <= options.port <= 65535:
        parser.error("--port must be between 0 and 65535")
    if min(options.width, options.height, options.fps) <= 0:
        parser.error("--width, --height and --fps must be positive")
    if not 1 <= options.quality <= 100:
        parser.error("--quality must be between 1 and 100")
    return options


def development_services(options):
    """Construct lazy gateways; commissioning selects an arm-only control service."""
    if __package__:
        from .control import ControlService
        from .arm_gateway import ArmGateway
        from .arm_motion import ArmMotionGateway
    else:
        from control import ControlService
        from arm_gateway import ArmGateway
        from arm_motion import ArmMotionGateway
    arm_gateway = None
    profile = options.arm_motion_profile
    try:
        if profile:
            if os.name != "posix":
                raise ValueError("Hardware arm diagnostics require a POSIX service host")
            arm_gateway = ArmMotionGateway(options.arm_device, profile)
        elif options.control_mode in ("hardware", "readonly"):
            arm_gateway = ArmGateway(device=options.arm_device, host=options.arm_host)
        control = ControlService(mode=options.control_mode, dog_host=options.dog_host,
                                 arm_gateway=arm_gateway if profile else None,
                                 dog_enabled=not bool(profile))
        return control, arm_gateway
    except Exception:
        if arm_gateway is not None:
            arm_gateway.close()
        raise


def main(argv=None):
    options = parse_args(argv)
    try:
        control, arm_gateway = development_services(options)
    except (OSError, ValueError) as error:
        print("Invalid development server configuration: {}".format(error), file=sys.stderr)
        return 2
    controller = None
    stopping = False

    def request_stop(_signum, _frame):
        nonlocal stopping
        stopping = True

    with ExitStack() as listener:
        try:
            with ExitStack() as services:
                # A commissioning gateway belongs to ControlService once construction succeeds.
                if arm_gateway is not None and not options.arm_motion_profile:
                    services.callback(arm_gateway.close)
                services.callback(control.close)
                controller = CaptureController(options) if options.source != "none" else None
                frames = controller.frames if controller else LatestFrame("none")

                for name in ("SIGINT", "SIGTERM", "SIGHUP", "SIGBREAK"):
                    signum = getattr(signal, name, None)
                    if signum is not None:
                        signal.signal(signum, request_stop)

                try:
                    server = CameraHTTPServer(options.port, frames, controller, bind=options.bind,
                                              control=control, arm_gateway=arm_gateway)
                except OSError as error:
                    print("Cannot listen on {}:{}: {}".format(options.bind, options.port, error), file=sys.stderr)
                    return 1

                listener.enter_context(server)
                control.start()
                if controller:
                    controller.start()
                print("http://{}:{}/".format(options.bind, server.server_port), flush=True)
                while not stopping:
                    server.handle_request()
        finally:
            stopped = controller.shutdown() if controller else True
    if not stopped:
        print("Camera shutdown timed out", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
