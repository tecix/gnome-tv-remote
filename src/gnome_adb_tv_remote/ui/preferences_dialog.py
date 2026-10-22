"""
Preferences Dialog for Keyboard Shortcut Configuration.

This module provides the PreferencesDialog class for customizing keyboard
shortcuts, along with helper functions for loading shortcut configurations
from GSettings and converting between key names and values.
"""

from __future__ import annotations

import json
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..core.home_assistant import (  # noqa: E402
    DEFAULT_ENTITY,
    HomeAssistantError,
    HomeAssistantRemote,
    load_token,
    save_token,
)


# Default keyboard shortcuts mapping: action -> list of Gdk key names
DEFAULT_SHORTCUTS: dict[str, list[str]] = {
    "dpad-up": ["Up", "w"],
    "dpad-down": ["Down", "s"],
    "dpad-left": ["Left", "a"],
    "dpad-right": ["Right", "d"],
    "dpad-center": ["Return", "KP_Enter", "e"],
    "back": ["Escape", "q", "BackSpace"],
    "home": ["h"],
    "menu": ["i", "Idotabove"],
    "volume-up": ["plus", "KP_Add", "period"],
    "volume-down": ["minus", "KP_Subtract", "comma"],
    "volume-mute": ["m"],
    "power": ["Delete"],
    "play-pause": ["space"],
    "previous": ["z"],
    "next": ["x"],
    "apps": ["r"],
    "assistant": ["g"],
    "captions": ["c"],
    "tv-input": ["t"],
    "focus-keyboard": ["Tab"],
    "search": ["f"],
    "notifications": ["n"],
    "toggle-home-assistant": ["grave"],
}

# Action to ADB keycode mapping
ACTION_TO_KEYCODE: dict[str, str] = {
    "dpad-up": "KEYCODE_DPAD_UP",
    "dpad-down": "KEYCODE_DPAD_DOWN",
    "dpad-left": "KEYCODE_DPAD_LEFT",
    "dpad-right": "KEYCODE_DPAD_RIGHT",
    "dpad-center": "KEYCODE_DPAD_CENTER",
    "back": "KEYCODE_BACK",
    "home": "KEYCODE_HOME",
    "menu": "KEYCODE_MENU",
    "volume-up": "KEYCODE_VOLUME_UP",
    "volume-down": "KEYCODE_VOLUME_DOWN",
    "volume-mute": "KEYCODE_VOLUME_MUTE",
    "power": "KEYCODE_POWER",
    "play-pause": "KEYCODE_MEDIA_PLAY_PAUSE",
    "previous": "KEYCODE_MEDIA_PREVIOUS",
    "next": "KEYCODE_MEDIA_NEXT",
    "apps": "KEYCODE_ALL_APPS",
    "assistant": "KEYCODE_ASSIST",
    "captions": "KEYCODE_CAPTIONS",
    "tv-input": "KEYCODE_TV_INPUT",
    "focus-keyboard": None,  # Special action, not an ADB keycode
    "search": None,  # Special action, sends text "s" instead of keycode
    "notifications": None,  # Special action, expands notification panel
    "toggle-home-assistant": None,  # Special action, switches the button route
}

# Human-readable action names
ACTION_LABELS: dict[str, str] = {
    "dpad-up": "Up",
    "dpad-down": "Down",
    "dpad-left": "Left",
    "dpad-right": "Right",
    "dpad-center": "OK/Select",
    "back": "Back",
    "home": "Home",
    "menu": "Menu",
    "volume-up": "Volume Up",
    "volume-down": "Volume Down",
    "volume-mute": "Mute",
    "power": "Power",
    "play-pause": "Play/Pause",
    "previous": "Previous",
    "next": "Next",
    "apps": "Apps",
    "assistant": "Google Assistant",
    "captions": "Captions/Subtitles",
    "tv-input": "Input",
    "focus-keyboard": "Focus Keyboard",
    "search": "Find (YouTube)",
    "notifications": "Notifications",
    "toggle-home-assistant": "Switch Direct / Home Assistant",
}

