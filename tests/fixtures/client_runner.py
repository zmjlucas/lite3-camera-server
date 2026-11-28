"""Run the launcher with Python SSH/SCP substitutes, without a local shell."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from unittest.mock import patch


def stub_command():
    name, *arguments = sys.argv[2:]
    with open(os.environ["CAMERA_TEST_LOG"], "a", encoding="utf-8") as log:
        log.write(json.dumps([name] + arguments) + "\n")
    if name == "scp" and os.environ.get("CAMERA_TEST_COPY_LOG"):
        with open(os.environ["CAMERA_TEST_COPY_LOG"], "a", encoding="utf-8") as log:
            log.write(json.dumps(Path(arguments[-2]).read_text(encoding="utf-8")) + "\n")
    if "--source" in arguments[-1]:
        print("Shared camera server on port 8080", flush=True)
    if os.environ.get("CAMERA_TEST_PID_FILE"):
        Path(os.environ["CAMERA_TEST_PID_FILE"]).write_text(str(os.getpid()), encoding="utf-8")
        time.sleep(30)
    return int(os.environ.get("CAMERA_TEST_EXIT", "0"))


def main():
    if sys.argv[1] == "--ssh-stub":
        return stub_command()
    sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent))
    spec = importlib.util.spec_from_file_location("camera_cli", sys.argv[1])
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    cli.macos_route = lambda address: None
    original_popen = subprocess.Popen

    def start(command, *args, **kwargs):
        if command[0] in ("ssh", "scp"):
            command = [sys.executable, __file__, "--ssh-stub"] + list(command)
        return original_popen(command, *args, **kwargs)

    with patch.object(subprocess, "Popen", start):
        return cli.main(sys.argv[2:])


if __name__ == "__main__":
    sys.exit(main())
