# GNOME Desktop Bridge

A local, user-managed bridge for observing, debugging, and automating the GNOME Desktop on Wayland. The project provides a local AI client or diagnostic script with semantic access to application interfaces via AT-SPI, and, only after explicit user consent in the GNOME system dialog, control of the pointer and keyboard through the XDG Desktop Portal.

> **Status:** working MVP for GNOME 50 / Fedora 44 and Python 3.11+.
> By default, everything is disabled. This is a powerful tool, not a security boundary
> between untrusted processes running as the same Linux user.

## Capabilities at a Glance

- local HTTP/JSON API on `127.0.0.1:18766`;
- persistent random bearer token with file permissions `0600`;
- four modes: `Off`, `Observe`, `Control selected apps`, `ALL DESKTOP`;
- allowlist of applications for normal Control mode;
- reading semantic window trees through AT-SPI;
- searching for roles, names, states, bounds, text, and available actions of elements;
- invoking native AT-SPI actions, focusing elements, and filling editable fields;
- automatic masking of AT-SPI password fields;
- screenshots through the XDG Screenshot portal;
- global clicks, mouse movement, scroll, keys, and text input through the XDG RemoteDesktop portal;
- launching `.desktop` applications via a separate transient systemd unit;
- command log in memory and SSE event stream in real-time;
- native GTK/libadwaita control panel;
- emergency `STOP ALL`, which closes the portal session and returns to mode `Off`;
- systemd user service with hardening settings;
- CLI for human users and automated clients.

## What the Project Deliberately Does Not Do

- does not bypass Wayland or GNOME permission dialogs;
- does not obtain root or execute shell commands through the API;
- does not control the login screen, a locked session, or another Linux user;
- does not read arbitrary process memory;
- does not automatically obtain internal logs of other applications or extensions — only what they display via AT-SPI, on the screen, or in normal files/logs to which the current user already has access;
- does not expose the API to the LAN;
- does not allow websites to access the API through CORS;
- does not permit the API client to enable a more dangerous access mode by itself.

## Access Model

| Mode | UI Reading | Screenshot | AT-SPI Actions | Application Scope | Global Input Events |
|---|---:|---:|---:|---|---:|
| `off` | no | no | no | — | no |
| `observe` | yes | by feature-gate | no | any visible AT-SPI application | no |
| `control` | yes | by feature-gate | yes | only `allowedApps` | no |
| `all` | yes | by feature-gate | yes | entire desktop | only by separate feature-gate and portal consent |

Commands `ping`, `capabilities`, `get_state`, and `stop_all` are available in any mode, but require a token. The only public endpoint is the minimal `/health`.

### Why Both Mode and Feature-Gate Exist

Permission is checked in two places. For example, a global click requires:

1. `accessMode: "all"`;
2. `allowPortalInput: true`;
3. an active RemoteDesktop session that the user has confirmed in the GNOME dialog;
4. actual portal permission for pointer.

Changing the settings to a safer configuration automatically closes the active portal session within one second. This makes the switch in Control Center a true revocation mechanism, not merely a setting for the next request.

`ALL DESKTOP` is additionally session-scoped: after crash, logout, reboot, or manual daemon restart, it automatically resets to `Off`, and the global-input gate is turned off. `Observe` and scoped `Control` can persist across restarts.

## Architecture

```text
AI / CLI / diagnostic process
          |
          | HTTP + Bearer token, loopback only
          v
  gnome-desktop-bridge daemon
          |
          +-- Policy ------ settings.json (human-owned)
          +-- EventBuffer - JSON audit + SSE
          +-- AT-SPI ------ semantic UI tree/actions
          +-- XDG Portal -- screenshot + consented input
          +-- Gio/systemd - desktop app discovery/launch
          |
          v
  Active GNOME user session on Wayland

GNOME Desktop Bridge Control Center
          |
          +-- changes settings.json directly
          +-- starts/stops portal session through daemon
          +-- can always invoke STOP ALL
```

The solution is intentionally not a GNOME Shell extension. The main logic operates as a regular user service: this is simpler to test, update, and isolate. A shell indicator can be added later as an optional visual frontend.

