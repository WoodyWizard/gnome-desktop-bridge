// On-screen feedback for everything an agent does through the bridge.
//
// Every actor here is non-reactive, so pointer and keyboard input always pass
// through to the windows underneath. The overlay hides itself for the
// duration of an agent screenshot so it never appears in what the agent sees.
import Cairo from 'cairo';
import Clutter from 'gi://Clutter';
import GLib from 'gi://GLib';
import St from 'gi://St';

import * as Main from 'resource:///org/gnome/shell/ui/main.js';

import {
    INTERNAL_CLIENTS,
    MODE_LABELS,
    describeCommand,
    describeDisconnect,
    describeTarget,
    friendlyError,
    keyLabel,
} from './format.js';

const Mode = Clutter.AnimationMode;

// Two tones per palette; they breathe in antiphase so the glow slowly
// shifts color instead of just pulsing.
const PALETTES = {
    agent: [[139, 92, 246, 0.62], [34, 211, 238, 0.55]],
    input: [[245, 158, 11, 0.64], [244, 63, 94, 0.58]],
    alarm: [[239, 68, 68, 0.75], [239, 68, 68, 0.5]],
};
const GLOW_SIZE = 30;
const GLOW_IDLE_OPACITY = 165;
const BREATH_MS = 2600;
const CURSOR_TIP = 2;
const CURSOR_MIN_GLIDE_MS = 260;
const CURSOR_MAX_GLIDE_MS = 900;
const CURSOR_HIDE_MS = 5000;
const RIPPLE_SIZE = 96;
const SCREENSHOT_RESTORE_MS = 8000;
const IDLE_TEXT_MS = 1500;

function rgba([r, g, b, a], alpha = a) {
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

class Timers {
    constructor() {
        this._named = new Map();
        this._anonymous = new Set();
    }

    // Run fn after ms; a new call with the same key replaces the old one.
    later(key, ms, fn) {
        this.cancel(key);
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._named.delete(key);
            fn();
            return GLib.SOURCE_REMOVE;
        });
        this._named.set(key, id);
    }

    after(ms, fn) {
        const id = GLib.timeout_add(GLib.PRIORITY_DEFAULT, ms, () => {
            this._anonymous.delete(id);
            fn();
            return GLib.SOURCE_REMOVE;
        });
        this._anonymous.add(id);
    }

    has(key) {
        return this._named.has(key);
    }

    cancel(key) {
        const id = this._named.get(key);
        if (id) {
            GLib.source_remove(id);
            this._named.delete(key);
        }
    }

    clear() {
        for (const id of [...this._named.values(), ...this._anonymous])
            GLib.source_remove(id);
        this._named.clear();
        this._anonymous.clear();
    }
}

function fadeOutAndDestroy(actor, duration = 350) {
    actor.ease({
        opacity: 0,
        duration,
        mode: Mode.EASE_OUT_QUAD,
        onComplete: () => actor.destroy(),
    });
}

class EdgeGlow {
    constructor(parent) {
        this.actor = new St.Widget({reactive: false, visible: false, opacity: 0});
        parent.add_child(this.actor);
        this._strips = [];
        this._palette = 'agent';
        this.shown = false;
        this.alarming = false;
        this.rebuild();
    }

    rebuild() {
        this.actor.destroy_all_children();
        this._strips = [];
        for (const monitor of Main.layoutManager.monitors) {
            for (const side of ['top', 'bottom', 'left', 'right']) {
                for (const tone of [0, 1]) {
                    const strip = new St.Widget({reactive: false});
                    const horizontal = side === 'top' || side === 'bottom';
                    strip.set_size(
                        horizontal ? monitor.width : GLOW_SIZE,
                        horizontal ? GLOW_SIZE : monitor.height);
                    strip.set_position(
                        side === 'right' ? monitor.x + monitor.width - GLOW_SIZE : monitor.x,
                        side === 'bottom' ? monitor.y + monitor.height - GLOW_SIZE : monitor.y);
                    strip.set_pivot_point(
                        horizontal ? 0.5 : 0,
                        side === 'bottom' ? 1 : 0);
                    strip._side = side;
                    strip._tone = tone;
                    this.actor.add_child(strip);
                    this._strips.push(strip);
                }
            }
        }
        this.setPalette(this._palette);
        if (this.shown)
            this._breathe();
    }

