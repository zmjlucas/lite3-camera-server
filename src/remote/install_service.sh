#!/usr/bin/env bash
# Arguments: Python installer source, dev server options.
set -e
installer=$1
shift
exec python3 -c "$installer" "$@"
