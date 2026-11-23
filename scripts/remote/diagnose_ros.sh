#!/usr/bin/env bash
# Arguments: ROS setup file, diagnostic (environment, package, or topics).
setup=$1
# Isolate setup arguments and preserve the diagnostic selector in the caller.
load_ros_setup() { source "$setup"; }
load_ros_setup
case "$2" in
  environment)
    printf 'ROS_VERSION=%s ROS_DISTRO=%s ROS_MASTER_URI=%s ROS_DOMAIN_ID=%s\n' \
      "$ROS_VERSION" "$ROS_DISTRO" "$ROS_MASTER_URI" "$ROS_DOMAIN_ID" ;;
  package)
    if [ "$ROS_VERSION" = 1 ]; then exec rospack find web_video_server;
    else exec ros2 pkg prefix web_video_server; fi ;;
  topics)
    if [ "$ROS_VERSION" = 1 ]; then exec rostopic list -v;
    else exec ros2 topic list -t --no-daemon; fi ;;
esac
