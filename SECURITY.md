# Security policy

GNOME Desktop Bridge can read application interfaces and, with ALL DESKTOP enabled,
generate input events. Treat the bearer token as a secret with the same weight as the
active user session.

## Supported version

While the project is an MVP, security fixes are applied only to the current branch.

## Trust boundary

The bridge protects against:

- network clients outside loopback;
- accidental localhost requests without the token;
- web origins, because there is no CORS and the Authorization header is mandatory;
- raising the permission mode through the HTTP API;
- global input without GNOME portal consent;
- portal input continuing after the settings revoke it.

The bridge does not protect against a malicious process running as the same Linux UID
that can already read the token file, inspect the user session, or change the settings.
Such a process needs a separate OS sandbox or broker architecture.

The on-screen overlay is a transparency feature, not a security control: it shows what
an agent does through the bridge, but an agent with the token could also act while the
Shell extension is disabled.

## Safe operation

- Keep the mode at `Off` when the bridge is not needed.
- Prefer `Control` with exact application IDs over `ALL`.
- Do not disable protected-text redaction.
- Do not publish the token, settings, screenshots, or journal without reviewing them.
- Call `stop_all` after an ALL task.
- Do not run the daemon as root.
- Do not bind the API to a LAN interface; the code deliberately forbids it.
- Do not add a CORS wildcard.
- Keep the project directory writable only by you: the service, launchers, and Shell
  extension run from it. The installer refuses a group- or world-writable tree.

`ALL DESKTOP` resets to `Off` on every daemon start, so a restart never preserves
elevated access.

## Secret handling

The token and the portal restore token are created with mode `0600`; their parent
directories use `0700`. `fill.text`, `type_text.text`, and screenshot base64 are
excluded from the in-memory audit log, and the overlay never receives typed text: a
lone printable key is shown as `•`. Screenshots, visible text, application names, and
coordinates may still be sensitive. The bridge keeps at most 100 of its own screenshots.

Token rotation takes effect on the next request, without a restart:

```bash
gnome-desktop-bridge-cli token --rotate
```

## Emergency response

```bash
gnome-desktop-bridge-cli stop-all
systemctl --user stop gnome-desktop-bridge
```

`stop-all` writes `Off` to disk before contacting the daemon, so it works even when
the daemon is hung. The top-bar indicator and the Control Center do the same.

If you suspect compromise:

1. stop the service;
2. rotate the token;
3. delete the portal restore token;
4. review screenshots and the journal;
5. revoke screen sharing and remote desktop permissions in GNOME Settings if listed;
6. restart the service only after the cause is fixed.

## Reporting a vulnerability

Do not attach a working token, screenshots, or personal UI snapshots to a public
report. Include the version or commit, GNOME version, portal backend version,
reproducible steps, and the access boundary you expected. Report privately to the
project owner through GitHub:
<https://github.com/WoodyWizard/gnome-desktop-bridge/security/advisories/new>.