### Backends

- **AT-SPI 2** — semantic tree, roles, states, text, actions, focus, editable text.
- **XDG Screenshot portal** — screenshots of the screen selected or permitted by system policy.
- **XDG RemoteDesktop + ScreenCast portals** — system consent, pointer/keyboard events,
  stream geometry for multiple monitors.
- **Gio AppInfo** — list of desktop launchers.
- **systemd-run + gtk-launch** — application launch outside the sandbox namespace of the daemon.

Official specifications:

- [RemoteDesktop portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.RemoteDesktop.html)
- [ScreenCast portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.ScreenCast.html)
- [libei](https://libinput.pages.freedesktop.org/libei/)
- [AT-SPI Accessible](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/class.Accessible.html)
- [AT-SPI Action](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/iface.Action.html)
- [AT-SPI EditableText](https://gnome.pages.gitlab.gnome.org/at-spi2-core/libatspi/iface.EditableText.html)

## Requirements

A typical Fedora Workstation with GNOME already includes everything necessary:

- GNOME + Wayland;
- Python 3.11+;
- PyGObject;
- `at-spi2-core`;
- `xdg-desktop-portal` and `xdg-desktop-portal-gnome`;
- systemd user manager;
- `gtk-launch`;
- GTK 4 and libadwaita for Control Center.

Verification on Fedora:

```bash
python3 -c 'import gi; gi.require_version("Atspi", "2.0")'
rpm -q at-spi2-core xdg-desktop-portal xdg-desktop-portal-gnome python3-gobject
```

## Installation

From the project root:

```bash
chmod +x scripts/*.sh
./scripts/install-user.sh
```

Installer:

1. creates secure config/data directories;
2. creates settings in `Off` mode;
3. generates a token once;
4. creates symlink commands in `~/.local/bin`;
5. installs systemd user unit and desktop entry;
6. starts the daemon.

Re-running the installer also forces fail-closed `access off`, so an update cannot silently preserve a previously enabled `ALL DESKTOP` mode.

Open the panel:

```bash
gnome-desktop-bridge-control
```

Check the daemon:

```bash
systemctl --user status gnome-desktop-bridge
gnome-desktop-bridge-cli status
```

Uninstall without deleting the token and settings:

```bash
./scripts/uninstall-user.sh
```

Complete removal of local data, screenshots, and token:

```bash
./scripts/uninstall-user.sh --purge
```

## First Secure Launch

1. Open **GNOME Desktop Bridge**.
2. Select `Observe`.
3. Leave `Redact protected text` enabled.
4. Click **Apply**.
5. Check `list_apps` and one snapshot.
6. For actions, select `Control selected apps` and specify the allowlist, for example
   `org.mozilla.firefox, Firefox`.
7. Use `ALL DESKTOP` only during the task.
8. To enable global clicks, turn on `Global pointer and keyboard`, save the settings,
   click **Start session** and confirm the GNOME system dialog.
9. After the task, click **STOP ALL**.

## Local Files

By default:

| Purpose | Path | Permissions |
|---|---|---:|
| Settings | `~/.config/gnome-desktop-bridge/settings.json` | `0600` |
| Bearer token | `~/.local/share/gnome-desktop-bridge/token` | `0600` |
| Portal restore token | `~/.local/share/gnome-desktop-bridge/portal-restore-token` | `0600` |
| Screenshots | `~/.local/share/gnome-desktop-bridge/screenshots/` | dir `0700`, files `0600` |
| Runtime lock | `$XDG_RUNTIME_DIR/gnome-desktop-bridge/daemon.lock` | `0600` |

The bridge honors `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_RUNTIME_DIR`.

### Token Persistence

The token is generated only if the token file does not exist. Restarting the daemon, GNOME, or the entire computer does not change it. It will only change after manual rotation or deletion of the file.

Show the token:

```bash
gnome-desktop-bridge-cli token
```

Rotate and then restart the daemon:

```bash
gnome-desktop-bridge-cli token --rotate
systemctl --user restart gnome-desktop-bridge
```

Do not place the token in Git, screenshots, issue reports, or shell history. For `curl`, it is better to read it from the file into a variable of the current shell:

```bash
TOKEN="$(<~/.local/share/gnome-desktop-bridge/token)"
```

### Format of settings.json

```json
{
  "version": 1,
  "accessMode": "control",
  "allowedApps": ["org.mozilla.*", "Firefox"],
  "allowScreenshots": true,
  "allowPortalInput": false,
  "allowLaunchApps": false,
  "persistPortalSession": false,
  "redactProtectedText": true,
  "maxEvents": 2000,
  "host": "127.0.0.1",
  "port": 18766
}
```

`host` accepts only loopback (`127.0.0.1`, `::1`, `localhost`). Changing host/port requires a restart; other daemon values are automatically picked up.

`allowedApps` contains case-insensitive shell-style globs matched against the AT-SPI application name, application ID, toolkit name, and PID. Prefer a desktop/application ID over a PID.

## HTTP API

### Transport

- base URL: `http://127.0.0.1:18766`;
- JSON UTF-8;
- maximum POST body size: 1 MiB;
- `Content-Type: application/json` is required;
- unknown top-level fields and action arguments are rejected, not ignored;
- all `/api/*` require `Authorization: Bearer <token>`;
- CORS is intentionally absent;
- responses contain `Cache-Control: no-store`.

Success:

```json
{"ok": true, "result": {}}
```

Error:

```json
{
  "ok": false,
  "error": {
    "code": "access_denied",
    "message": "pointer_click is available only in ALL DESKTOP mode",
    "details": {"requiredMode": "all", "currentMode": "control"}
  }
}
```

Typical error codes: `unauthorized`, `invalid_request`, `access_denied`, `not_found`,
`stale_reference`, `backend_unavailable`, `portal_cancelled`, `portal_denied`,
`device_not_granted`, `internal_error`.

### Endpoints

| Method | Path | Auth | Purpose |
|---|---|---:|---|
| `GET` | `/health` | no | minimal liveness check |
| `GET` | `/api/status` | yes | mode, feature gates, portal state |
| `POST` | `/api/command` | yes | execute one command |
| `GET` | `/api/events?after=0&limit=200` | yes | get audit events |
| `GET` | `/api/events/stream?after=0` | yes | Server-Sent Events stream |

Command format:

```json
{
  "action": "snapshot",
  "args": {
    "ref": "g12:n3",
    "depth": 8,
    "maxNodes": 1000,
    "includeText": true
  }
}
```

`curl` example:

```bash
curl --fail-with-body \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  --data '{"action":"get_state","args":{}}' \
  http://127.0.0.1:18766/api/command
```

## Full Command Catalog

### Always Allowed After Authentication

#### `ping`

Checks the request path and daemon version.

```json
{"action":"ping","args":{}}
```

#### `capabilities`

Returns versions and availability of AT-SPI/portal, modes, and list of actions. This is the first command that a new AI client must call.

```json
{"action":"capabilities","args":{}}
```

#### `get_state`

Returns current policy and RemoteDesktop state, but never returns a token.

```json
{"action":"get_state","args":{}}
```

#### `stop_all`

Fail-closed emergency operation:

- closes the RemoteDesktop portal session;
- sets `accessMode: off`;
- sets `allowPortalInput: false`;
- adds `security.stop_all` to audit.

Result contains `settingsPersisted`. Even if the filesystem does not allow saving the file, the daemon immediately retains the fail-closed `Off` in memory and reports `false` instead of re-enabling commands under the old policy.

```json
{"action":"stop_all","args":{}}
```

### Observe or Higher

#### `list_apps`

Returns AT-SPI applications and refs:

```json
{"action":"list_apps","args":{}}
```

Abbreviated result:

```json
{
  "generation": 4,
  "apps": [
    {
      "ref": "g4:n1",
      "name": "Firefox",
      "appId": "org.mozilla.firefox",
      "toolkitName": "Gecko",
      "pid": 12345,
      "role": "application",
      "childCount": 2
    }
  ]
}
```

#### `snapshot`

Reads the tree of the selected application/window/widget.

Arguments:

- `ref` — required ref from the current generation;
- `depth` — `0..30`, default `8`;
- `maxNodes` — `1..5000`, default `1000`;
- `includeText` — default `true`.

```json
{
  "action":"snapshot",
  "args":{"ref":"g4:n1","depth":10,"maxNodes":1500,"includeText":true}
}
```

Each node may contain:

- `ref`, `name`, `role`, `description`;
- `states`, `interfaces`, `childCount`;
- `bounds: {x,y,width,height}`;
- `actions: [{index,name,description,keyBinding}]`;
- `text`, `value`;
- `protected: true` and `text: "[REDACTED]"` for password role;
- nested `children`.

To protect the daemon, one text node is limited to 4096 characters, and the total text budget of a single snapshot is 256,000 characters; truncated nodes receive `textTruncated: true`. Names, descriptions, interfaces, and action metadata also have conservative limits.

When `redactProtectedText` is enabled, the bridge does not request `name`, `description`, or `Text` content from password-role widgets: the response immediately includes `[REDACTED]`.

#### `screenshot`

Arguments:

- `interactive` — request portal to show interactive selector, default `false`;
- `includeBase64` — return PNG base64 along with local path, default `false`, limit 25 MiB.

```json
{"action":"screenshot","args":{"interactive":false,"includeBase64":false}}
```

Result:

```json
{
  "path":"/home/user/.local/share/gnome-desktop-bridge/screenshots/screenshot-....png",
  "size":321456,
  "mimeType":"image/png"
}
```

#### `list_launchers`

Searches for visible desktop entries. Arguments: `query` (substring), `limit` (`1..1000`).

```json
{"action":"list_launchers","args":{"query":"terminal","limit":20}}
```

### Control Selected Apps or ALL

#### `invoke`

Triggers the native AT-SPI action. The preferred way to click a button.

```json
{"action":"invoke","args":{"ref":"g5:n42","action":"click"}}
```

`args.action` can be an exact action name or index, default is `0`.

#### `click`

Convenience command with two methods:

- `method: "action"` — AT-SPI Action, available in scoped Control;
- `method: "coordinates"` — center bounds + portal click, requires ALL DESKTOP,
  global-input gate and an active portal session.

```json
{
  "action":"click",
  "args":{"ref":"g5:n42","method":"action","action":0}
}
```

Coordinates fallback:

```json
{
  "action":"click",
  "args":{"ref":"g5:n42","method":"coordinates","button":"left"}
}
```

#### `focus`

```json
{"action":"focus","args":{"ref":"g5:n51"}}
```

Triggers `Atspi.Component.grab_focus()`.

#### `fill`

```json
{"action":"fill","args":{"ref":"g5:n51","text":"hello@example.com"}}
```

Triggers `Atspi.EditableText.set_text_contents()`. Maximum 100,000 characters.
Text is never recorded in audit; only length and redaction marker are saved.

#### `launch_app`

Requires `allowLaunchApps` to be enabled. In Control, the desktop ID/name must also match
`allowedApps`.

```json
{"action":"launch_app","args":{"desktopId":"org.gnome.Terminal.desktop"}}
```

### Only ALL DESKTOP + Global-Input Gate

#### `start_remote_desktop`

```json
{"action":"start_remote_desktop","args":{}}
```

The daemon sequentially creates a RemoteDesktop session, requests keyboard+pointer,
adds one monitor ScreenCast source and calls Start. The user selects the screen
and confirms access in the GNOME system dialog. Without consent, the command ends
with `portal_cancelled` or `portal_denied`.

If `persistPortalSession` is enabled and the portal supports version 2, the restore token
is saved locally. This does not prevent GNOME or the user from revoking access.

#### `stop_remote_desktop`

Closes only the portal session. For full disconnection, use `stop_all`.

```json
{"action":"stop_remote_desktop","args":{}}
```

#### `pointer_move`

Global logical screen coordinates:

```json
{"action":"pointer_move","args":{"x":1280,"y":720}}
```

Daemon selects a shared stream by portal `position`/`size`, translates global coordinates
to stream-local and calls `NotifyPointerMotionAbsolute`.

#### `pointer_click`

Optional `x` and `y` first move the pointer. They must be passed together.

```json
{"action":"pointer_click","args":{"button":"left","x":1280,"y":720}}
```

Buttons: `left`, `right`, `middle`, `side`, `extra` or numeric Linux evdev code.

#### `scroll`

Continuous scroll deltas:

```json
{"action":"scroll","args":{"dx":0,"dy":-120}}
```

#### `key`

`key` — GDK keysym name, one Unicode character or numeric keysym. `event` — `tap`,
`press`, `release`.

```json
{"action":"key","args":{"key":"Return","event":"tap"}}
```

For keyboard shortcuts, modifier press/release events must be sent explicitly:

```json
{"action":"key","args":{"key":"Control_L","event":"press"}}
{"action":"key","args":{"key":"l","event":"tap"}}
{"action":"key","args":{"key":"Control_L","event":"release"}}
```

The client is responsible for releasing modifiers even after an error.

#### `type_text`

```json
{"action":"type_text","args":{"text":"Hello, GNOME!","intervalMs":10}}
```

Maximum 10,000 characters, interval `0..1000` ms. Plaintext is excluded from audit.
For regular editable fields, `fill` is preferred: it is faster and independent of layout.

## Semantic Refs and Generations

Refs such as `g12:n84` are intentionally short-lived.

- `list_apps` starts a new generation;
- `snapshot` starts the next generation and returns new refs;
- the subsequent discovery call makes old refs stale;
- a stale action returns HTTP 409 `stale_reference`;
- refs should not be saved between tasks or daemon restarts.

This reduces the risk of clicking on the wrong element after UI changes. The correct cycle is:

1. `list_apps`;
2. select an application ref;
3. `snapshot`;
4. select an element ref from this snapshot;
5. immediately perform one action;
6. get a new snapshot before the next important action.

## Audit and Real-Time Events

Events are stored in daemon memory. The maximum is set by `maxEvents`; after a restart, the history is reset. A persistent operational log is available in the systemd journal.

Get batch:

```bash
gnome-desktop-bridge-cli events --after 0 --limit 200
```

Monitor in real-time:

```bash
gnome-desktop-bridge-cli events --follow
```

Raw SSE:

```bash
curl -N -H "Authorization: Bearer $TOKEN" \
  http://127.0.0.1:18766/api/events/stream?after=0
```

Main event types:

- `daemon.started`, `daemon.stopping`;
- `command.started`, `command.completed`, `command.failed`;
- `portal.session.started`, `portal.session.stopped`, `portal.session.revoked`;
- `security.stop_all`, `security.startup_downgrade`.

Audit does not contain `fill.text`, `type_text.text` or screenshot base64. It may contain
application names, refs, coordinates, key names, and local screenshot paths.

## CLI

```bash
# State and capabilities
gnome-desktop-bridge-cli status
gnome-desktop-bridge-cli capabilities

# Generic action
gnome-desktop-bridge-cli call list_apps
gnome-desktop-bridge-cli call snapshot --args '{"ref":"g1:n1","depth":6}'

# Human-controlled modes
gnome-desktop-bridge-cli access observe
gnome-desktop-bridge-cli access control --app 'org.mozilla.*' --app 'Firefox'

# ALL requires interactive phrase unless an explicit acknowledgement is supplied
gnome-desktop-bridge-cli access all --portal-input
gnome-desktop-bridge-cli access all --portal-input --yes-i-understand

# Feature gates
gnome-desktop-bridge-cli feature screenshots on
gnome-desktop-bridge-cli feature launch-apps off

# Emergency stop
gnome-desktop-bridge-cli stop-all
```

`access` and `feature` modify human-owned settings locally. In the HTTP API, there is intentionally no
`set_access`: AI, having received a token, cannot elevate its own level independently.

## AI Client Instruction

An AI that encounters this project for the first time must follow this protocol:

1. Do not assume that the bridge is running: check `/health`.
2. Read the token from the user-specified file without printing it in the response/log.
3. Call `capabilities`, then `get_state`.
4. If the mode is insufficient, ask the person to change it in the Control Center. Do not change
   `settings.json` yourself.
5. Start with AT-SPI: `list_apps` → `snapshot` → semantic action.
6. Select the application by `appId`; use name as a fallback.
7. Before an irreversible action, update the snapshot and compare name/role/state.
8. Prefer `invoke`/`fill` to coordinate events.
9. Do not read password fields; consider `[REDACTED]` as the final value.
10. For global input, ensure `accessMode == all`, feature gate, and active portal session.
11. Do not start a portal session without a direct user task: the dialog requires attention.
12. After the task, call `stop_all` if ALL DESKTOP is no longer needed.
13. If a modifier was pressed, send the corresponding release event in a `finally` block.
14. On `stale_reference`, repeat discovery instead of guessing a new ref.
15. On `portal_cancelled`, do not repeat the dialog indefinitely.
16. Never perform purchases, sends, submissions, deletes, or other irreversible actions without separate, current user confirmation.

Recommended machine workflow:

```text
health
  -> capabilities
  -> get_state
  -> list_apps
  -> select exact appId
  -> snapshot
  -> identify exact role/name/action
  -> (human confirmation if consequential)
  -> invoke/fill
  -> snapshot to verify outcome
  -> stop_all when elevated access is no longer needed
```

### Minimal Python Client

```python
import json
import pathlib
import urllib.request

token = pathlib.Path.home().joinpath(
    ".local/share/gnome-desktop-bridge/token"
).read_text().strip()

payload = json.dumps({"action": "get_state", "args": {}}).encode()
request = urllib.request.Request(
    "http://127.0.0.1:18766/api/command",
    data=payload,
    method="POST",
    headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    },
)
with urllib.request.urlopen(request) as response:
    print(json.load(response)["result"])
```

## Security Model

See [SECURITY.md](SECURITY.md) for details.

Main properties:

- default policy `Off`;
- random 256-bit token;
- token/config mode `0600`, directories `0700`;
- constant-time token comparison;
- loopback-only bind and Host validation;
- no CORS;
- request size limit;
- central allow/deny policy before backend call;
- human-only elevation path;
- separate global-input gate;
- GNOME portal consent;
- fail-closed revocation watcher;
- password-role redaction;
- text redaction in audit;
- bounded event buffer;
- systemd hardening;
- no shell command endpoint;
- no API token disclosure.

### Important Trust Boundary

Any process already running under the same Linux user and capable of reading the token file
usually has broad access to user data and can impersonate an API client.
The token protects against accidental access, websites, and other users, but does not create a sandbox between
two untrusted processes with the same UID.

For an untrusted AI-runtime, an additional OS sandbox and broker with separate
per-request approval are needed, in addition to this bearer token.

### Redaction is Not Absolute

AT-SPI password role is masked. However, the application may mistakenly represent a secret as
a regular text widget; screenshots can also contain secrets. Therefore, Observe is already
a sensitive permission.

## Troubleshooting

### Daemon Does Not Connect

```bash
systemctl --user status gnome-desktop-bridge
journalctl --user -u gnome-desktop-bridge -n 200 --no-pager
curl http://127.0.0.1:18766/health
```

Check that the command is running inside an active GNOME user session where
`DBUS_SESSION_BUS_ADDRESS`, `XDG_RUNTIME_DIR`, accessibility bus, and portals are available.

### `401 unauthorized`

- Re-read the token file;
- Do not add a newline to the header value;
- After rotation, restart the daemon;
- Ensure that both the client and the daemon run as the same Linux user.

### `AT-SPI backend unavailable`

Check the packages and accessibility bus:

```bash
busctl --user status org.a11y.Bus
python3 -c 'import gi; gi.require_version("Atspi","2.0"); from gi.repository import Atspi; print(Atspi.get_desktop_count())'
```

Do not start the daemon through `sudo`: root will end up in a different D-Bus/session environment.

### Snapshot is empty or incomplete

- Some apps poorly implement accessibility;
- Increase `depth`/`maxNodes`;
- An Electron/Chromium app may require enabled accessibility support;
- Canvas/game/remote-video UI often lacks a useful semantic tree;
- Use screenshot only after explicit permission.

### `stale_reference`

Another `list_apps` or `snapshot` has already started a new generation. Repeat the discovery and
do not use the old ref.

### Portal dialog cancelled or permission denied

This is an expected user decision. Do not loop the request. Ensure that:

- The `ALL DESKTOP` mode is saved;
- `Global pointer and keyboard` is enabled;
- The daemon runs in a GNOME session;
- `xdg-desktop-portal-gnome` is started.

```bash
systemctl --user status xdg-desktop-portal xdg-desktop-portal-gnome
```

### Click hits the wrong target

- Get a new snapshot immediately before the click;
- Check the bounds;
- Ensure that the correct monitor is selected in the portal dialog;
- Fractional scaling and an app with incorrect AT-SPI bounds can cause discrepancies;
- First use `invoke`, then coordinates only as a fallback.

### Control mode does not allow the application

Check the identity from `list_apps`. Add the exact `appId` or a safe glob to
the Control Center. Do not use `*` unless you actually want near-global access.

### Port is in use

Change `port` in settings, then:

```bash
systemctl --user restart gnome-desktop-bridge
```

### Emergency stop without GUI

```bash
gnome-desktop-bridge-cli stop-all
```

If the daemon hangs, disable the service; the portal session will close when the client disappears:

```bash
systemctl --user stop gnome-desktop-bridge
```

## Development

Running from source tree without installation:

```bash
PYTHONPATH=. python3 -m gnome_desktop_bridge.server --verbose
PYTHONPATH=. python3 -m gnome_desktop_bridge.cli status
PYTHONPATH=. python3 -m gnome_desktop_bridge.control_center
```

Tests:

```bash
python3 -m unittest discover -v
python3 -m compileall -q gnome_desktop_bridge tests
desktop-file-validate data/io.github.local.GnomeDesktopBridge.desktop
systemd-analyze --user verify systemd/gnome-desktop-bridge.service
```

Tests do not click UI and do not open portal dialog. Live smoke test is performed separately
in an active GNOME session with a person at the screen.

### Project structure

```text
gnome_desktop_bridge/
  app_backend.py       # Gio launchers and isolated launching
  atspi_backend.py     # semantic tree and safe actions
  cli.py               # local client and human settings commands
  commands.py          # dispatcher, policy enforcement, audit redaction
  config.py            # settings, XDG paths, persistent token
  control_center.py    # GTK/libadwaita UI
  errors.py            # stable API errors
  events.py            # bounded audit + wait support
  policy.py            # access-mode matrix and app allowlist
  portal_backend.py    # Screenshot/RemoteDesktop/ScreenCast D-Bus APIs
  server.py            # authenticated HTTP + SSE daemon
data/                  # desktop entry
scripts/               # launch/install/uninstall helpers
systemd/               # hardened user service
tests/                 # unit tests
```

## Known Limitations and Roadmap

The MVP uses compatible `NotifyPointer*`/`NotifyKeyboard*` methods for RemoteDesktop portal. The next backend should use `ConnectToEIS` + libei; this is the official modern transport and better suits complex input sequences.

Other possible improvements:

- PipeWire frame capture for consented real-time visual debugging;
- optional GNOME Shell indicator with constantly visible mode/session state;
- Unix domain socket and peer-credential authentication;
- per-client tokens/scopes and rotation without restart;
- persistent encrypted audit with retention policy;
- action confirmation broker for consequential operations;
- better multi-monitor/fractional-scale calibration;
- semantic search endpoint without passing the full tree;
- rate limits and per-command deadlines;
- package/RPM/Flatpak-friendly distribution;
- libei Python binding or small Rust helper;
- clipboard portal support as a separate feature gate;
- automated accessibility event subscriptions instead of polling snapshots.

## License

MIT — see [LICENSE](LICENSE).
