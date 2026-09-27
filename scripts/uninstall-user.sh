#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
bin_dir="${HOME}/.local/bin"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
desktop_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
extension_uuid="gnome-desktop-bridge-overlay@woodywizard.github.io"
extension_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/gnome-shell/extensions"

systemctl --user disable --now gnome-desktop-bridge.service >/dev/null 2>&1 || true
gnome-extensions disable "$extension_uuid" >/dev/null 2>&1 || true

remove_our_link() {
  local source="$1"
  local target="$2"
  if [[ -L "$target" && "$(readlink -f -- "$target")" == "$(readlink -f -- "$source")" ]]; then
    rm -- "$target"
  fi
}

for name in gnome-desktop-bridge gnome-desktop-bridge-cli gnome-desktop-bridge-control; do
  remove_our_link "$project_root/scripts/launch.sh" "$bin_dir/$name"
done
remove_our_link \
  "$project_root/systemd/gnome-desktop-bridge.service" \
  "$unit_dir/gnome-desktop-bridge.service"
remove_our_link \
  "$project_root/data/io.github.local.GnomeDesktopBridge.desktop" \
  "$desktop_dir/io.github.local.GnomeDesktopBridge.desktop"
remove_our_link \
  "$project_root/shell-extension/$extension_uuid" \
  "$extension_dir/$extension_uuid"

systemctl --user daemon-reload

if [[ "${1:-}" == "--purge" ]]; then
  rm -rf -- \
    "${XDG_CONFIG_HOME:-${HOME}/.config}/gnome-desktop-bridge" \
    "${XDG_DATA_HOME:-${HOME}/.local/share}/gnome-desktop-bridge"
  echo "Removed the service and all local settings, screenshots, and tokens."
else
  echo "Removed the service. Settings and token were preserved."
  echo "Run '$0 --purge' to delete local bridge data as well."
fi
