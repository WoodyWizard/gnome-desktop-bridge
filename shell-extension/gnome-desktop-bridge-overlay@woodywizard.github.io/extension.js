// Desktop Bridge Overlay: visible feedback for GNOME Desktop Bridge agents.
import * as Main from 'resource:///org/gnome/shell/ui/main.js';
import {Extension} from 'resource:///org/gnome/shell/extensions/extension.js';

import {BridgeConnection} from './connection.js';
import {Indicator} from './indicator.js';
import {Overlay} from './overlay.js';
import {WindowLocator} from './windows.js';

export default class DesktopBridgeOverlayExtension extends Extension {
    enable() {
        this._connection = new BridgeConnection();
        this._overlay = new Overlay(this._connection);
        this._indicator = new Indicator(this, this._connection);
        Main.panel.addToStatusArea(this.uuid, this._indicator);
        this._locator = new WindowLocator(this.metadata.version);
        this._connection.start();
    }

    disable() {
        this._connection.stop();
        this._locator.destroy();
        this._indicator.destroy();
        this._overlay.destroy();
        this._connection = this._overlay = this._indicator = this._locator = null;
    }
}
