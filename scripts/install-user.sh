#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
project_root="$(cd -- "$script_dir/.." && pwd)"
bin_dir="${HOME}/.local/bin"
unit_dir="${XDG_CONFIG_HOME:-${HOME}/.config}/systemd/user"
desktop_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/applications"
extension_uuid="gnome-desktop-bridge-overlay@woodywizard.github.io"
extension_dir="${XDG_DATA_HOME:-${HOME}/.local/share}/gnome-shell/extensions"

# The service, launchers, and Shell extension run straight from this tree, so
# anyone who can write to it could run code as you.
writable="$(find "$project_root" -path "$project_root/.git" -prune -o -perm /022 -print -quit)"
if [[ -n "$writable" ]]; then
  echo "Refusing to install: $writable is writable by other users." >&2
  echo "Fix with: chmod -R go-w '$project_root'" >&2
  exit 1
fi

install -d -m 0755 "$bin_dir" "$unit_dir" "$desktop_dir" "$extension_dir"

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
safe_link \
  "$project_root/shell-extension/$extension_uuid" \
  "$extension_dir/$extension_uuid"

"$bin_dir/gnome-desktop-bridge-cli" access off >/dev/null
systemctl --user daemon-reload
systemctl --user enable --now gnome-desktop-bridge.service

if command -v update-desktop-database >/dev/null 2>&1; then
  update-desktop-database "$desktop_dir" >/dev/null 2>&1 || true
fi

echo "GNOME Desktop Bridge installed and started in Off mode."
echo "Open the app named 'GNOME Desktop Bridge' or run:"
echo "  gnome-desktop-bridge-control"

# GNOME Shell on Wayland discovers new extensions only at login, so enabling
# may fail until the next session; the setting is remembered either way.
if command -v gnome-extensions >/dev/null 2>&1 \
  && gnome-extensions enable "$extension_uuid" >/dev/null 2>&1; then
  echo "On-screen animations: the Desktop Bridge Overlay extension is enabled."
else
  if command -v gsettings >/dev/null 2>&1; then
    current="$(gsettings get org.gnome.shell enabled-extensions 2>/dev/null || echo "@as []")"
    if [[ "$current" != *"$extension_uuid"* ]]; then
      if [[ "$current" == "@as []" || "$current" == "[]" ]]; then
        updated="['$extension_uuid']"
      else
        updated="${current%]}, '$extension_uuid']"
      fi
      gsettings set org.gnome.shell enabled-extensions "$updated" 2>/dev/null || true
    fi
  fi
  echo "On-screen animations: log out and back in to load the Desktop Bridge Overlay extension."
fi
