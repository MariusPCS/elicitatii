#!/bin/zsh
# Double-click in Finder to start the viewer and open it in your browser.
cd "$(dirname "$0")"
exec python3 server.py "$@"
