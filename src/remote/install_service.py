"""Install the native server's systemd boot service as the SSH deployment user."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile


SERVICE = "lite3-camera-server.service"


def unit_quote(value, command=False):
    """Quote one systemd value, including its specifier/command expansions."""
    value = str(value)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("Control characters are not allowed in service paths or arguments")
    value = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    if command:
        value = value.replace("$", "$$")
    return '"' + value + '"'


def install_service(home, options):
    home = Path(home).absolute()
    directory = home / "lite3-camera-server"
    server = directory / "server.py"
    if not server.is_file():
        raise RuntimeError("The deployed camera server is missing: " + str(server))
    # systemd expands dollars in arguments, but not in its executable path. env
    # keeps a Python path containing a literal dollar in the argument position.
    command = ["/usr/bin/env", sys.executable, "-u", str(server), "--retry-camera"] + options
    unit = "\n".join([
        "[Unit]",
        "Description=Lite3 Camera Server",
        "After=network.target",
        "RequiresMountsFor=" + unit_quote(directory),
        "StartLimitIntervalSec=0",
        "",
        "[Service]",
        "Type=simple",
        "User=" + str(os.getuid()),
        "Environment=" + unit_quote("HOME=" + str(home)),
        "ExecStart=" + " ".join(unit_quote(argument, command=True) for argument in command),
        "Restart=on-failure",
        "RestartSec=5",
        "TimeoutStopSec=15",
        "StandardOutput=journal",
        "StandardError=journal",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
        "",
    ])
    # Detect an unavailable system manager before requesting privileges or writing /etc.
    subprocess.run(["systemctl", "show", "--property=Version", "--value"],
                   stdout=subprocess.DEVNULL, check=True)
    privilege = []
    if os.getuid() != 0:
        print("Installing the camera boot service requires sudo on the perception host.", flush=True)
        subprocess.run(["sudo", "-v"], check=True)
        privilege = ["sudo", "-n"]
    with tempfile.TemporaryDirectory(prefix="lite3-camera-service-") as temporary:
        source = Path(temporary) / SERVICE
        source.write_text(unit, encoding="utf-8")
        subprocess.run(privilege + ["install", "-o", "root", "-g", "root", "-m", "644",
                                   str(source), "/etc/systemd/system/" + SERVICE], check=True)
    subprocess.run(privilege + ["systemctl", "daemon-reload"], check=True)
    subprocess.run(privilege + ["systemctl", "enable", SERVICE], check=True)
    print("Enabled {} for every boot. Current camera processes were left running.\n"
          "To apply the boot settings now, stop any manually started camera server, then run:\n"
          "  sudo systemctl restart {}\n"
          "Check service logs with: journalctl -u {} -b".format(SERVICE, SERVICE, SERVICE), flush=True)


def main():
    try:
        install_service(Path.home(), sys.argv[1:])
    except (OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print("Camera boot service installation failed: {}. A running systemd host and root/sudo "
              "access are required.".format(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
