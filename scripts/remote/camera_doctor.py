"""Read the perception host environment without opening a camera.

Arguments contain the ROS diagnostic shell source and Python module probe source,
so this check can run over SSH before the server is deployed.
"""
import glob
import json
import platform
import subprocess
import sys


def check(label, argv):
    print("\n" + label, flush=True)
    try:
        result = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, timeout=10)
        print(result.stdout[:16000].strip() or "(no output)", flush=True)
        print("exit=" + str(result.returncode), flush=True)
    except (OSError, subprocess.TimeoutExpired) as exc:
        print(str(exc), flush=True)


def main():
    ros_diagnostic, module_probe = sys.argv[1:]
    print("Host: " + platform.platform(), flush=True)
    print("Python: " + sys.version.split()[0], flush=True)
    check("OS", ["cat", "/etc/os-release"])
    for module in ("numpy", "cv2", "pyrealsense2"):
        check("Python binding: " + module, [sys.executable, "-c", module_probe, module])
    print("Video device nodes: " + json.dumps(glob.glob("/dev/video*") + glob.glob("/dev/v4l/by-id/*")), flush=True)
    check("Camera service status", ["systemctl", "is-active", "realsense", "realsense_ros2"])
    setups = sorted(glob.glob("/opt/ros/*/setup.bash"))
    print("ROS setup files: " + json.dumps(setups), flush=True)
    for setup in setups:
        for mode, label in (("environment", "ROS environment"),
                            ("package", "web_video_server"),
                            ("topics", "ROS topics (check the image topic name)")):
            check(label + ": " + setup,
                  ["bash", "-lc", ros_diagnostic, "lite3-camera-doctor", setup, mode])


if __name__ == "__main__":
    main()
