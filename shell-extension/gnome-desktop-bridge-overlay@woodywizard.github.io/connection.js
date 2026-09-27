// Connection to the local GNOME Desktop Bridge daemon.
//
// The overlay is a pure consumer of the daemon's authenticated event stream:
// it reads the same bearer token as every other local client, identifies
// itself as "overlay" (so it never counts as an agent), and reconnects with
// backoff whenever the daemon restarts.
import Gio from 'gi://Gio';
import GLib from 'gi://GLib';
import Soup from 'gi://Soup?version=3.0';

import * as Signals from 'resource:///org/gnome/shell/misc/signals.js';

Gio._promisify(Soup.Session.prototype, 'send_async');
Gio._promisify(Soup.Session.prototype, 'send_and_read_async');
// GNOME Shell may already have promisified this with the byte-returning
// finisher; _readStream() accepts both forms.
Gio._promisify(Gio.DataInputStream.prototype, 'read_line_async');

const APP_ID = 'gnome-desktop-bridge';
const DEFAULT_HOST = '127.0.0.1';
const DEFAULT_PORT = 18766;
const MAX_BACKOFF_S = 10;
const REFRESH_DELAY_MS = 120;

// Events after which the cached daemon state is re-read.
const STATE_EVENTS = /^(settings\.|portal\.session\.|agent\.|security\.)/;

const decoder = new TextDecoder();
const encoder = new TextEncoder();

function readText(path) {
    try {
        const [ok, bytes] = GLib.file_get_contents(path);
        return ok ? decoder.decode(bytes) : null;
    } catch {
        return null;
    }
}

export class BridgeConnection extends Signals.EventEmitter {
    // Signals:
    //   'state' (state | null)  — daemon status, or null while offline
    //   'event' (event)         — one audit event from the stream
    constructor() {
        super();
        this._session = new Soup.Session({
            timeout: 60,
            max_conns_per_host: 6,
            user_agent: 'gnome-desktop-bridge-overlay',
        });
        this._cancellable = null;
        this._retryId = 0;
        this._refreshId = 0;
        this._backoff = 1;
        this._running = false;
        this.state = null;
    }

    start() {
        this._running = true;
        this._connect().catch(e => logError(e, 'Desktop Bridge overlay'));
    }

    stop() {
        this._running = false;
        this._cancellable?.cancel();
        this._cancellable = null;
        for (const id of [this._retryId, this._refreshId]) {
            if (id)
                GLib.source_remove(id);
        }
        this._retryId = this._refreshId = 0;
        this._session.abort();
    }

    get online() {
        return this.state !== null;
    }

    _endpoint() {
        let host = DEFAULT_HOST;
        let port = DEFAULT_PORT;
        const settings = readText(
            GLib.build_filenamev([GLib.get_user_config_dir(), APP_ID, 'settings.json']));
        if (settings) {
            try {
                const parsed = JSON.parse(settings);
                if (['127.0.0.1', '::1', 'localhost'].includes(parsed.host))
                    host = parsed.host;
                if (Number.isInteger(parsed.port))
                    port = parsed.port;
            } catch {
                // An unreadable file leaves the daemon on its defaults too.
            }
        }
        const token = readText(
            GLib.build_filenamev([GLib.get_user_data_dir(), APP_ID, 'token']))?.trim();
        if (!token || !/^[0-9a-f]{64}$/i.test(token))
            return null;
        const urlHost = host.includes(':') ? `[${host}]` : host;
        return {base: `http://${urlHost}:${port}`, token};
    }

    _message(method, endpoint, path) {
        const message = Soup.Message.new(method, `${endpoint.base}${path}`);
        const headers = message.get_request_headers();
        headers.append('Authorization', `Bearer ${endpoint.token}`);
        headers.append('X-Bridge-Client', 'overlay');
        return message;
    }

    async _json(method, path, body = null) {
        const endpoint = this._endpoint();
        if (!endpoint)
            throw new Error('The bridge token is not available');
        const message = this._message(method, endpoint, path);
        if (body !== null) {
            message.set_request_body_from_bytes('application/json',
                new GLib.Bytes(encoder.encode(JSON.stringify(body))));
        }
        const bytes = await this._session.send_and_read_async(
            message, GLib.PRIORITY_DEFAULT, null);
        const payload = JSON.parse(decoder.decode(bytes.get_data() ?? new Uint8Array()));
        if (!payload.ok)
            throw new Error(payload.error?.message ?? `HTTP ${message.get_status()}`);
        return payload.result;
    }

    command(action, args = {}) {
        return this._json('POST', '/api/command', {action, args});
    }

    async _connect() {
        this._retryId = 0;
        if (!this._running)
            return;
        const endpoint = this._endpoint();
        this._cancellable = new Gio.Cancellable();
        const cancellable = this._cancellable;
        let input = null;
        try {
            if (!endpoint)
                throw new Error('no token yet');
            const state = await this._json('GET', '/api/status');
            this._setState(state);
            this._backoff = 1;

            const message = this._message('GET', endpoint,
                `/api/events/stream?after=${state.latestEventId ?? 0}`);
            message.get_request_headers().append('Accept', 'text/event-stream');
            const stream = await this._session.send_async(
                message, GLib.PRIORITY_DEFAULT, cancellable);
            if (message.get_status() !== Soup.Status.OK)
                throw new Error(`event stream HTTP ${message.get_status()}`);
            input = new Gio.DataInputStream({base_stream: stream});
            await this._readStream(input, cancellable);
        } catch (e) {
            if (e.matches?.(Gio.IOErrorEnum, Gio.IOErrorEnum.CANCELLED))
                return;
        } finally {
            // Close explicitly so the daemon sees the disconnect right away.
            try {
                input?.close(null);
            } catch {}
        }
        if (!this._running || cancellable.is_cancelled())
            return;
        this._setState(null);
        this._retryId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, this._backoff, () => {
            this._connect().catch(e => logError(e, 'Desktop Bridge overlay'));
            return GLib.SOURCE_REMOVE;
        });
        this._backoff = Math.min(this._backoff * 2, MAX_BACKOFF_S);
    }

    async _readStream(input, cancellable) {
        let data = [];
        for (;;) {
            // eslint-disable-next-line no-await-in-loop
            const [raw] = await input.read_line_async(GLib.PRIORITY_DEFAULT, cancellable);
            if (raw === null)
                return;
            const line = typeof raw === 'string' ? raw : decoder.decode(raw);
            if (line === '') {
                if (data.length)
                    this._dispatch(data.join('\n'));
                data = [];
            } else if (line.startsWith('data:')) {
                data.push(line.slice(5).trimStart());
            }
        }
    }

    _dispatch(raw) {
        let event;
        try {
            event = JSON.parse(raw);
        } catch {
            return;
        }
        this.emit('event', event);
        if (STATE_EVENTS.test(event.type ?? ''))
            this._queueRefresh();
    }

    refresh() {
        if (this.online)
            this._queueRefresh();
    }

    _queueRefresh() {
        if (this._refreshId)
            return;
        this._refreshId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, REFRESH_DELAY_MS, () => {
            this._refreshId = 0;
            this._json('GET', '/api/status')
                .then(state => this._setState(state))
                .catch(() => {});
            return GLib.SOURCE_REMOVE;
        });
    }

    _setState(state) {
        if (state === null && this.state === null)
            return;
        this.state = state;
        this.emit('state', state);
    }
}