    setPalette(name) {
        this._palette = name;
        const tones = PALETTES[name];
        for (const strip of this._strips) {
            const color = tones[strip._tone];
            const solid = rgba(color);
            const clear = rgba(color, 0);
            const [direction, start, end] = {
                top: ['vertical', solid, clear],
                bottom: ['vertical', clear, solid],
                left: ['horizontal', solid, clear],
                right: ['horizontal', clear, solid],
            }[strip._side];
            strip.set_style(
                `background-gradient-direction: ${direction};` +
                `background-gradient-start: ${start};` +
                `background-gradient-end: ${end};`);
        }
    }

    _breathe() {
        for (const strip of this._strips) {
            strip.remove_transition('opacity');
            strip.opacity = strip._tone ? 0 : 255;
            strip.ease({
                opacity: strip._tone ? 255 : 110,
                duration: BREATH_MS,
                mode: Mode.EASE_IN_OUT_SINE,
                repeatCount: -1,
                autoReverse: true,
            });
        }
    }

    // Trace the frame from the top center, down both sides, to the bottom.
    show(animate) {
        const wasShown = this.shown;
        this.shown = true;
        this.actor.remove_all_transitions();
        this.actor.visible = true;
        this.actor.ease({opacity: GLOW_IDLE_OPACITY, duration: 450, mode: Mode.EASE_OUT_QUAD});
        if (!wasShown)
            this._breathe();
        if (!animate)
            return;
        for (const strip of this._strips) {
            const horizontal = strip._side === 'top' || strip._side === 'bottom';
            const delay = {top: 0, left: 200, right: 200, bottom: 420}[strip._side];
            if (horizontal)
                strip.scale_x = 0;
            else
                strip.scale_y = 0;
            strip.ease({
                [horizontal ? 'scale_x' : 'scale_y']: 1,
                delay,
                duration: 560,
                mode: Mode.EASE_OUT_CUBIC,
            });
        }
    }

    hide(duration = 650) {
        if (!this.shown)
            return;
        this.shown = false;
        this.actor.ease({
            opacity: 0,
            duration,
            mode: Mode.EASE_IN_QUAD,
            onComplete: () => {
                if (this.shown)
                    return;
                this.actor.visible = false;
                for (const strip of this._strips)
                    strip.remove_all_transitions();
            },
        });
    }

    setBusy(busy) {
        if (!this.shown)
            return;
        this.actor.ease({
            opacity: busy ? 255 : GLOW_IDLE_OPACITY,
            duration: busy ? 160 : 700,
            mode: Mode.EASE_OUT_QUAD,
        });
    }

    alarm() {
        this.alarming = true;
        this.setPalette('alarm');
        this.actor.visible = true;
        this.actor.remove_all_transitions();
        this.actor.opacity = 255;
        this.shown = false;
        this.actor.ease({
            opacity: 0,
            delay: 350,
            duration: 900,
            mode: Mode.EASE_IN_QUAD,
            onComplete: () => {
                this.alarming = false;
                this.actor.visible = false;
                this.setPalette('agent');
            },
        });
    }
}

class AgentCursor {
    constructor(parent) {
        this._parent = parent;
        this.actor = new St.Widget({reactive: false, visible: false, opacity: 0});
        this._tag = new St.Label({style_class: 'gdb-cursor-tag', text: 'Agent', x: 17, y: 25});
        this._arrow = new St.DrawingArea({reactive: false, width: 24, height: 30});
        this._arrow.set_pivot_point(0.1, 0.07);
        this._arrow.connect('repaint', area => this._paint(area));
        this.actor.add_child(this._tag);
        this.actor.add_child(this._arrow);
        parent.add_child(this.actor);
        this._target = null;
    }

    setName(name) {
        this._tag.text = name || 'Agent';
    }

    // Where the agent's pointer is, as far as the overlay knows.
    position() {
        if (this._target)
            return this._target;
        const [x, y] = global.get_pointer();
        return {x, y};
    }

