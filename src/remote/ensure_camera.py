"""Start or reuse a detached native camera server on the Linux perception host."""
import argparse
import errno
import fcntl
from pathlib import Path
from http.client import HTTPConnection, HTTPException
import socket
import signal
import subprocess
import sys
import time


def camera_available(port, starting=False, require_dev=False, environment="dist", simulate=False,
                     control_mode="hardware", arm_diagnostics=False):
    # Probe exactly this listener: no environment proxy or HTTP redirects.
    connection = HTTPConnection("127.0.0.1", port, timeout=1)
    try:
        connection.request("GET", "/healthz")
        response = connection.getresponse()
        if response.status in (200, 503) and response.getheader("X-Lite3-Camera-Protocol") == "1":
            dev_protocol = response.getheader("X-Lite3-Dev-Protocol")
            if require_dev and dev_protocol != "1":
                raise RuntimeError("This listener is a camera-only server. Deploy and explicitly restart it to use robot control.")
            actual_environment = response.getheader("X-Lite3-Environment")
            actual_simulated = response.getheader("X-Lite3-Simulated")
            actual_mode = response.getheader("X-Lite3-Control-Mode")
            actual_diagnostics = response.getheader("X-Lite3-Arm-Diagnostics")
            # Legacy camera-only listeners remain usable for ordinary camera viewing.
            # Every control-capable listener must identify its runtime configuration.
            if (dev_protocol is not None or actual_environment is not None or actual_simulated is not None
                    or actual_mode is not None or actual_diagnostics is not None
                    or require_dev or environment != "dist" or simulate or arm_diagnostics):
                if (actual_environment not in ("dev", "dist") or actual_simulated not in ("true", "false")
                        or actual_mode not in ("disabled", "readonly", "hardware")
                        or actual_diagnostics not in ("true", "false")):
                    raise RuntimeError("Cannot verify this server's environment, simulation, control mode and arm diagnostics settings. "
                                       "Deploy and explicitly restart it before reuse.")
                if (actual_environment != environment or actual_simulated != ("true" if simulate else "false")
                        or actual_mode != control_mode or actual_diagnostics != ("true" if arm_diagnostics else "false")):
                    raise RuntimeError("The running server's environment, simulation, control mode or arm diagnostics setting differs from this launch. "
                                       "Explicitly restart it with the requested settings before reuse.")
            return True
        raise RuntimeError("The remote port is occupied by an incompatible server. "
                           "Stop the old server and run deploy, or select another --remote-port.")
    except (OSError, HTTPException) as error:
        if starting or getattr(error, "errno", None) == errno.ECONNREFUSED:
            return False
        raise RuntimeError("Cannot check the remote camera port: " + str(error))
    finally:
        connection.close()


def report(message):
    try:
        print(message, flush=True)
    except OSError:
        # The client may have disconnected while the detached server was starting.
        pass


def ensure_server(server_path, port, options):
    options = list(options)
    require_dev = "--require-dev" in options
    if require_dev:
        options.remove("--require-dev")
    identity_parser = argparse.ArgumentParser(add_help=False)
    identity_parser.add_argument("--env", choices=("dev", "dist"), default="dist")
    identity_parser.add_argument("--simulate", action="store_true")
    identity_parser.add_argument("--control-mode", choices=("disabled", "readonly", "hardware"), default="hardware")
    identity_parser.add_argument("--arm-motion-profile")
    identity, _ = identity_parser.parse_known_args(options)
    if identity.simulate and identity.env != "dev":
        raise RuntimeError("--simulate requires --env dev")
    expected = dict(require_dev=require_dev, environment=identity.env, simulate=identity.simulate,
                    control_mode=identity.control_mode, arm_diagnostics=identity.arm_motion_profile is not None)
    server_path = Path(server_path)
    # A sibling launcher may hold the lock while its new listener initializes.
    if camera_available(port, starting=server_path.is_file(), **expected):
        report("Reusing camera server on port {} (existing camera and control settings; launch options do not change the running server).".format(port))
        return
    if not server_path.is_file():
        raise RuntimeError("The camera server script is missing. Run deploy first.")
    runtime = server_path.parent / ".runtime"
    runtime.mkdir(exist_ok=True)
    log_path = runtime / (str(port) + ".log")
    pid_path = runtime / (str(port) + ".pid")
    # Serialize probe + spawn across simultaneous SSH clients; the camera opens only once.
    with (runtime / (str(port) + ".lock")).open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if camera_available(port, **expected):
            report("Reusing camera server on port {} (existing camera and control settings; launch options do not change the running server).".format(port))
            return
        # Check all interfaces, not just loopback, before detaching a new LAN listener.
        with socket.socket() as listener:
            listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                listener.bind(("0.0.0.0", port))
            except OSError as error:
                raise RuntimeError("Cannot use the remote camera port: " + str(error))
        with log_path.open("ab", buffering=0) as log:
            process = subprocess.Popen([sys.executable, "-u", str(server_path)] + options,
                                       stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                                       start_new_session=True, close_fds=True)
        try:
            pid_path.write_text(str(process.pid) + "\n")
            deadline = time.monotonic() + 15
            while process.poll() is None and time.monotonic() < deadline:
                if camera_available(port, starting=True, **expected):
                    report("Started shared camera server on port {}. Log: {}. PID: {}.".format(
                        port, log_path, pid_path))
                    return
                time.sleep(0.1)
            raise RuntimeError("Camera server did not start. Check log: " + str(log_path))
        except BaseException:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            remove_pid(pid_path)
            raise


def remove_pid(path):
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def main():
    # Complete startup even if the SSH terminal disappears before readiness.
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    try:
        ensure_server(sys.argv[1], int(sys.argv[2]), sys.argv[3:])
    except (OSError, RuntimeError) as error:
        print(str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
