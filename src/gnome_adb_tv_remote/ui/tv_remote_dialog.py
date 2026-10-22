"""
TV Remote Dialog for controlling external TV device.

This module provides the TvRemoteDialog class, a modal dialog that displays
a d-pad interface for controlling a separate TV device when the Input button
is used with an external TV IP configured. Commands go over its own remote
connection to that TV.
"""

from __future__ import annotations

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from .preferences_dialog import get_action_tooltip, _load_shortcuts_dict  # noqa: E402
from .ui_utils import create_icon, flash_button  # noqa: E402
from ..core.remote_v2 import AndroidTvRemote, DeviceInfo  # noqa: E402

logger = logging.getLogger(__name__)

# CSS for compact button layout
BUTTON_CSS = """
button.tv-remote-button {
    padding: 16px;
    min-width: 110px;
    min-height: 110px;
}

button.tv-remote-button image {
    -gtk-icon-style: symbolic;
}
"""

# D-pad keycodes
DPAD_KEYCODES = {
    "KEYCODE_DPAD_UP": "dpad-up",
    "KEYCODE_DPAD_DOWN": "dpad-down",
    "KEYCODE_DPAD_LEFT": "dpad-left",
    "KEYCODE_DPAD_RIGHT": "dpad-right",
    "KEYCODE_DPAD_CENTER": "dpad-center",
}