    _paint(area) {
        const cr = area.get_context();
        const arrow = () => {
            cr.moveTo(2, 2);
            cr.lineTo(2, 22.5);
            cr.lineTo(7.4, 17.4);
            cr.lineTo(11.1, 25.8);
            cr.lineTo(14.6, 24.3);
            cr.lineTo(10.9, 16.1);
            cr.lineTo(18, 16.1);
            cr.closePath();
        };
        cr.save();
        cr.translate(1.2, 1.8);
        arrow();
        cr.setSourceRGBA(0, 0, 0, 0.32);
        cr.fill();
        cr.restore();

        arrow();
        const gradient = new Cairo.LinearGradient(2, 2, 14, 25);
        gradient.addColorStopRGBA(0, 0.55, 0.36, 0.96, 1);
        gradient.addColorStopRGBA(1, 0.13, 0.83, 0.93, 1);
        cr.setSource(gradient);
        cr.fillPreserve();
        cr.setSourceRGBA(1, 1, 1, 0.96);
        cr.setLineWidth(1.5);
        cr.setLineJoin(Cairo.LineJoin.ROUND);
        cr.stroke();
        cr.$dispose();
    }

    _reveal() {
        if (this.actor.visible && this.actor.opacity > 0)
            return;
        const {x, y} = this.position();
        this.actor.remove_all_transitions();
        this.actor.set_position(x - CURSOR_TIP, y - CURSOR_TIP);
        this.actor.visible = true;
        this.actor.ease({opacity: 255, duration: 180, mode: Mode.EASE_OUT_QUAD});
    }

    moveTo(x, y, durationMs, timers) {
        this._reveal();
        this._target = {x, y};
        const duration = Math.min(
            Math.max(durationMs ?? 0, CURSOR_MIN_GLIDE_MS), CURSOR_MAX_GLIDE_MS);
        this.actor.ease({
            x: x - CURSOR_TIP,
            y: y - CURSOR_TIP,
            duration,
            mode: Mode.EASE_IN_OUT_CUBIC,
        });
        const transition = this.actor.get_transition('x') ?? this.actor.get_transition('y');
        if (transition) {
            let frame = 0;
            transition.connect('new-frame', () => {
                if (frame++ % 2 === 0)
                    this._dropTrail();
            });
        }
        this.keepAlive(timers);
    }

    keepAlive(timers) {
        timers.later('cursor-hide', CURSOR_HIDE_MS, () => this.hide());
    }

    _dropTrail() {
        const dot = new St.Widget({style_class: 'gdb-trail', reactive: false});
        dot.set_size(10, 10);
        dot.set_position(this.actor.x + CURSOR_TIP - 5, this.actor.y + CURSOR_TIP - 5);
        dot.set_pivot_point(0.5, 0.5);
        this._parent.insert_child_below(dot, this.actor);
        dot.ease({
            opacity: 0,
            scale_x: 0.2,
            scale_y: 0.2,
            duration: 460,
            mode: Mode.EASE_OUT_QUAD,
            onComplete: () => dot.destroy(),
        });
    }

    press() {
        this._arrow.ease({
            scale_x: 0.78,
            scale_y: 0.78,
            duration: 80,
            mode: Mode.EASE_OUT_QUAD,
            onComplete: () => this._arrow.ease({
                scale_x: 1,
                scale_y: 1,
                duration: 220,
                mode: Mode.EASE_OUT_BACK,
            }),
        });
    }

    hide() {
        this._target = null;
        if (!this.actor.visible)
            return;
        this.actor.ease({
            opacity: 0,
            duration: 400,
            mode: Mode.EASE_OUT_QUAD,
            onComplete: () => {
                this.actor.visible = false;
            },
        });
    }
}

