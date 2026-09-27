// Human-readable wording for what the agent is doing.

// Clients that act for the person at the screen, never for an agent.
export const INTERNAL_CLIENTS = new Set(['control-center', 'overlay', 'cli-admin']);

export const MODE_LABELS = {
    off: 'Off',
    observe: 'Observe',
    control: 'Control',
    all: 'All desktop',
};

const KEY_LABELS = {
    Control_L: 'Ctrl', Control_R: 'Ctrl',
    Alt_L: 'Alt', Alt_R: 'Alt', ISO_Level3_Shift: 'AltGr',
    Super_L: 'Super', Super_R: 'Super', Meta_L: 'Meta', Meta_R: 'Meta',
    Shift_L: 'Shift', Shift_R: 'Shift',
    Return: 'Enter', KP_Enter: 'Enter', BackSpace: '⌫', Delete: 'Del',
    Escape: 'Esc', Tab: 'Tab', ISO_Left_Tab: 'Tab', space: 'Space', ' ': 'Space',
    Up: '↑', Down: '↓', Left: '←', Right: '→',
    Page_Up: 'PgUp', Page_Down: 'PgDn', Home: 'Home', End: 'End',
    Insert: 'Ins', Print: 'PrtSc', Menu: 'Menu',
};

export function keyLabel(key) {
    const name = String(key);
    if (KEY_LABELS[name])
        return KEY_LABELS[name];
    if (/^F\d{1,2}$/.test(name))
        return name;
    if ([...name].length === 1)
        return name.toUpperCase();
    return name.replace(/_/g, ' ');
}

function quoted(text) {
    const value = String(text ?? '').trim();
    if (!value)
        return '';
    return ` “${value.length > 32 ? `${value.slice(0, 31)}…` : value}”`;
}

// Activity line for a command.started event; null keeps the current line.
export function describeCommand(action, args = {}) {
    switch (action) {
    case 'ping':
    case 'capabilities':
    case 'get_state':
    case 'hello':
    case 'goodbye':
        return null;
    case 'list_apps':
        return 'Looking at open apps';
    case 'snapshot':
        return 'Reading the interface';
    case 'screenshot':
        return 'Taking a screenshot';
    case 'list_launchers':
        return 'Browsing installed apps';
    case 'launch_app':
        return `Launching${quoted(args.desktopId?.replace?.(/\.desktop$/, ''))}`;
    case 'invoke':
    case 'click':
        return 'Clicking';
    case 'fill':
        return 'Filling in a field';
    case 'focus':
        return 'Moving focus';
    case 'start_remote_desktop':
        return 'Asking for desktop control';
    case 'stop_remote_desktop':
        return 'Releasing desktop control';
    case 'pointer_move':
        return 'Moving the pointer';
    case 'pointer_click':
        return args.count === 2 ? 'Double-clicking' : 'Clicking';
    case 'scroll':
        return 'Scrolling';
    case 'key':
        return 'Pressing keys';
    case 'type_text': {
        const length = args.textLength;
        return Number.isInteger(length)
            ? `Typing ${length} character${length === 1 ? '' : 's'}` : 'Typing';
    }
    case 'stop_all':
        return 'Stopping';
    default:
        return action.replace(/_/g, ' ');
    }
}

// Richer wording once the target element is known.
export function describeTarget(action, name, role) {
    const what = quoted(name) || (role ? ` ${role}` : '');
    switch (action) {
    case 'invoke':
    case 'click':
        return `Clicking${what}`;
    case 'fill':
        return `Filling${what || ' a field'}`;
    case 'focus':
        return `Focusing${what}`;
    case 'scan':
        return `Reading${what}`;
    default:
        return `${action}${what}`;
    }
}

export function describeDisconnect(reason) {
    switch (reason) {
    case 'goodbye':
        return 'Finished';
    case 'idle':
        return 'Went idle';
    case 'stop_all':
        return 'Access stopped';
    case 'replaced':
        return 'Another agent took over';
    default:
        return 'Disconnected';
    }
}

export function friendlyError(error) {
    const code = error?.code ?? '';
    const known = {
        access_denied: 'Not allowed in this mode',
        portal_cancelled: 'Permission dialog cancelled',
        portal_denied: 'Permission denied',
        stale_reference: 'The window changed',
        coordinates_unavailable: 'Cannot locate the window',
        backend_unavailable: 'Desktop service unavailable',
        operation_cancelled: 'Stopped',
        internal_error: 'Internal bridge error',
        device_not_granted: 'Input device not granted',
    };
    if (known[code])
        return known[code];
    const message = String(error?.message ?? 'Something went wrong');
    return message.length > 60 ? `${message.slice(0, 59)}…` : message;
}
