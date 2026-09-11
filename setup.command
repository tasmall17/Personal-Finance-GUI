#!/usr/bin/env bash
# Double-click this file in Finder to install Flightdeck.
cd "$(dirname "$0")" || exit 1
# `bash ./setup.sh`, not `./setup.sh`: downloading the project as a zip rather
# than cloning it strips the executable bit, and the double-click route is
# exactly where that happens.
chmod +x ./setup.sh ./run.sh 2>/dev/null || true
bash ./setup.sh
echo ""
read -n 1 -s -r -p "Press any key to close this window."