class StatusPill {
    constructor(parent) {
        this.actor = new St.BoxLayout({
            style_class: 'gdb-pill',
            orientation: Clutter.Orientation.VERTICAL,
            reactive: false,
            visible: false,
            opacity: 0,
        });
        const row = new St.BoxLayout({style_class: 'gdb-pill-row'});
        this._dot = new St.Widget({style_class: 'gdb-pill-dot', y_align: Clutter.ActorAlign.CENTER});
        this._name = new St.Label({style_class: 'gdb-pill-name', y_align: Clutter.ActorAlign.CENTER});
        this._activity = new St.Label({
            style_class: 'gdb-pill-activity',
            y_align: Clutter.ActorAlign.CENTER,
        });
        this._chip = new St.Label({style_class: 'gdb-pill-chip', y_align: Clutter.ActorAlign.CENTER});
        for (const child of [this._dot, this._name, this._activity, this._chip])
            row.add_child(child);
        this._track = new St.Widget({style_class: 'gdb-pill-track', visible: false});
        this._bar = new St.Widget({style_class: 'gdb-pill-bar'});
        this._bar.add_constraint(new Clutter.BindConstraint({
            source: this._track,
            coordinate: Clutter.BindCoordinate.SIZE,
        }));
        this._bar.set_pivot_point(0, 0.5);
        this._track.add_child(this._bar);
        this.actor.add_child(row);
        this.actor.add_child(this._track);
        this.actor.set_pivot_point(0.5, 0);
        parent.add_child(this.actor);
        this.actor.connect('notify::width', () => this.place());
        this._chipClass = null;
    }

    place() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor)
            return;
        const top = Main.layoutManager.panelBox.visible ? Main.layoutManager.panelBox.height : 0;
        this.actor.set_position(
            Math.round(monitor.x + (monitor.width - this.actor.width) / 2),
            monitor.y + top + 12);
    }

    get shown() {
        return this.actor.visible && this.actor.opacity > 0;
    }

    present(name, activity, animate) {
        this._name.text = name;
        this._activity.text = activity;
        this.actor.remove_all_transitions();
        this.actor.visible = true;
        this.place();
        this._dot.remove_all_transitions();
        this._dot.opacity = 255;
        this._dot.ease({
            opacity: 70,
            duration: 900,
            mode: Mode.EASE_IN_OUT_SINE,
            repeatCount: -1,
            autoReverse: true,
        });
        if (animate) {
            this.actor.opacity = 0;
            this.actor.translation_y = -28;
            this.actor.scale_x = this.actor.scale_y = 0.9;
            this.actor.ease({
                opacity: 255,
                translation_y: 0,
                scale_x: 1,
                scale_y: 1,
                duration: 520,
                mode: Mode.EASE_OUT_BACK,
            });
        } else {
            this.actor.translation_y = 0;
            this.actor.scale_x = this.actor.scale_y = 1;
            this.actor.ease({opacity: 255, duration: 250, mode: Mode.EASE_OUT_QUAD});
        }
    }

    setName(name) {
        this._name.text = name;
    }

    setActivity(text) {
        if (this._activity.text === text)
            return;
        this._activity.text = text;
        this._activity.remove_all_transitions();
        this._activity.opacity = 90;
        this._activity.ease({opacity: 255, duration: 220, mode: Mode.EASE_OUT_QUAD});
    }

    setMode(mode, inputActive) {
        const kind = inputActive ? 'input' : mode;
        this._chip.text = inputActive ? 'Input control' : MODE_LABELS[mode] ?? mode;
        if (this._chipClass)
            this._chip.remove_style_class_name(this._chipClass);
        this._chipClass = `gdb-chip-${kind}`;
        this._chip.add_style_class_name(this._chipClass);
    }

    showError(text, timers) {
        this.setActivity(text);
        this.actor.add_style_class_name('gdb-pill-error');
        const offsets = [9, -8, 6, -4, 0];
        const shake = index => {
            if (index >= offsets.length)
                return;
            this.actor.ease({
                translation_x: offsets[index],
                duration: 55,
                mode: Mode.EASE_IN_OUT_QUAD,
                onComplete: () => shake(index + 1),
            });
        };
        shake(0);
        timers.later('pill-error', 2400, () => {
            this.actor.remove_style_class_name('gdb-pill-error');
        });
    }

    progress(durationMs) {
        this._track.visible = true;
        this._bar.remove_all_transitions();
        this._bar.scale_x = 0;
        this._bar.ease({
            scale_x: 1,
            duration: Math.max(200, durationMs),
            mode: Mode.LINEAR,
        });
    }

    endProgress() {
        this._bar.remove_all_transitions();
        this._track.visible = false;
    }

    dismiss() {
        this._dot.remove_all_transitions();
        this.actor.ease({
            opacity: 0,
            translation_y: -18,
            duration: 380,
            mode: Mode.EASE_IN_QUAD,
            onComplete: () => {
                this.actor.visible = false;
                this.endProgress();
            },
        });
    }
}

