#!/usr/bin/env bash
set -euo pipefail

invoked_name="$(basename -- "$0")"
script_path="$(readlink -f -- "$0")"
project_root="$(cd -- "$(dirname -- "$script_path")/.." && pwd)"
export PYTHONPATH="$project_root${PYTHONPATH:+:$PYTHONPATH}"

case "$invoked_name" in
  gnome-desktop-bridge)
    exec python3 -m gnome_desktop_bridge.server "$@"
    ;;
  gnome-desktop-bridge-cli)
    exec python3 -m gnome_desktop_bridge.cli "$@"
    ;;
  gnome-desktop-bridge-control)
    exec python3 -m gnome_desktop_bridge.control_center "$@"
    ;;
  *)
    echo "Unknown launcher name: $invoked_name" >&2
    exit 2
    ;;
esac