class TvRemoteDialog(Adw.Window):
    """Modal dialog for controlling external TV device with d-pad."""

    def __init__(self, parent: Gtk.Window, tv_ip: str, settings: Gio.Settings, tv_remote: AndroidTvRemote | None = None, tv_device_info: DeviceInfo | None = None) -> None:
        super().__init__(
            transient_for=parent,
            modal=True,
            title="TV Remote",
            default_width=280,
            default_height=280,
        )
        
        self._tv_ip = tv_ip
        self._settings = settings
        self._tv_remote = tv_remote  # Connection to the TV
        self._tv_device_info = tv_device_info  # Device info for display
        self._key_map: dict[int, str] = {}
        self._title_widget: Adw.WindowTitle | None = None
        self._selection_made = False  # Track if OK/Select was pressed (to avoid re-sending TV_INPUT on close)
        
        self._build_ui()
        self._load_dpad_shortcuts()
        
        # Send Input command immediately
        if self._tv_remote and self._tv_remote.connected:
            self._send_input_command_to_tv()
        
        # Add keyboard controller for shortcuts and Escape
        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)
        
        self.connect("close-request", self._on_close_request)

    def _on_close_request(self, *_args) -> bool:
        """Send Input command and close the dialog."""
        # Only send Input command to TV if no selection was made
        # (If user selected an input, the menu is already closed - don't reopen it!)
        if not self._selection_made:
            self._send_input_command_to_tv()
        self.hide()
        return True

    def _on_key_pressed(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType) -> bool:
        """Handle keyboard shortcuts for d-pad, Escape, and Back keys."""
        # Escape closes the dialog (Input signal will be sent in _on_close_request)
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        
        # Check for Back key (Q) - closes the dialog (Input signal will be sent in _on_close_request)
        from .preferences_dialog import ACTION_TO_KEYCODE, gdk_name_to_keyval, _load_shortcuts_dict
        shortcuts = _load_shortcuts_dict(self._settings)
        back_keys = shortcuts.get("back", [])
        for key_name in back_keys:
            keyval_back = gdk_name_to_keyval(key_name)
            if keyval_back and (keyval == keyval_back or Gdk.keyval_to_lower(keyval) == keyval_back):
                self.close()
                return True
        
        # Handle d-pad shortcuts
        lower_keyval = Gdk.keyval_to_lower(keyval)
        keycode = self._key_map.get(keyval) or self._key_map.get(lower_keyval)
        
        if keycode and keycode in DPAD_KEYCODES:
            # If OK/Select (KEYCODE_DPAD_CENTER), close dialog after sending command
            if keycode == "KEYCODE_DPAD_CENTER":
                # Mark that a selection was made (so we don't re-send TV_INPUT on close)
                self._selection_made = True
                # Flash the button
                self._flash_button(keycode)
                # Send command to TV
                self._send_keycode_to_tv(keycode)
                # Close dialog after a short delay
                GLib.timeout_add(100, lambda: self.close() or False)
                return True
            
            # Flash the button
            self._flash_button(keycode)
            # Send command to TV
            self._send_keycode_to_tv(keycode)
            return True
        
        return False

    def _load_dpad_shortcuts(self) -> None:
        """Load d-pad keyboard shortcuts from settings."""
        from .preferences_dialog import ACTION_TO_KEYCODE, gdk_name_to_keyval
        
        shortcuts = _load_shortcuts_dict(self._settings)
        
        # Build keyval -> keycode mapping for d-pad only
        self._key_map = {}
        dpad_actions = ["dpad-up", "dpad-down", "dpad-left", "dpad-right", "dpad-center"]
        
        for action in dpad_actions:
            keycode = ACTION_TO_KEYCODE.get(action)
            if keycode is None:
                continue
            key_names = shortcuts.get(action, [])
            for key_name in key_names:
                keyval = gdk_name_to_keyval(key_name)
                if keyval is not None:
                    self._key_map[keyval] = keycode

    def _update_button_tooltips(self) -> None:
        """Update button tooltips with keyboard shortcuts."""
        from .preferences_dialog import get_action_tooltip
        
        # Mapping from keycode to action name
        keycode_to_action = {
            "KEYCODE_DPAD_UP": "dpad-up",
            "KEYCODE_DPAD_DOWN": "dpad-down",
            "KEYCODE_DPAD_LEFT": "dpad-left",
            "KEYCODE_DPAD_RIGHT": "dpad-right",
            "KEYCODE_DPAD_CENTER": "dpad-center",
        }
        
        # Human-readable names
        keycode_to_label = {
            "KEYCODE_DPAD_UP": "Up",
            "KEYCODE_DPAD_DOWN": "Down",
            "KEYCODE_DPAD_LEFT": "Left",
            "KEYCODE_DPAD_RIGHT": "Right",
            "KEYCODE_DPAD_CENTER": "OK/Select",
        }
        
        for keycode, button in self._keycode_buttons.items():
            action = keycode_to_action.get(keycode)
            label = keycode_to_label.get(keycode, "")
            if action:
                shortcut_text = get_action_tooltip(action, self._settings)
                if shortcut_text:
                    button.set_tooltip_text(f"{label}: {shortcut_text}")
                else:
                    button.set_tooltip_text(label)

    def _send_input_command_to_tv(self) -> None:
        """Send KEYCODE_TV_INPUT command to TV device."""
        if not self._tv_remote or not self._tv_remote.connected:
            return
        
        try:
            self._tv_remote.send_keycode("KEYCODE_TV_INPUT")
        except Exception as e:
            logger.error(f"Failed to send Input command: {e}")

    def _send_keycode_to_tv(self, keycode: str) -> None:
        """Send keycode to TV device."""
        if not self._tv_remote or not self._tv_remote.connected:
            return
        
        try:
            self._tv_remote.send_keycode(keycode)
        except Exception as e:
            logger.error(f"Failed to send keycode {keycode}: {e}")

    def _flash_button(self, keycode: str) -> None:
        """Flash the button corresponding to the keycode."""
        btn = self._keycode_buttons.get(keycode)
        if not btn:
            return
        flash_button(btn)

    def _build_ui(self) -> None:
        """Build the dialog UI."""
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        # Show manufacturer + model if available, otherwise show IP
        if self._tv_device_info:
            subtitle = f"{self._tv_device_info.manufacturer} {self._tv_device_info.model}"
        else:
            subtitle = self._tv_ip
        self._title_widget = Adw.WindowTitle(title="TV Remote", subtitle=subtitle)
        header.set_title_widget(self._title_widget)
        toolbar_view.add_top_bar(header)

        # Apply CSS
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(BUTTON_CSS.encode())
        self.get_style_context().add_provider(css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        # Content
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        content.set_valign(Gtk.Align.CENTER)
        content.set_halign(Gtk.Align.CENTER)

        # D-pad grid
        grid = Gtk.Grid(column_spacing=8, row_spacing=8)
        grid.set_column_homogeneous(True)
        grid.set_row_homogeneous(True)
        grid.set_halign(Gtk.Align.CENTER)
        grid.set_valign(Gtk.Align.CENTER)

        # Map to store keycode -> button
        self._keycode_buttons: dict[str, Gtk.Button] = {}

        # D-pad buttons
        self._add_key_button("Up", "KEYCODE_DPAD_UP", 1, 0, grid, icon_name="keyboard_arrow_up-symbolic.svg")
        self._add_key_button("Left", "KEYCODE_DPAD_LEFT", 0, 1, grid, icon_name="keyboard_arrow_left-symbolic.svg")
        self._add_key_button("Enter", "KEYCODE_DPAD_CENTER", 1, 1, grid, icon_name="fiber_manual_record-symbolic.svg")
        self._add_key_button("Right", "KEYCODE_DPAD_RIGHT", 2, 1, grid, icon_name="keyboard_arrow_right-symbolic.svg")
        self._add_key_button("Down", "KEYCODE_DPAD_DOWN", 1, 2, grid, icon_name="keyboard_arrow_down-symbolic.svg")

        content.append(grid)

        toolbar_view.set_content(content)
        
        # Update tooltips after buttons are created
        self._update_button_tooltips()

    def _add_key_button(self, label: str, keycode: str, col: int, row: int, grid: Gtk.Grid, icon_name: str | None = None) -> None:
        """Add a key button to the grid."""
        btn = Gtk.Button()
        btn.add_css_class("tv-remote-button")
        btn.set_tooltip_text(label)
        btn.set_hexpand(True)
        btn.set_vexpand(True)
        
        btn.connect("clicked", lambda *_: self._on_button_clicked(keycode))
        
        # Create icon or label
        if icon_name:
            image = create_icon(icon_name, pixel_size=40)
            btn.set_child(image)
        else:
            btn.set_label(label)
        
        grid.attach(btn, col, row, 1, 1)
        self._keycode_buttons[keycode] = btn

    def _on_button_clicked(self, keycode: str) -> None:
        """Handle button click - send keycode and close dialog if OK/Select was pressed."""
        self._send_keycode_to_tv(keycode)
        
        # If OK/Select (KEYCODE_DPAD_CENTER), close dialog after sending command
        if keycode == "KEYCODE_DPAD_CENTER":
            # Mark that a selection was made (so we don't re-send TV_INPUT on close)
            self._selection_made = True
            # Close dialog after a short delay
            GLib.timeout_add(100, lambda: self.close() or False)