ACTION_CATEGORIES: dict[str, list[str]] = {
    "Navigation": ["dpad-up", "dpad-down", "dpad-left", "dpad-right", "dpad-center"],
    "System": ["back", "home", "menu", "apps", "assistant"],
    "Volume": ["volume-up", "volume-down", "volume-mute"],
    "Media": ["play-pause", "previous", "next"],
    "TV Controls": ["power", "captions", "tv-input", "notifications"],
    "Other": ["focus-keyboard", "search", "toggle-home-assistant"],
}


def get_key_display_name(key_name: str) -> str:
    """Convert Gdk key name to human-readable display name."""
    display_map = {
        "Up": "↑",
        "Down": "↓",
        "Left": "←",
        "Right": "→",
        "Return": "Enter",
        "KP_Enter": "Enter",
        "Escape": "Esc",
        "BackSpace": "Backspace",
        "space": "Space",
        "plus": "+",
        "minus": "-",
        "equal": "=",
        "period": ".",
        "comma": ",",
        "KP_Add": "+",
        "KP_Subtract": "-",
        "Idotabove": "I",
        "grave": "`",
    }
    return display_map.get(key_name, key_name.upper() if len(key_name) == 1 else key_name)


def gdk_keyval_to_name(keyval: int) -> str | None:
    """Convert Gdk keyval to key name string."""
    name = Gdk.keyval_name(keyval)
    return name


def gdk_name_to_keyval(name: str) -> int | None:
    """Convert key name string to Gdk keyval."""
    keyval = Gdk.keyval_from_name(name)
    return keyval if keyval != Gdk.KEY_VoidSymbol else None


def _load_shortcuts_dict(settings: Gio.Settings) -> dict[str, list[str]]:
    """Load shortcuts from settings, merging with defaults.
    
    This is the single source of truth for loading shortcut configuration.
    Returns a dict mapping action names to lists of key names.
    """
    shortcuts = {k: v.copy() for k, v in DEFAULT_SHORTCUTS.items()}
    try:
        custom = json.loads(settings.get_string("keyboard-shortcuts"))
        if custom:
            for action, keys in custom.items():
                if action in shortcuts:
                    shortcuts[action] = keys
    except (json.JSONDecodeError, TypeError):
        pass
    return shortcuts


class ShortcutButton(Gtk.Button):
    """A button that captures keyboard input to set a shortcut."""

    def __init__(self, action: str, key_names: list[str], on_change: Callable[[str, list[str]], None]) -> None:
        super().__init__()
        self._action = action
        self._key_names = key_names.copy()
        self._on_change = on_change
        self._listening = False

        self.set_hexpand(True)
        self._update_label()

        self.connect("clicked", self._on_clicked)

        # Key controller for capturing shortcuts
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

        # Focus controller to stop listening when focus is lost
        focus_controller = Gtk.EventControllerFocus()
        focus_controller.connect("leave", self._on_focus_leave)
        self.add_controller(focus_controller)

    def _update_label(self) -> None:
        if self._listening:
            self.set_label("Press a key…")
            self.add_css_class("suggested-action")
        else:
            if self._key_names:
                # Filter out numpad keys (KP_*) from display
                non_numpad_keys = [k for k in self._key_names if not k.startswith("KP_")]
                if non_numpad_keys:
                    # Get human-readable names and deduplicate (e.g. i and Idotabove both show as I)
                    display_names = list(dict.fromkeys(get_key_display_name(k) for k in non_numpad_keys))
                    self.set_label(" / ".join(display_names))
                else:
                    # If only numpad keys exist, show "Not set" instead
                    self.set_label("Not set")
            else:
                self.set_label("Not set")
            self.remove_css_class("suggested-action")

    def _on_clicked(self, *_args) -> None:
        self._listening = True
        self._update_label()

    def _on_focus_leave(self, *_args) -> None:
        self._listening = False
        self._update_label()

    def _on_key_pressed(self, _controller, keyval: int, _keycode: int, state: Gdk.ModifierType) -> bool:
        if not self._listening:
            return False

        # Escape cancels editing
        if keyval == Gdk.KEY_Escape:
            self._listening = False
            self._update_label()
            return True

        # Ignore modifier keys alone
        if keyval in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L,
                      Gdk.KEY_Control_R, Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
                      Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Caps_Lock):
            return True

        key_name = gdk_keyval_to_name(keyval)
        if key_name:
            # Replace shortcuts with the new key
            self._key_names = [key_name]
            self._listening = False
            self._update_label()
            self._on_change(self._action, self._key_names)
            return True

        return False

    def get_key_names(self) -> list[str]:
        return self._key_names.copy()

    def set_key_names(self, key_names: list[str]) -> None:
        self._key_names = key_names.copy()
        self._update_label()


