#!/bin/sh
# Start The Won't-Do List and open it in your browser (macOS and Linux).
# Anything you add is passed on, for example: ./start.sh --port 9000

cd "$(dirname "$0")" || exit 1

for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 && "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' 2>/dev/null; then
    exec "$candidate" -m wontdo serve --open "$@"
  fi
done

echo "The Won't-Do List needs Python 3.9 or newer: https://www.python.org/downloads/" >&2
exit 1
