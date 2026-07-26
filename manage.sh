#!/bin/bash
# manage.py with the wire package on the path — point a camera at this controller.
set -euo pipefail
cd "$(dirname "$0")"
if [ -d ../pyunifiwire/src ]; then
  export PYTHONPATH="$(cd ../pyunifiwire/src && pwd)${PYTHONPATH:+:$PYTHONPATH}"
fi
exec python3 manage.py "$@"
