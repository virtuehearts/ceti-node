#!/bin/bash
# Uncensored Hub launcher (Linux/macOS): python3 -m hub "$@"
cd "$(dirname "$0")"
exec python3 -m hub "$@"
