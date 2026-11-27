#!/usr/bin/env bash
# Arguments: Python installer source, camera server options.
set -e
installer=$1
shift
exec python3 -c "$installer" "$@"
