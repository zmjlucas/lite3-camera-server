"""Inspect and explicitly restart the native systemd service on the SSH host."""
import argparse
from contextlib import contextmanager
from http.client import HTTPConnection, HTTPException
import json
import os
import re
import signal
import subprocess
import sys
import time


SERVICE = "lite3-dev-server.service"
LEGACY_SERVICE = "lite3-camera-server.service"
PROPERTIES = ("Id", "LoadState", "UnitFileState", "ActiveState", "SubState", "MainPID",
              "ExecMainStartTimestampMonotonic", "ExecMainStatus", "Result",
              "FragmentPath", "DropInPaths", "ExecStart")


def process_identity(state):
    return state.get("MainPID", "0"), state.get("ExecMainStartTimestampMonotonic", "0")


def socket_pids(line):
    return set(re.findall(r"\bpid=(\d+)\b", line))


def port_conflicts(state, sockets):
    pid = state.get("MainPID", "0")
    return [line for line in sockets.splitlines() if line.strip()
            and (pid == "0" or socket_pids(line) != {pid})]


@contextmanager
def probe_timeout(seconds):
    # The remote Linux helper runs on the main thread. An interval timer bounds
    # the whole exchange, including a peer that trickles individual header bytes.
    if not hasattr(signal, "setitimer"):
        # Permit portable offline HTTP tests; deployed hosts always have setitimer.
        yield
        return
    def expired(_signum, _frame):
        raise TimeoutError("HTTP probe exceeded {:.2f}s".format(seconds))
    handler = signal.signal(signal.SIGALRM, expired)
    started = time.monotonic()
    timer = signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, handler)
        if timer[0]:
            signal.setitimer(signal.ITIMER_REAL, max(0.001, timer[0] - (time.monotonic() - started)), timer[1])


