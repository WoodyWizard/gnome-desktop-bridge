#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
bin_dir="${HOME}/.local/bin"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
desktop_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"

install -d -m 0755 "$bin_dir" "$unit_dir" "$desktop_dir"

safe_link() {
  local source="$1"
  local target="$2"
  if [[ -e "$target" && ! -L "$target" ]]; then
    echo "Refusing to replace non-symlink: $target" >&2
    exit 1
  fi
  ln -sfn -- "$source" "$target"
}

for name in gnome-desktop-bridge gnome-desktop-bridge-cli gnome-desktop-bridge-control; do
  safe_link "$project_root/scripts/launch.sh" "$bin_dir/$name"
done
safe_link \
  "$project_root/systemd/gnome-desktop-bridge.service" \
  "$unit_dir/gnome-desktop-bridge.service"
safe_link \
  "$project_root/data/io.github.local.GnomeDesktopBridge.desktop" \
  "$desktop_dir/io.github.local.GnomeDesktopBridge.desktop"

"$bin_dir/gnome-desktop-bridge-cli" access off >/dev/null
systemctl --user daemon-reload
systemctl --user enable --now gnome-desktop-bridge.service

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi

echo "GNOME Desktop Bridge installed and started in Off mode."
echo "Open the app named 'GNOME Desktop Bridge' or run:"
echo "  gnome-desktop-bridge-control"
