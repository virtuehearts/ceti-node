#!/bin/bash
# Uncensored Hub autorun (Linux / WSL / macOS)
cd "$(dirname "$0")"
exec python3 autorun.py "$@"
