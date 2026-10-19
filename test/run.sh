#!/bin/sh
# Run the whole test suite: test/run.sh [-v] [pattern]
# Uses python3 from PATH; set PYTHON=/usr/bin/python3 to test with another interpreter.
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd -P)
py=${PYTHON:-python3}
cd "$here" || exit 1
exec "$py" -W ignore::ResourceWarning -m unittest discover -s "$here" -p "${2:-test_*.py}" ${1:--v}