class KeyStrip {
    constructor(parent) {
        this.actor = new St.BoxLayout({
            style_class: 'gdb-keys',
            reactive: false,
            visible: false,
            opacity: 0,
        });
        this.actor.set_pivot_point(0.5, 0.5);
        parent.add_child(this.actor);
        this.actor.connect('notify::width', () => this.place());
        this._masked = null;
    }

    place() {
        const monitor = Main.layoutManager.primaryMonitor;
        if (!monitor)
            return;
        this.actor.set_position(
            Math.round(monitor.x + (monitor.width - this.actor.width) / 2),
            monitor.y + monitor.height - this.actor.height - 96);
    }

    show(keys, timers) {
        const masked = keys.length === 1 && keys[0] === '•';
        if (masked && this._masked && this.actor.visible) {
            // Hidden characters accumulate in one cap, like a password field.
            this._masked.text = `${this._masked.text}•`.slice(-16);
        } else {
            this.actor.destroy_all_children();
            this._masked = null;
            const labels = keys.length ? keys.map(keyLabel) : ['⌨'];
            labels.forEach((label, index) => {
                if (index > 0)
                    this.actor.add_child(new St.Label({style_class: 'gdb-keyplus', text: '+'}));
                const cap = new St.Label({style_class: 'gdb-keycap', text: label});
                this.actor.add_child(cap);
                if (masked)
                    this._masked = cap;
            });
        }
        this.actor.remove_all_transitions();
        if (!this.actor.visible || this.actor.opacity === 0) {
            this.actor.visible = true;
            this.actor.opacity = 0;
            this.actor.translation_y = 18;
            this.actor.scale_x = this.actor.scale_y = 1;
            this.actor.ease({
                opacity: 255,
                translation_y: 0,
                duration: 260,
                mode: Mode.EASE_OUT_BACK,
            });
        } else {
            this.actor.opacity = 255;
            this.actor.translation_y = 0;
            this.actor.scale_x = this.actor.scale_y = 1.07;
            this.actor.ease({scale_x: 1, scale_y: 1, duration: 200, mode: Mode.EASE_OUT_QUAD});
        }
        timers.later('keys-hide', 1500, () => this.hide());
    }

    hide() {
        this.actor.ease({
            opacity: 0,
            translation_y: 10,
            duration: 320,
            mode: Mode.EASE_IN_QUAD,
            onComplete: () => {
                this.actor.visible = false;
                this._masked = null;
            },
        });
    }
}

export class Overlay {
    constructor(connection) {
        this._connection = connection;
        this._timers = new Timers();
        this._agent = null;
        this._enabled = true;
        this._edgeGlow = true;
        this._mode = 'off';
        this._inputActive = false;
        this._busy = 0;

        this.actor = new St.Widget({name: 'gdbOverlay', reactive: false});
        this.actor.add_constraint(new Clutter.BindConstraint({
            source: global.stage,
            coordinate: Clutter.BindCoordinate.ALL,
        }));
        this._glow = new EdgeGlow(this.actor);
        this._effects = new St.Widget({reactive: false});
        this.actor.add_child(this._effects);
        this._cursor = new AgentCursor(this._effects);
        this._keys = new KeyStrip(this.actor);
        this._pill = new StatusPill(this.actor);
        this._highlight = null;
        Main.layoutManager.addTopChrome(this.actor);

        connection.connectObject(
            'state', (_source, state) => this._onState(state),
            'event', (_source, event) => this._onEvent(event),
            this);
        Main.layoutManager.connectObject('monitors-changed', () => {
            this._glow.rebuild();
            this._pill.place();
            this._keys.place();
        }, this);
    }

