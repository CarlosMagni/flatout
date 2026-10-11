#!/bin/bash
# Double-click: installs (or updates) RaceLab then builds RaceLab.app.
# Safe to run again: whatever already exists is reused or replaced.
# Copies nothing into /Applications and never uses sudo.

cd "$(dirname "$0")" || exit 1
ROOT="$PWD"

# Terminal runs this script with a reduced PATH: add the usual Python locations.
export PATH="$HOME/.pyenv/shims:/opt/homebrew/bin:/usr/local/bin:$PATH"

finish() {  # finish [code]: leaves the window readable before closing it
  echo
  printf "Press Enter to close. "
  read -r _
  exit "${1:-0}"
}

echo "=== Installing RaceLab ==="
echo "Folder: $ROOT"
echo

# --- 1. Python >= 3.11
find_python() {
  local c p
  for c in python3.12 python3.11 python3; do
    p="$(command -v "$c" 2>/dev/null)" || continue
    if "$p" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
      echo "$p"
      return 0
    fi
  done
  return 1
}

PY="$(find_python)" || {
  echo "Python 3.11 or newer was not found."
  echo "  - with Homebrew:  brew install python@3.12"
  echo "  - or download it from https://www.python.org/downloads/macos/"
  echo "Then double-click \"Install RaceLab.command\" again."
  finish 1
}
echo "Python: $PY"
"$PY" install.py
finish $?