class PreferencesDialog(Adw.Dialog):
    """Preferences dialog for configuring keyboard shortcuts."""

    def __init__(self, parent: Gtk.Window) -> None:
        super().__init__()
        self._parent = parent
        self._settings = Gio.Settings.new("io.github.erenseymen.android-tv-remote")
        self._shortcut_buttons: dict[str, ShortcutButton] = {}
        self._shortcuts = self._load_shortcuts()

        self.set_title("Configure Shortcuts")
        self.set_content_width(450)
        self.set_content_height(600)

        self._build_ui()

    def _load_shortcuts(self) -> dict[str, list[str]]:
        """Load shortcuts from settings, falling back to defaults."""
        return _load_shortcuts_dict(self._settings)

    def _save_shortcuts(self) -> None:
        """Save custom shortcuts to settings."""
        # Only save differences from defaults
        custom = {}
        for action, keys in self._shortcuts.items():
            if keys != DEFAULT_SHORTCUTS.get(action, []):
                custom[action] = keys
        self._settings.set_string("keyboard-shortcuts", json.dumps(custom))

        # Notify parent window to reload shortcuts
        if hasattr(self._parent, "reload_shortcuts"):
            self._parent.reload_shortcuts()

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_child(toolbar_view)

        # Header bar
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        # Reset button
        reset_btn = Gtk.Button(label="Reset All")
        reset_btn.add_css_class("destructive-action")
        reset_btn.connect("clicked", self._on_reset_all)
        header.pack_end(reset_btn)

        # Scrolled content
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        toolbar_view.set_content(scrolled)

        # Main content box
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        scrolled.set_child(content)

        # Info banner
        info_label = Gtk.Label(
            label="Click a shortcut button and press a key to change it. Press Escape to cancel.",
            wrap=True,
            xalign=0,
        )
        info_label.add_css_class("dim-label")
        content.append(info_label)

        # TV IP Configuration Group
        tv_ip_group = Adw.PreferencesGroup(title="TV Input Routing")
        content.append(tv_ip_group)
        
        tv_ip_row = Adw.ActionRow(title="TV IP Address")
        tv_ip_row.set_subtitle("Input button will be sent to this TV (leave empty to use current device)")
        
        tv_ip_entry = Gtk.Entry()
        tv_ip_entry.set_placeholder_text("e.g. 192.168.1.100")
        tv_ip_entry.set_input_purpose(Gtk.InputPurpose.URL)
        tv_ip_entry.set_hexpand(True)
        tv_ip_entry.set_valign(Gtk.Align.CENTER)
        
        # Load current TV IP from settings
        current_tv_ip = self._settings.get_string("tv-ip")
        if current_tv_ip:
            tv_ip_entry.set_text(current_tv_ip)
        
        # Save TV IP when changed
        def on_tv_ip_changed(entry):
            ip = entry.get_text().strip()
            self._settings.set_string("tv-ip", ip)
        
        tv_ip_entry.connect("changed", on_tv_ip_changed)
        tv_ip_row.add_suffix(tv_ip_entry)
        tv_ip_group.add(tv_ip_row)

        content.append(self._build_home_assistant_group())

        # Create preference groups for each category
        for category, actions in ACTION_CATEGORIES.items():
            group = Adw.PreferencesGroup(title=GLib.markup_escape_text(category))
            content.append(group)

            for action in actions:
                row = Adw.ActionRow(title=GLib.markup_escape_text(ACTION_LABELS.get(action, action)))

                # Shortcut button
                btn = ShortcutButton(
                    action,
                    self._shortcuts.get(action, []),
                    self._on_shortcut_changed,
                )
                btn.set_valign(Gtk.Align.CENTER)
                row.add_suffix(btn)
                self._shortcut_buttons[action] = btn

                # Reset button for individual action
                reset_action_btn = Gtk.Button(icon_name="edit-undo-symbolic")
                reset_action_btn.set_valign(Gtk.Align.CENTER)
                reset_action_btn.set_tooltip_text("Reset to default")
                reset_action_btn.add_css_class("flat")
                reset_action_btn.connect("clicked", self._on_reset_action, action)
                row.add_suffix(reset_action_btn)

                group.add(row)

    def _build_home_assistant_group(self) -> Adw.PreferencesGroup:
        """Settings for sending buttons through Home Assistant's ADB integration."""
        group = Adw.PreferencesGroup(
            title="Home Assistant",
            description=(
                "Buttons can go through Home Assistant's Android Debug Bridge integration instead, "
                "for screens the TV's remote service can't control. Switch with the header's HA "
                "button or its shortcut."
            ),
        )

        url_row = Adw.EntryRow(title="URL (e.g. https://ha.example.com)")
        url_row.set_text(self._settings.get_string("home-assistant-url"))
        url_row.set_input_purpose(Gtk.InputPurpose.URL)
        url_row.set_show_apply_button(True)
        url_row.connect("apply", lambda row: self._apply_home_assistant(url=row.get_text().strip()))
        group.add(url_row)

        token_row = Adw.PasswordEntryRow(title="Long-lived access token")
        token_row.set_text(load_token())
        token_row.set_show_apply_button(True)
        token_row.connect("apply", lambda row: self._apply_home_assistant(token=row.get_text().strip()))
        group.add(token_row)

        entity_row = Adw.EntryRow(title=f"Remote entity (default {DEFAULT_ENTITY})")
        entity_row.set_text(self._settings.get_string("home-assistant-entity"))
        entity_row.set_show_apply_button(True)
        entity_row.connect("apply", lambda row: self._apply_home_assistant(entity=row.get_text().strip()))
        group.add(entity_row)

        self._ha_test_row = Adw.ActionRow(title="Connection", subtitle="Not tested")
        test_btn = Gtk.Button(label="Test")
        test_btn.set_valign(Gtk.Align.CENTER)
        test_btn.connect("clicked", self._on_test_home_assistant)
        self._ha_test_row.add_suffix(test_btn)
        group.add(self._ha_test_row)
        return group

    def _apply_home_assistant(self, *, url: str | None = None, token: str | None = None, entity: str | None = None) -> None:
        if url is not None:
            self._settings.set_string("home-assistant-url", url)
        if token is not None:
            save_token(token)
        if entity is not None:
            self._settings.set_string("home-assistant-entity", entity or DEFAULT_ENTITY)
        if hasattr(self._parent, "reload_home_assistant"):
            self._parent.reload_home_assistant()
        self._ha_test_row.set_subtitle("Saved, not tested")

    def _on_test_home_assistant(self, btn: Gtk.Button) -> None:
        try:
            ha = HomeAssistantRemote(
                self._settings.get_string("home-assistant-url"),
                load_token(),
                self._settings.get_string("home-assistant-entity"),
            )
        except HomeAssistantError as e:
            self._ha_test_row.set_subtitle(str(e))
            return

        btn.set_sensitive(False)
        self._ha_test_row.set_subtitle("Testing…")

        def show(name: str | None, error: str | None) -> bool:
            btn.set_sensitive(True)
            self._ha_test_row.set_subtitle(f"OK: {name}" if name else (error or "Failed"))
            return False

        ha.check_async(lambda name, error: GLib.idle_add(show, name, error))
        ha.close()

    def _on_shortcut_changed(self, action: str, key_names: list[str]) -> None:
        """Called when a shortcut is changed."""
        self._shortcuts[action] = key_names
        self._save_shortcuts()

    def _on_reset_action(self, _btn: Gtk.Button, action: str) -> None:
        """Reset a single action to its default shortcut."""
        default_keys = DEFAULT_SHORTCUTS.get(action, [])
        self._shortcuts[action] = default_keys.copy()
        self._shortcut_buttons[action].set_key_names(default_keys)
        self._save_shortcuts()

    def _on_reset_all(self, *_args) -> None:
        """Reset all shortcuts to defaults."""
        self._shortcuts = {k: v.copy() for k, v in DEFAULT_SHORTCUTS.items()}
        for action, btn in self._shortcut_buttons.items():
            btn.set_key_names(DEFAULT_SHORTCUTS.get(action, []))
        self._save_shortcuts()