    destroy() {
        this._connection.disconnectObject(this);
        Main.layoutManager.disconnectObject(this);
        this._timers.clear();
        this.actor.destroy();
    }

    _onState(state) {
        if (!state) {
            if (this._agent)
                this._agentLeft('Bridge stopped', false);
            return;
        }
        const overlay = state.overlay ?? {};
        this._enabled = overlay.enabled ?? true;
        this._edgeGlow = overlay.edgeGlow ?? true;
        this._mode = state.accessMode ?? 'off';
        this._inputActive = Boolean(state.remoteDesktop?.active);
        this.actor.visible = this._enabled;
        this._pill.setMode(this._mode, this._inputActive);
        if (!this._glow.alarming &&
            (!this._glow.shown || this._inputActive !== (this._glow._palette === 'input')))
            this._glow.setPalette(this._inputActive ? 'input' : 'agent');
        if (!this._edgeGlow)
            this._glow.hide(250);

        if (state.agent && !this._agent)
            this._agentArrived(state.agent, false);
        else if (state.agent && this._agent && state.agent.name !== this._agent.name)
            this._rename(state.agent.name);
        else if (!state.agent && this._agent)
            this._agentLeft('Disconnected', false);
        else if (this._agent && this._edgeGlow && !this._glow.shown)
            this._glow.show(false);
    }

    _onEvent(event) {
        if (!this._enabled)
            return;
        const data = event.data ?? {};
        const internal = INTERNAL_CLIENTS.has(data.client);
        switch (event.type) {
        case 'agent.connected':
            this._agentArrived({name: data.name, client: data.client}, true);
            break;
        case 'agent.renamed':
            this._rename(data.name);
            break;
        case 'agent.disconnected':
            this._agentLeft(describeDisconnect(data.reason), data.reason === 'stop_all');
            break;
        case 'command.started':
            if (!internal && this._agent)
                this._commandStarted(data);
            break;
        case 'command.completed':
            if (!internal && this._agent)
                this._commandFinished(data, null);
            break;
        case 'command.failed':
            if (!internal && this._agent)
                this._commandFinished(data, data.error);
            break;
        case 'visual.pointer':
            if (Number.isFinite(data.x) && Number.isFinite(data.y))
                this._cursor.moveTo(data.x, data.y, data.durationMs, this._timers);
            break;
        case 'visual.click':
            this._click(data);
            break;
        case 'visual.scroll':
            this._scroll(data);
            break;
        case 'visual.key':
            this._keys.show(Array.isArray(data.keys) ? data.keys.map(String) : [], this._timers);
            break;
        case 'visual.type':
            if (this._agent && Number.isFinite(data.estimatedMs))
                this._pill.progress(data.estimatedMs);
            break;
        case 'visual.target':
            this._target(data);
            break;
        case 'visual.screenshot':
            this._screenshot(data.phase);
            break;
        case 'security.stop_all':
            this._glow.alarm();
            break;
        default:
            break;
        }
    }

    // ------------------------------------------------------------ presence

    _agentArrived(agent, animate) {
        this._agent = {name: agent.name || 'AI agent'};
        this._busy = 0;
        this._cursor.setName(this._agent.name);
        this._pill.setMode(this._mode, this._inputActive);
        this._pill.present(this._agent.name, animate ? 'Connected' : 'Waiting', animate);
        if (this._edgeGlow)
            this._glow.show(animate);
        this._timers.cancel('pill-dismiss');
        if (animate)
            this._timers.later('idle-text', 2200, () => this._pill.setActivity('Waiting'));
    }

    _rename(name) {
        if (!this._agent || !name)
            return;
        this._agent.name = name;
        this._pill.setName(name);
        this._cursor.setName(name);
    }

