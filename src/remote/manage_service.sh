#!/usr/bin/env bash
# Arguments: Python service manager source, action and inspection options.
set -e
manager=$1
shift
exec python3 -c "$manager" "$@"
