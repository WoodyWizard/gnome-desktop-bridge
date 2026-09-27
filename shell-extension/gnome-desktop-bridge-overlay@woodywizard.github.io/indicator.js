// Top-bar indicator: always-visible bridge state and an emergency stop.
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import GObject from 'gi://GObject';
import Gio from 'gi://Gio';
import Shell from 'gi://Shell';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import * as PanelMenu from 'resource:///org/gnome/shell/ui/panelMenu.js';
import * as PopupMenu from 'resource:///org/gnome/shell/ui/popupMenu.js';

import {MODE_LABELS} from './format.js';

const CONTROL_CENTER_ID = 'io.github.local.GnomeDesktopBridge.desktop';
const DOT_CLASSES = ['gdb-mode-observe', 'gdb-mode-control', 'gdb-mode-all', 'gdb-agent', 'gdb-input'];

function findProgram(name) {
    const candidates = [
        GLib.build_filenamev([GLib.get_home_dir(), '.local', 'bin', name]),
        GLib.find_program_in_path(name),
    ];
    return candidates.find(path => path && GLib.file_test(path, GLib.FileTest.IS_EXECUTABLE));
}

function spawn(name, args = []) {
    const program = findProgram(name);
    if (!program) {
        Main.notifyError('Desktop Bridge', `${name} was not found. Run scripts/install-user.sh.`);
        return null;
    }
    try {
        return Gio.Subprocess.new([program, ...args], Gio.SubprocessFlags.STDOUT_SILENCE);
    } catch (e) {
        Main.notifyError('Desktop Bridge', e.message);
        return null;
    }
}

export const Indicator = GObject.registerClass(
class Indicator extends PanelMenu.Button {
    _init(extension, connection) {
        super._init(0.5, 'Desktop Bridge');
        this._connection = connection;

        const box = new St.BoxLayout({style_class: 'panel-status-indicators-box'});
        box.add_child(new St.Icon({
            gicon: Gio.icon_new_for_string(`${extension.path}/icons/desktop-bridge-symbolic.svg`),
            style_class: 'system-status-icon',
        }));
        this._dot = new St.Widget({
            style_class: 'gdb-indicator-dot',
            y_align: Clutter.ActorAlign.CENTER,
        });
        box.add_child(this._dot);
        this.add_child(box);

        this._title = new PopupMenu.PopupMenuItem('Desktop Bridge', {reactive: false});
        this._title.label.add_style_class_name('gdb-menu-title');
        this.menu.addMenuItem(this._title);
        this._status = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._status.label.add_style_class_name('gdb-menu-subtitle');
        this.menu.addMenuItem(this._status);
        this._agentItem = new PopupMenu.PopupMenuItem('', {reactive: false});
        this._agentItem.label.add_style_class_name('gdb-menu-subtitle');
        this.menu.addMenuItem(this._agentItem);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._animations = new PopupMenu.PopupSwitchMenuItem('On-screen animations', true);
        this._animations.connect('toggled', (_item, state) =>
            spawn('gnome-desktop-bridge-cli', ['feature', 'overlay', state ? 'on' : 'off']));
        this.menu.addMenuItem(this._animations);
        this.menu.addAction('Open Control Center', () => this._openControlCenter(),
            'preferences-system-symbolic');

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());
        this._stop = this.menu.addAction('Stop all agent access', () => this._stopAll(),
            'process-stop-symbolic');
        this._stop.add_style_class_name('gdb-menu-stop');
        // Inline so the theme's menu-item label color cannot override it.
        this._stop.label.set_style('color: #f87171; font-weight: 700;');

        this.menu.connect('open-state-changed', (_menu, open) => {
            if (open)
                connection.refresh();
        });

        connection.connectObject('state', (_source, state) => this._sync(state), this);
        this._sync(connection.state);
    }

    _sync(state) {
        for (const name of DOT_CLASSES)
            this._dot.remove_style_class_name(name);
        this._dot.remove_all_transitions();
        this._dot.opacity = 255;

        if (!state) {
            this._status.label.text = 'The bridge daemon is not running';
            this._agentItem.visible = false;
            this._animations.visible = false;
            return;
        }
        const mode = state.accessMode ?? 'off';
        const input = Boolean(state.remoteDesktop?.active);
        const agent = state.agent;
        if (mode !== 'off')
            this._dot.add_style_class_name(`gdb-mode-${mode}`);
        if (agent)
            this._dot.add_style_class_name(input ? 'gdb-input' : 'gdb-agent');
        if (agent) {
            this._dot.ease({
                opacity: 90,
                duration: 900,
                mode: Clutter.AnimationMode.EASE_IN_OUT_SINE,
                repeatCount: -1,
                autoReverse: true,
            });
        }

        const parts = [`Mode: ${MODE_LABELS[mode] ?? mode}`];
        if (input)
            parts.push('input control active');
        if (state.settingsError)
            parts.push('settings error, fail-closed');
        this._status.label.text = parts.join(' · ');
        this._agentItem.visible = Boolean(agent);
        if (agent) {
            const count = agent.commands ?? 0;
            this._agentItem.label.text =
                `${agent.name} connected · ${count} command${count === 1 ? '' : 's'}`;
        }
        this._animations.visible = true;
        this._animations.setToggleState(state.overlay?.enabled ?? true);
    }

    _openControlCenter() {
        const app = Shell.AppSystem.get_default().lookup_app(CONTROL_CENTER_ID);
        if (app)
            app.activate();
        else
            spawn('gnome-desktop-bridge-control');
    }

    _stopAll() {
        // The CLI fallback revokes access on disk even if the daemon hangs.
        this._connection.command('stop_all')
            .catch(() => spawn('gnome-desktop-bridge-cli', ['stop-all']));
    }

    destroy() {
        this._connection.disconnectObject(this);
        super.destroy();
    }
});