    _agentLeft(text, alarm) {
        if (!this._agent)
            return;
        this._agent = null;
        this._busy = 0;
        this._timers.cancel('idle-text');
        this._pill.endProgress();
        this._pill.setActivity(text);
        if (alarm)
            this._pill.showError(text, this._timers);
        else
            this._glow.hide();
        this._cursor.hide();
        this._timers.later('pill-dismiss', 1900, () => {
            if (!this._agent)
                this._pill.dismiss();
        });
    }

    _commandStarted(data) {
        this._busy += 1;
        this._glow.setBusy(true);
        const text = describeCommand(String(data.action ?? ''), data.args ?? {});
        if (text) {
            this._timers.cancel('idle-text');
            this._pill.setActivity(text);
        }
    }

    _commandFinished(data, error) {
        this._busy = Math.max(0, this._busy - 1);
        if (data.action === 'type_text')
            this._pill.endProgress();
        if (error && data.action !== 'stop_all')
            this._pill.showError(friendlyError(error), this._timers);
        if (this._busy)
            return;
        this._glow.setBusy(false);
        if (this._timers.has('idle-text'))
            return;
        this._timers.later('idle-text', IDLE_TEXT_MS, () => {
            if (this._agent)
                this._pill.setActivity('Waiting');
        });
    }

    // ------------------------------------------------------------- effects

    _click(data) {
        const point = Number.isFinite(data.x) && Number.isFinite(data.y)
            ? {x: data.x, y: data.y} : this._cursor.position();
        const kind = {right: 'secondary', middle: 'middle'}[data.button] ?? 'primary';
        const count = Math.min(Math.max(Number(data.count) || 1, 1), 3);
        this._cursor.moveTo(point.x, point.y, 0, this._timers);
        for (let click = 0; click < count; click++) {
            this._timers.after(click * 170, () => {
                this._cursor.press();
                this._ring(point, kind, 0);
                this._timers.after(110, () => this._ring(point, kind, 1));
                this._clickDot(point, kind);
            });
        }
    }

    _ring(point, kind, index) {
        const ring = new St.Widget({
            style_class: `gdb-ripple gdb-ripple-${kind}`,
            reactive: false,
        });
        ring.set_size(RIPPLE_SIZE, RIPPLE_SIZE);
        ring.set_position(point.x - RIPPLE_SIZE / 2, point.y - RIPPLE_SIZE / 2);
        ring.set_pivot_point(0.5, 0.5);
        ring.scale_x = ring.scale_y = 0.18;
        ring.opacity = index ? 180 : 255;
        this._effects.insert_child_below(ring, this._cursor.actor);
        ring.ease({
            scale_x: index ? 0.8 : 1,
            scale_y: index ? 0.8 : 1,
            opacity: 0,
            duration: 720,
            mode: Mode.EASE_OUT_CUBIC,
            onComplete: () => ring.destroy(),
        });
    }

    _clickDot(point, kind) {
        const dot = new St.Widget({style_class: `gdb-click-dot gdb-ripple-${kind}`, reactive: false});
        dot.set_size(44, 44);
        dot.set_position(point.x - 22, point.y - 22);
        dot.set_pivot_point(0.5, 0.5);
        dot.scale_x = dot.scale_y = 0.3;
        this._effects.insert_child_below(dot, this._cursor.actor);
        dot.ease({
            scale_x: 1.25,
            scale_y: 1.25,
            opacity: 0,
            duration: 460,
            mode: Mode.EASE_OUT_QUAD,
            onComplete: () => dot.destroy(),
        });
    }

    _scroll(data) {
        const dx = Number(data.dx) || 0;
        const dy = Number(data.dy) || 0;
        if (!dx && !dy)
            return;
        const vertical = Math.abs(dy) >= Math.abs(dx);
        const sign = Math.sign(vertical ? dy : dx);
        const icon = vertical
            ? sign > 0 ? 'pan-down-symbolic' : 'pan-up-symbolic'
            : sign > 0 ? 'pan-end-symbolic' : 'pan-start-symbolic';
        const point = this._cursor.position();
        this._cursor.keepAlive(this._timers);
        for (let index = 0; index < 2; index++) {
            this._timers.after(index * 120, () => {
                const bubble = new St.Icon({
                    style_class: 'gdb-scroll',
                    icon_name: icon,
                    reactive: false,
                });
                bubble.set_position(point.x + 24, point.y - 38);
                this._effects.add_child(bubble);
                bubble.ease({
                    translation_x: vertical ? 0 : sign * 22,
                    translation_y: vertical ? sign * 22 : 0,
                    opacity: 0,
                    duration: 560,
                    mode: Mode.EASE_OUT_QUAD,
                    onComplete: () => bubble.destroy(),
                });
            });
        }
    }

