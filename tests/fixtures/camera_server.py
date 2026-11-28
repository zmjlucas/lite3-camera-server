"""Run the real server with a test-only capture input for subprocess tests."""
import importlib.util
from pathlib import Path
import sys
from unittest.mock import patch


def camera_frames(options, stop):
    jpeg = (Path(__file__).resolve().parent / "camera-frame.jpg").read_bytes()
    while not stop.is_set():
        yield jpeg
        stop.wait(1.0 / options.fps)


if __name__ == "__main__":
    # Match the sibling-module search path of executing src/server.py directly.
    sys.path.insert(0, str(Path(sys.argv[1]).resolve().parent))
    spec = importlib.util.spec_from_file_location("camera_server_under_test", sys.argv[1])
    server = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(server)
    import camera
    with patch.object(camera, "v4l2_frames", camera_frames):
        sys.exit(server.main(sys.argv[2:]))
