#!/usr/bin/env bash
# Arguments: port, optional setup file, Python port-check source.
set -e
setup=$2
python3 -c "$3" "$1"

if [ -z "$setup" ]; then
  shopt -s nullglob
  setups=(/opt/ros/*/setup.bash)
  if [ "${#setups[@]}" != 1 ]; then
    printf '%s\n' 'Cannot select the ROS environment. Use --ros-setup /absolute/path/setup.bash.' >&2
    exit 2
  fi
  setup="${setups[0]}"
fi
if [ ! -r "$setup" ]; then
  printf 'Cannot read the ROS setup file: %s\n' "$setup" >&2
  exit 2
fi
# Give the setup its own positional arguments so it cannot change our port.
load_ros_setup() { source "$setup"; }
load_ros_setup
case "$ROS_VERSION" in
  1)
    rospack find web_video_server >/dev/null || {
      printf '%s\n' 'This ROS 1 environment has no web_video_server. See docs/CAMERA_STREAM.md.' >&2
      exit 2
    }
    exec rosrun web_video_server web_video_server _address:=127.0.0.1 "_port:=$1" __name:=lite3_camera_view_$$ ;;
  2)
    ros2 pkg prefix web_video_server >/dev/null || {
      printf '%s\n' 'This ROS 2 environment has no web_video_server. See docs/CAMERA_STREAM.md.' >&2
      exit 2
    }
    exec ros2 run web_video_server web_video_server --ros-args -p address:=127.0.0.1 -p "port:=$1" -p default_qos_profile:=sensor_data ;;
  *) printf '%s\n' 'ROS_VERSION must be 1 or 2 after you load the setup file.' >&2; exit 2 ;;
esac