    _target(data) {
        const bounds = data.bounds;
        if (!bounds || ![bounds.x, bounds.y, bounds.width, bounds.height].every(Number.isFinite))
            return;
        const text = describeTarget(String(data.action ?? ''), data.name, data.role);
        if (this._agent)
            this._pill.setActivity(text);
        if (this._highlight) {
            for (const actor of this._highlight)
                fadeOutAndDestroy(actor, 150);
        }
        const scan = data.action === 'scan';
        const pad = scan ? 0 : 5;
        const box = new St.Widget({
            style_class: scan ? 'gdb-target gdb-target-scan' : 'gdb-target',
            reactive: false,
            clip_to_allocation: true,
        });
        box.set_position(bounds.x - pad, bounds.y - pad);
        box.set_size(bounds.width + 2 * pad, bounds.height + 2 * pad);
        box.set_pivot_point(0.5, 0.5);
        box.opacity = 0;
        box.scale_x = box.scale_y = scan ? 1 : 1.14;
        this._effects.insert_child_below(box, this._cursor.actor);
        box.ease({scale_x: 1, scale_y: 1, opacity: 255, duration: 240, mode: Mode.EASE_OUT_QUAD});

        const actors = [box];
        if (scan) {
            const line = new St.Widget({style_class: 'gdb-scan-line', reactive: false});
            line.set_size(box.width, 28);
            line.set_position(0, -28);
            box.add_child(line);
            line.ease({
                translation_y: box.height + 28,
                duration: Math.min(1400, 500 + box.height),
                mode: Mode.EASE_IN_OUT_SINE,
            });
        } else {
            const tag = new St.Label({style_class: 'gdb-target-tag', text: text, reactive: false});
            this._effects.insert_child_below(tag, this._cursor.actor);
            const monitor = Main.layoutManager.monitors.find(item =>
                bounds.x >= item.x && bounds.x < item.x + item.width &&
                bounds.y >= item.y && bounds.y < item.y + item.height) ??
                Main.layoutManager.primaryMonitor;
            const above = bounds.y - pad - tag.height - 6;
            tag.set_position(
                bounds.x - pad,
                above >= monitor.y + 4 ? above : bounds.y + bounds.height + pad + 6);
            tag.opacity = 0;
            tag.translation_y = 6;
            tag.ease({opacity: 255, translation_y: 0, duration: 260, mode: Mode.EASE_OUT_QUAD});
            actors.push(tag);
        }
        this._highlight = actors;
        this._timers.later('highlight', scan ? 1500 : 1300, () => {
            for (const actor of actors)
                fadeOutAndDestroy(actor, 380);
            if (this._highlight === actors)
                this._highlight = null;
        });
    }

    _screenshot(phase) {
        if (phase === 'before') {
            // Hide instantly: the agent must see the desktop, not the overlay.
            this.actor.remove_transition('opacity');
            this.actor.opacity = 0;
            this._timers.later('screenshot-restore', SCREENSHOT_RESTORE_MS, () => {
                this.actor.opacity = 255;
            });
            return;
        }
        this._timers.cancel('screenshot-restore');
        this.actor.opacity = 255;
        for (const monitor of Main.layoutManager.monitors) {
            const flash = new St.Widget({style_class: 'gdb-flash', reactive: false});
            flash.set_position(monitor.x, monitor.y);
            flash.set_size(monitor.width, monitor.height);
            flash.opacity = 0;
            this.actor.add_child(flash);
            flash.ease({
                opacity: 150,
                duration: 70,
                mode: Mode.EASE_OUT_QUAD,
                onComplete: () => fadeOutAndDestroy(flash, 320),
            });
        }
    }
}
