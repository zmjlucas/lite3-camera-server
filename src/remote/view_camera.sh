#!/usr/bin/env bash
# Arguments: Python port-check source, port, camera server options.
set -e
python3 -c "$1" "$2"
shift 2
server="$HOME/lite3-camera-server/server.py"
if [ ! -f "$server" ]; then
  printf '%s\n' 'The camera server script is missing. Run the deploy command first.' >&2
  exit 2
fi
exec python3 -u "$server" "$@"
