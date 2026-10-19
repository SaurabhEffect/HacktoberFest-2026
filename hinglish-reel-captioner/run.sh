#!/usr/bin/env bash
# Starts Hinglish Reel Captioner. The first run sets up a private Python environment (about a minute).
#
#   ./run.sh           real transcription (a Whisper model downloads the first time you caption something)
#   ./run.sh --demo    no model download: plays a sample script so you can try the whole studio
#
# Any other option is passed through, e.g.  ./run.sh --model medium --port 8001
set -euo pipefail
cd "$(dirname "$0")"

PYTHON="${PYTHON:-python3}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "Python was not found. Install Python 3.10 or newer from https://www.python.org/downloads/ and try again." >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "This needs Python 3.10 or newer, but '$PYTHON' is $("$PYTHON" -c 'import platform; print(platform.python_version())')." >&2
  echo "Install a newer one, then run it like this:  PYTHON=python3.12 ./run.sh" >&2
  exit 1
fi

if [ ! -f .venv/.installed ]; then
  echo "First run: setting things up. This takes about a minute and needs an internet connection."
  "$PYTHON" -m venv .venv
  .venv/bin/python -m pip install --quiet --upgrade pip
  .venv/bin/python -m pip install -r requirements.txt
  touch .venv/.installed
fi

if ! command -v ffmpeg >/dev/null 2>&1; then
  echo "Note: FFmpeg was not found, and captioning needs it to read your videos." >&2
  echo "  macOS: brew install ffmpeg        Ubuntu/Debian: sudo apt install ffmpeg" >&2
fi

exec .venv/bin/python -m reelcaptioner "$@"
