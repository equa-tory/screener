#!/usr/bin/env sh
cd "$(dirname "$0")"
if [ ! -d venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv venv
    echo "Installing requirements..."
    venv/bin/pip install -r requirements.txt
fi
venv/bin/python3 server.py "$@"
