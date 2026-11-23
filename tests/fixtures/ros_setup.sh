if [ "$#" -ne 0 ]; then
  printf '%s\n' 'Unexpected arguments passed to the ROS setup file' >&2
  exit 2
fi
export ROS_VERSION=__ROS_VERSION__
export PATH=__BIN_DIRECTORY__:$PATH
# A setup file may use these names and change its positional arguments.
port=11311
diagnostic=unrelated
set -- setup internal arguments