def load_shortcuts_from_settings(settings: Gio.Settings) -> dict[int, str]:
    """Load keyboard shortcuts and return a mapping of Gdk keyval -> ADB keycode.

    This function is used by MainWindow to get the current shortcut configuration.
    """
    shortcuts = _load_shortcuts_dict(settings)

    # Build keyval -> keycode mapping
    key_map: dict[int, str] = {}
    for action, key_names in shortcuts.items():
        keycode = ACTION_TO_KEYCODE.get(action)
        if keycode is None:
            continue  # Skip special actions like focus-keyboard
        for key_name in key_names:
            keyval = gdk_name_to_keyval(key_name)
            if keyval is not None:
                key_map[keyval] = keycode

    return key_map


def get_focus_keyboard_keys(settings: Gio.Settings) -> list[int]:
    """Get the Gdk keyvals for the focus-keyboard action."""
    shortcuts = _load_shortcuts_dict(settings)
    keyvals = []
    for key_name in shortcuts.get("focus-keyboard", []):
        keyval = gdk_name_to_keyval(key_name)
        if keyval is not None:
            keyvals.append(keyval)
    return keyvals


def get_search_keys(settings: Gio.Settings) -> list[int]:
    """Get the Gdk keyvals for the search action."""
    shortcuts = _load_shortcuts_dict(settings)
    keyvals = []
    for key_name in shortcuts.get("search", []):
        keyval = gdk_name_to_keyval(key_name)
        if keyval is not None:
            keyvals.append(keyval)
    return keyvals


