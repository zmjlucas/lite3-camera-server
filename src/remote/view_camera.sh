#!/usr/bin/env bash
# Arguments: Python shared-start source, port, camera server options.
set -e
helper=$1
port=$2
shift 2
server="$HOME/lite3-camera-server/server.py"
python3 -c "$helper" "$server" "$port" "$@"
# Only this idle SSH command belongs to the client. The shared server is detached.
exec sleep infinity