class ServiceManager:
    def __init__(self, port=8080, lines=50, timeout=30, expect_build=None):
        self.port = port
        self.lines = lines
        self.timeout = timeout
        self.expect_build = expect_build
        self.privilege = []
        self.deadline = None

    def remaining(self, maximum):
        if self.deadline is None:
            return maximum
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError("Timed out waiting for the restarted service")
        return min(remaining, maximum)

    def command(self, args, allow_missing=False, timeout=10):
        try:
            result = subprocess.run(self.privilege + args, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, universal_newlines=True,
                                    timeout=self.remaining(timeout))
        except subprocess.TimeoutExpired as error:
            output = error.output or ""
            if isinstance(output, bytes):
                output = output.decode("utf-8", errors="replace")
            raise RuntimeError("Timed out running {}: {}".format(" ".join(args), output.strip()))
        except OSError as error:
            raise RuntimeError("Cannot run {}: {}".format(args[0], error))
        # Some systemd versions return nonzero for an absent unit with properties.
        if result.returncode and not (allow_missing and "LoadState=not-found" in result.stdout.splitlines()):
            raise RuntimeError("{} failed (exit {}): {}".format(" ".join(args), result.returncode, result.stdout.strip()))
        return result.stdout

    def unit(self, name):
        output = self.command(["systemctl", "show", "--no-pager", "--all",
                               "--property=" + ",".join(PROPERTIES), name], allow_missing=True)
        state = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
        if not all(key in state for key in ("LoadState", "ActiveState", "MainPID")):
            raise RuntimeError("Incomplete systemd state for {}: {}".format(name, output.strip()))
        return state

    def sockets(self):
        # Inspect IPv4 and IPv6 listeners, including listeners bound to a LAN address.
        return self.command(["ss", "-H", "-ltnp", "sport = :{}".format(self.port)])

    def http_identity(self):
        timeout = self.remaining(1)
        connection = HTTPConnection("127.0.0.1", self.port, timeout=timeout)
        try:
            with probe_timeout(timeout):
                connection.request("GET", "/api/status")
                response = connection.getresponse()
                if (response.status != 200 or response.getheader("X-Lite3-Dev-Protocol") != "1"
                        or response.getheader("X-Lite3-Camera-Protocol") != "1"):
                    raise RuntimeError("HTTP port {} returned status {} with unsupported Lite3 protocol headers".format(
                        self.port, response.status))
                body = response.read(65537)
                if len(body) > 65536:
                    raise ValueError("status body exceeds 64 KiB")
                status = json.loads(body.decode("utf-8"))
                if (not isinstance(status, dict) or status.get("environment") not in ("dev", "dist")
                        or status.get("mode") not in ("disabled", "readonly", "hardware")):
                    raise ValueError("invalid development status")
                build = response.getheader("X-Lite3-Build")
                return "HTTP ready on port {}: environment={}, mode={}, build={}".format(
                    self.port, status["environment"], status["mode"], build or "unavailable"), build
        except (OSError, HTTPException, ValueError) as error:
            raise RuntimeError("HTTP port {} is unavailable: {}".format(self.port, error))
        finally:
            connection.close()

    def report(self):
        states = []
        for name in (SERVICE, LEGACY_SERVICE):
            state = self.unit(name)
            states.append(state)
            print("{}: {}".format(name, ", ".join("{}={}".format(key, state.get(key, ""))
                                                   for key in PROPERTIES if key != "Id")), flush=True)
        sockets = self.sockets()
        print("Listeners on port {}:\n{}".format(self.port, sockets.strip() or "none"), flush=True)
        pids = sorted(socket_pids(sockets))
        if pids:
            print(self.command(["ps", "-ww", "-p", ",".join(pids), "-o", "pid=,ppid=,args="]).strip(), flush=True)
        if port_conflicts(states[0], sockets):
            print("Listener conflict: a process outside {} owns this port (possibly detached). "
                  "Inspect the PID and command above and resolve it before restart.".format(SERVICE), flush=True)
        if states[1]["ActiveState"] not in ("inactive", "failed"):
            print("Legacy service conflict: {} is {}. Resolve it before restart.".format(
                LEGACY_SERVICE, states[1]["ActiveState"]), flush=True)
        try:
            print(self.http_identity()[0], flush=True)
        except RuntimeError as error:
            print(str(error), flush=True)

    def logs(self):
        print(self.command(["journalctl", "--no-pager", "-b", "-n", str(self.lines),
                            "-o", "short-iso", "-u", SERVICE, "-u", LEGACY_SERVICE]).rstrip(), flush=True)

    def restart(self):
        previous = self.unit(SERVICE)
        if previous["LoadState"] != "loaded":
            raise RuntimeError("{} has LoadState={}; deploy the service and resolve any mask before restart".format(
                SERVICE, previous["LoadState"]))
        legacy = self.unit(LEGACY_SERVICE)
        if legacy["ActiveState"] not in ("inactive", "failed"):
            raise RuntimeError("Restart refused: {} is {} (PID {}). Inspect and explicitly stop it first.".format(
                LEGACY_SERVICE, legacy["ActiveState"], legacy["MainPID"]))
        conflicts = port_conflicts(previous, self.sockets())
        if conflicts:
            raise RuntimeError("Restart refused: listener conflict on port {}:\n{}\n"
                               "Inspect and explicitly stop the conflicting process first.".format(
                                   self.port, "\n".join(conflicts)))
        self.deadline = time.monotonic() + self.timeout
        # Wait for this specific restart job; a merely queued job can leave a
        # previous healthy process serving HTTP during the readiness probes.
        self.command(["systemctl", "restart", SERVICE], timeout=self.timeout)
        last = "waiting for a fresh systemd process"
        while time.monotonic() < self.deadline:
            state = self.unit(SERVICE)
            if state["ActiveState"] == "failed":
                raise RuntimeError("{} failed: Result={}, ExecMainStatus={}".format(
                    SERVICE, state.get("Result"), state.get("ExecMainStatus")))
            fresh = (state["ActiveState"] == "active" and state.get("SubState") == "running"
                     and state["MainPID"] != "0" and process_identity(state) != process_identity(previous))
            if fresh:
                sockets = self.sockets()
                if port_conflicts(state, sockets):
                    raise RuntimeError("Listener conflict after restart on port {}:\n{}".format(self.port, sockets.strip()))
                if sockets.strip():
                    try:
                        message, build = self.http_identity()
                    except RuntimeError as error:
                        last = str(error)
                    else:
                        if self.expect_build and build != self.expect_build:
                            raise RuntimeError("HTTP build mismatch: expected {}, received {}".format(
                                self.expect_build, build or "unavailable"))
                        # Recheck process identity after I/O so a crash/restart cannot pass readiness.
                        after = self.unit(SERVICE)
                        if (after["ActiveState"] == "active" and after.get("SubState") == "running"
                                and process_identity(after) == process_identity(state)):
                            print("Restarted {} (PID {}). {}".format(SERVICE, state["MainPID"], message), flush=True)
                            return
                        last = "systemd process changed during HTTP verification"
                else:
                    last = "systemd PID {} has no listener on port {}".format(state["MainPID"], self.port)
            time.sleep(min(0.2, max(0, self.deadline - time.monotonic())))
        raise RuntimeError("Timed out after {}s: {}".format(self.timeout, last))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "logs", "restart"))
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--lines", type=int, default=50)
    parser.add_argument("--timeout", type=int, default=30)
    parser.add_argument("--expect-build")
    args = parser.parse_args(argv)
    if not 1 <= args.port <= 65535 or not 1 <= args.lines <= 10000 or not 1 <= args.timeout <= 300:
        parser.error("port, lines or timeout is outside its allowed range")
    if args.expect_build is not None and args.action != "restart":
        parser.error("--expect-build requires service restart")
    manager = ServiceManager(args.port, args.lines, args.timeout, args.expect_build)
    try:
        if os.getuid() != 0:
            print("Service management uses sudo to inspect all listener owners and the journal.", flush=True)
            subprocess.run(["sudo", "-v"], check=True)
            manager.privilege = ["sudo", "-n"]
        if args.action == "status":
            manager.report()
        elif args.action == "logs":
            manager.logs()
        else:
            manager.restart()
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print("Service {} failed: {}".format(args.action, error), file=sys.stderr, flush=True)
        if args.action == "restart":
            manager.deadline = None
            for diagnostic in (manager.report, manager.logs):
                try:
                    diagnostic()
                except (OSError, RuntimeError) as detail:
                    print("Diagnostic unavailable: {}".format(detail), file=sys.stderr, flush=True)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