def get_notifications_keys(settings: Gio.Settings) -> list[int]:
    """Get the Gdk keyvals for the notifications action."""
    shortcuts = _load_shortcuts_dict(settings)
    keyvals = []
    for key_name in shortcuts.get("notifications", []):
        keyval = gdk_name_to_keyval(key_name)
        if keyval is not None:
            keyvals.append(keyval)
    return keyvals


def get_toggle_home_assistant_keys(settings: Gio.Settings) -> list[int]:
    """Get the Gdk keyvals for the toggle-home-assistant action."""
    shortcuts = _load_shortcuts_dict(settings)
    keyvals = []
    for key_name in shortcuts.get("toggle-home-assistant", []):
        keyval = gdk_name_to_keyval(key_name)
        if keyval is not None:
            keyvals.append(keyval)
    return keyvals


def get_action_tooltip(action: str, settings: Gio.Settings) -> str:
    """Get tooltip text showing the current shortcut for an action."""
    shortcuts = _load_shortcuts_dict(settings)
    key_names = shortcuts.get(action, [])
    if key_names:
        # Filter out numpad keys (KP_*) from display
        non_numpad_keys = [k for k in key_names if not k.startswith("KP_")]
        if non_numpad_keys:
            # Get human-readable names and deduplicate (e.g. Enter and Numpad Enter)
            display_names = list(dict.fromkeys(get_key_display_name(k) for k in non_numpad_keys))
            return " / ".join(display_names)
    return ""


