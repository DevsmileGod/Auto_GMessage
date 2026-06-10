#!/usr/bin/env bash
set -e

cd "$(dirname "$0")"

echo "Gmail Auto Sender"
echo "================="

if ! command -v python3 >/dev/null 2>&1; then
  echo "Python 3 is not installed."
  echo "Install Python 3.10+ and try again."
  exit 1
fi

echo "Installing dependencies..."
python3 -m pip install -r requirements.txt -q

echo "Starting app..."
python3 main.py
