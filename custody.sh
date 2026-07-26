#!/bin/bash
# custody.py with the wire package on the path — move a camera between controllers.
set -euo pipefail
cd "$(dirname "$0")"
if [ -d ../pyunifiwire/src ]; then
  export PYTHONPATH="$(cd ../pyunifiwire/src && pwd)${PYTHONPATH:+:$PYTHONPATH}"
fi
exec python3 custody.py "$@"
