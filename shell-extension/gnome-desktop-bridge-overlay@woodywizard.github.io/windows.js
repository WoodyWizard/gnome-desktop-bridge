// Window geometry service for the bridge daemon.
//
// Native Wayland clients do not know where their windows are, so AT-SPI
// reports their element coordinates relative to the window. The daemon asks
// this object (exported on GNOME Shell's own bus name) where a window really
// is, then adds that origin to the AT-SPI coordinates.
import Gio from 'gi://Gio';
import Meta from 'gi://Meta';

export const OBJECT_PATH = '/io/github/woodywizard/GnomeDesktopBridge';

const INTERFACE = `
<node>
  <interface name="io.github.woodywizard.GnomeDesktopBridge.Overlay1">
    <method name="WindowOrigin">
      <arg type="u" name="pid" direction="in"/>
      <arg type="s" name="title" direction="in"/>
      <arg type="i" name="width" direction="in"/>
      <arg type="i" name="height" direction="in"/>
      <arg type="b" name="found" direction="out"/>
      <arg type="b" name="needsOffset" direction="out"/>
      <arg type="i" name="x" direction="out"/>
      <arg type="i" name="y" direction="out"/>
    </method>
    <method name="Ping">
      <arg type="s" name="version" direction="out"/>
    </method>
  </interface>
</node>`;

const SIZE_TOLERANCE = 2;
const MIN_SCORE = 3;

function sizeMatches(rect, width, height) {
    return Math.abs(rect.width - width) <= SIZE_TOLERANCE &&
        Math.abs(rect.height - height) <= SIZE_TOLERANCE;
}

export class WindowLocator {
    constructor(version) {
        this._version = String(version);
        this._object = Gio.DBusExportedObject.wrapJSObject(INTERFACE, this);
        this._object.export(Gio.DBus.session, OBJECT_PATH);
    }

    destroy() {
        this._object.unexport();
    }

    Ping() {
        return this._version;
    }

    WindowOrigin(pid, title, width, height) {
        // Flatpak apps reach AT-SPI through a proxy, so the pid may not match;
        // title and size then identify the window instead.
        let best = null;
        let bestScore = 0;
        for (const actor of global.get_window_actors()) {
            const window = actor.meta_window;
            if (!window || window.is_override_redirect())
                continue;
            let score = 0;
            if (pid && window.get_pid() === pid)
                score += 4;
            if (title && window.get_title() === title)
                score += 3;
            if (sizeMatches(window.get_frame_rect(), width, height) ||
                sizeMatches(window.get_buffer_rect(), width, height))
                score += 2;
            if (window.has_focus())
                score += 1;
            if (score > bestScore) {
                best = window;
                bestScore = score;
            }
        }
        if (!best || bestScore < MIN_SCORE)
            return [false, false, 0, 0];

        // X11 clients know their global position already.
        if (best.get_client_type() === Meta.WindowClientType.X11)
            return [true, false, 0, 0];

        // GTK 4 reports coordinates relative to the visible window (frame
        // rect); toolkits that include client-side shadows in their toplevel
        // (GTK 3) match the buffer rect instead.
        const frame = best.get_frame_rect();
        const buffer = best.get_buffer_rect();
        const origin = !sizeMatches(frame, width, height) && sizeMatches(buffer, width, height)
            ? buffer : frame;
        return [true, true, origin.x, origin.y];
    }
}
