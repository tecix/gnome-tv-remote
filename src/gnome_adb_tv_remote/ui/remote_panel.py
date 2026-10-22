"""
Remote Control Panel Widget.

This module provides the RemotePanel class, the main remote control interface
that displays navigation buttons, volume controls, media buttons, and a text
input area. Button presses are translated to ADB key events.
"""

from __future__ import annotations

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Adw", "1")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk  # noqa: E402

from ..core.remote_v2 import DeviceInfo  # noqa: E402
from .ui_utils import create_icon, flash_button  # noqa: E402


# CSS for compact button fonts and volume slider
BUTTON_CSS = """
button.remote-button {
    padding: 6px;
    min-width: 48px;
    min-height: 48px;
}

button.remote-button image {
    -gtk-icon-style: symbolic;
}

button.remote-button label.caption {
    font-size: 0.8em;
    font-weight: 700;
    opacity: 0.9;
    margin-top: 2px;
}

.volume-slider-box {
    margin-top: 4px;
    margin-bottom: 4px;
}

.volume-slider-box scale {
    min-height: 24px;
}

.volume-slider-box scale trough {
    min-height: 6px;
    border-radius: 3px;
}

.volume-slider-box scale highlight {
    border-radius: 3px;
}

.volume-mute-button {
    min-width: 32px;
    min-height: 32px;
    padding: 6px;
}

.media-controls-section {
    padding: 12px;
    margin: 8px 0;
    border-radius: 12px;
    background: alpha(@accent_bg_color, 0.08);
    border: 1px solid alpha(@accent_color, 0.2);
}

.media-controls-section:backdrop {
    background: alpha(@window_bg_color, 0.15);
    border-color: alpha(@borders, 0.3);
}

.media-buttons-row {
    margin-bottom: 8px;
}

.media-buttons-row button {
    min-width: 48px;
    min-height: 40px;
    padding: 8px 16px;
}

.now-playing-box {
    padding: 16px;
    margin: 4px;
    margin-bottom: 12px;
    border-radius: 12px;
    background-image: linear-gradient(135deg, alpha(@accent_bg_color, 0.2), alpha(@accent_bg_color, 0.05));
    border: 1px solid alpha(@accent_color, 0.1);
    box-shadow: 0 1px 2px rgba(0, 0, 0, 0.05);
}

.now-playing-box:backdrop {
    background-image: none;
    background-color: alpha(@window_bg_color, 0.5);
    box-shadow: none;
}

.now-playing-title {
    font-weight: 800;
    font-size: 1.1em;
    color: @accent_color;
    margin-bottom: 2px;
    text-shadow: 0 1px 0 rgba(0,0,0,0.1);
}

.now-playing-artist {
    opacity: 0.9;
    font-size: 0.95em;
    font-weight: 500;
}

.now-playing-status {
    opacity: 0.7;
    font-size: 0.8em;
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

.now-playing-icon {
    opacity: 0.9;
    color: @accent_color;
    background: alpha(@accent_color, 0.1);
    border-radius: 8px;
    padding: 8px;
    margin-right: 4px;
}
"""


# Mapping from Android keycode to action name (for tooltip lookup)
KEYCODE_TO_ACTION: dict[str, str] = {
    "KEYCODE_DPAD_UP": "dpad-up",
    "KEYCODE_DPAD_DOWN": "dpad-down",
    "KEYCODE_DPAD_LEFT": "dpad-left",
    "KEYCODE_DPAD_RIGHT": "dpad-right",
    "KEYCODE_DPAD_CENTER": "dpad-center",
    "KEYCODE_BACK": "back",
    "KEYCODE_HOME": "home",
    "KEYCODE_MENU": "menu",
    "KEYCODE_VOLUME_MUTE": "volume-mute",
    "KEYCODE_MEDIA_PLAY_PAUSE": "play-pause",
    "KEYCODE_MEDIA_PREVIOUS": "previous",
    "KEYCODE_MEDIA_NEXT": "next",
    "KEYCODE_ALL_APPS": "apps",
    "KEYCODE_ASSIST": "assistant",
    "KEYCODE_CAPTIONS": "captions",
    "KEYCODE_TV_INPUT": "tv-input",
}


class RemotePanel(Gtk.Box):
    """Remote control panel widget with navigation and media buttons.
    
    Displays a grid of buttons for D-pad navigation, system controls,
    volume, and media playback. Also provides a keyboard input area
    for typing text on the connected device.
    """

    def __init__(self) -> None:
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self.set_margin_top(8)
        self.set_margin_bottom(8)
        self.set_margin_start(8)
        self.set_margin_end(8)
        
        # Apply CSS for larger button fonts
        css_provider = Gtk.CssProvider()
        css_provider.load_from_data(BUTTON_CSS.encode())
        self.get_style_context().add_provider(css_provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

        self._title = Adw.WindowTitle(title="Remote", subtitle="Connect to a device to enable controls")
        self.append(self._title)

        # Connection status indicator
        self._status_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._status_box.set_halign(Gtk.Align.CENTER)
        self._status_box.set_margin_top(8)
        self._status_box.set_margin_bottom(8)
        
        self._status_spinner = Gtk.Spinner()
        self._status_spinner.set_size_request(16, 16)
        self._status_box.append(self._status_spinner)
        
        self._status_label = Gtk.Label()
        self._status_label.add_css_class("dim-label")
        self._status_box.append(self._status_label)
        
        self._status_box.set_visible(False)
        self.append(self._status_box)
        
        self._grid = Gtk.Grid(column_spacing=6, row_spacing=6)
        self._grid.set_column_homogeneous(True)
        self._grid.set_row_homogeneous(True)
        self.append(self._grid)

        self._on_keyevent = None
        self._on_text = None
        self._on_keyboard_focus = None
        self._device_subtitle: str | None = None
        self._input_route: str | None = None  # None = buttons go directly to the TV

        # Map keycode -> button for visual feedback
        self._keycode_buttons: dict[str, Gtk.Button] = {}
        # Map keycode -> shortcut label for updating shortcuts
        self._keycode_shortcut_labels: dict[str, Gtk.Label] = {}
        # Search button (special, sends text instead of keycode)
        self._search_button: Gtk.Button | None = None
        self._search_shortcut_label: Gtk.Label | None = None
        
        # Volume slider components
        self._volume_slider: Gtk.Scale | None = None
        self._mute_button: Gtk.Button | None = None
        self._volume_max: int = 15  # Will be updated from device
        self._on_volume_change = None  # Callback for volume changes
        self._updating_slider = False  # Prevent feedback loops
        self._volume_max: int = 15  # Will be updated from device
        self._on_volume_change = None  # Callback for volume changes
        self._updating_slider = False  # Prevent feedback loops
        self._is_muted: bool = False  # Track mute state for UI toggle
        self._volume_initialized: bool = False  # Track if volume has been fetched from device
        
        # Cache for shortcuts to persist them when tooltips are updated dynamically
        self._cached_shortcuts: dict[str, str] = {}
        
        # Notifications button (special handler)
        self._notifications_button: Gtk.Button | None = None
        self._notifications_shortcut_label: Gtk.Label | None = None
        
        # Play/Pause button and icon for dynamic updates
        self._play_pause_button: Gtk.Button | None = None
        self._play_pause_icon: Gtk.Image | None = None
        
        # Key repeat timer for mouse hold-to-repeat on D-pad buttons
        self._key_repeat_timer_id: int | None = None
        self._key_repeat_keycode: str | None = None
        
        # Long-press timer and state for OK/Enter button
        self._long_press_timer_id: int | None = None
        self._long_press_keycode: str | None = None
        self._long_press_triggered: bool = False
        self._on_long_press = None  # Callback for long-press events

        # D-pad (rows 0-2) - these support hold-to-repeat for fast seeking
        self._add_key_button_with_repeat("Up", "KEYCODE_DPAD_UP", 1, 0, icon_name="keyboard_arrow_up-symbolic.svg")
        self._add_key_button_with_repeat("Left", "KEYCODE_DPAD_LEFT", 0, 1, icon_name="keyboard_arrow_left-symbolic.svg")
        self._add_key_button_with_long_press("Enter", "KEYCODE_DPAD_CENTER", 1, 1, icon_name="fiber_manual_record-symbolic.svg")
        self._add_key_button_with_repeat("Right", "KEYCODE_DPAD_RIGHT", 2, 1, icon_name="keyboard_arrow_right-symbolic.svg")
        self._add_key_button_with_repeat("Down", "KEYCODE_DPAD_DOWN", 1, 2, icon_name="keyboard_arrow_down-symbolic.svg")

        # Row 3: Back, Home, Apps - all support long-press
        self._add_key_button_with_long_press("Back", "KEYCODE_BACK", 0, 3, icon_name="edit-undo-symbolic")
        self._add_key_button_with_long_press("Home", "KEYCODE_HOME", 1, 3, icon_name="user-home-symbolic")
        self._add_key_button_with_long_press("Apps", "KEYCODE_ALL_APPS", 2, 3, icon_name="view-app-grid-symbolic")

        # Row 4: Find, Assistant, Menu - all support long-press
        self._add_search_button(0, 4, icon_name="system-search-symbolic")
        self._add_key_button_with_long_press("Assistant", "KEYCODE_ASSIST", 1, 4, icon_name="audio-input-microphone-symbolic")
        self._add_key_button_with_long_press("Menu", "KEYCODE_MENU", 2, 4, icon_name="view-list-symbolic")

        # Row 5: Subtitles, Input, Notifications - all support long-press
        self._add_key_button_with_long_press("Subtitles", "KEYCODE_CAPTIONS", 0, 5, icon_name="media-view-subtitles-symbolic")
        self._add_key_button_with_long_press("Input", "KEYCODE_TV_INPUT", 1, 5, icon_name="video-display-symbolic")
        self._add_notifications_button(2, 5, icon_name="preferences-system-notifications-symbolic")

        # Keyboard input area - keystrokes are sent directly to Android TV
        self._keyboard_entry = Gtk.Entry(placeholder_text="Focus keyboard for text input")
        self._keyboard_entry.set_hexpand(True)
        self._keyboard_entry.set_editable(False)  # Read-only, but text is displayed and copyable
        self._keyboard_focused = False
        
        self._focus_keyboard_shortcut_text: str = "Tab"  # Default

        # Add focus state tracking
        focus_controller = Gtk.EventControllerFocus()
        focus_controller.connect("enter", self._on_keyboard_focus_enter)
        focus_controller.connect("leave", self._on_keyboard_focus_leave)
        self._keyboard_entry.add_controller(focus_controller)
        
        self.append(self._keyboard_entry)
        
        # Media Controls Section (now playing bar + player/volume controls at bottom)
        self._create_media_controls_section()

    def set_handlers(self, *, on_keyevent=None, on_text=None, on_volume_change=None, on_notifications=None, on_long_press=None, on_keyboard_focus=None) -> None:
        self._on_keyevent = on_keyevent
        self._on_text = on_text
        self._on_volume_change = on_volume_change
        self._on_notifications = on_notifications
        self._on_long_press = on_long_press
        self._on_keyboard_focus = on_keyboard_focus

    def update_tooltips(self, settings: Gio.Settings) -> None:
        """Update button shortcut labels based on current keyboard shortcuts."""
        from .preferences_dialog import get_action_tooltip

        # Mapping from keycode to action for direction buttons
        direction_keycodes = {
            "KEYCODE_DPAD_UP": "dpad-up",
            "KEYCODE_DPAD_DOWN": "dpad-down",
            "KEYCODE_DPAD_LEFT": "dpad-left",
            "KEYCODE_DPAD_RIGHT": "dpad-right",
        }
        
        # Human-readable names for tooltip
        direction_names = {
            "KEYCODE_DPAD_UP": "Up",
            "KEYCODE_DPAD_DOWN": "Down",
            "KEYCODE_DPAD_LEFT": "Left",
            "KEYCODE_DPAD_RIGHT": "Right",
        }

        # Updated mapping for tooltip lookup including media keys
        tooltip_action_map = {
            **KEYCODE_TO_ACTION,
            "KEYCODE_MEDIA_PREVIOUS": "previous",
            "KEYCODE_MEDIA_PLAY_PAUSE": "play-pause",
            "KEYCODE_MEDIA_NEXT": "next",
        }

        # Clear cache before rebuilding
        self._cached_shortcuts = {}

        for keycode, btn in self._keycode_buttons.items():
            action = tooltip_action_map.get(keycode)
            
            # Special handling for direction buttons
            if keycode in direction_keycodes:
                action = direction_keycodes[keycode]
                
                # Update tooltip to show all shortcuts
                shortcut_text = get_action_tooltip(action, settings)
                if shortcut_text:
                    btn.set_tooltip_text(f"{direction_names[keycode]}: {shortcut_text}")
                    self._cached_shortcuts[action] = shortcut_text
                
                # Never show shortcut label on direction buttons
                if keycode in self._keycode_shortcut_labels:
                    self._keycode_shortcut_labels[keycode].set_visible(False)
                continue
            
            # Special handling for Enter (dpad-center)
            if keycode == "KEYCODE_DPAD_CENTER":
                shortcut_text = get_action_tooltip("dpad-center", settings)
                
                if shortcut_text:
                    btn.set_tooltip_text(f"OK/Select: {shortcut_text}")
                    self._cached_shortcuts["dpad-center"] = shortcut_text
                
                if keycode in self._keycode_shortcut_labels:
                    self._keycode_shortcut_labels[keycode].set_visible(False)
                continue

            # Standard buttons and Media buttons
            if action:
                shortcut_text = get_action_tooltip(action, settings)
                if shortcut_text:
                    # Cache the shortcut
                    self._cached_shortcuts[action] = shortcut_text
                    
                    # Get action name for tooltip prefix
                    action_names = {
                        "back": "Back",
                        "home": "Home",
                        "menu": "Menu",
                        "apps": "Apps",
                        "assistant": "Assistant",
                        "captions": "Subtitles",
                        "tv-input": "Input",
                        "previous": "Prev",
                        "play-pause": "Play/Pause",
                        "next": "Next",
                    }
                    action_name = action_names.get(action, action.replace("-", " ").title())
                    btn.set_tooltip_text(f"{action_name}: {shortcut_text}")
            
            # Keep shortcut label hidden for all buttons
            if keycode in self._keycode_shortcut_labels:
                self._keycode_shortcut_labels[keycode].set_visible(False)
        
        # Update keyboard entry placeholder with focus shortcut
        focus_tooltip = get_action_tooltip("focus-keyboard", settings)
        if focus_tooltip:
            self._cached_shortcuts["focus-keyboard"] = focus_tooltip
            self._focus_keyboard_shortcut_text = focus_tooltip
            if not self._keyboard_focused:
                self._keyboard_entry.set_placeholder_text(f"Press {focus_tooltip} to focus keyboard")
        
        # Update search button tooltip
        if self._search_button:
            search_tooltip = get_action_tooltip("search", settings)
            if search_tooltip:
                self._cached_shortcuts["search"] = search_tooltip
                self._search_button.set_tooltip_text(f"Find (YouTube): {search_tooltip}")
            if self._search_shortcut_label:
                self._search_shortcut_label.set_visible(False)
        
        # Update notifications button tooltip
        if self._notifications_button:
            notif_tooltip = get_action_tooltip("notifications", settings)
            if notif_tooltip:
                self._cached_shortcuts["notifications"] = notif_tooltip
                self._notifications_button.set_tooltip_text(f"Notifications: {notif_tooltip}")
            if self._notifications_shortcut_label:
                self._notifications_shortcut_label.set_visible(False)
        
        # Update mute button tooltip
        if self._mute_button:
            mute_shortcut = get_action_tooltip("volume-mute", settings)
            if mute_shortcut:
                self._cached_shortcuts["volume-mute"] = mute_shortcut
                self._mute_button.set_tooltip_text(f"Mute: {mute_shortcut}")
            else:
                self._mute_button.set_tooltip_text("Mute")
        
        # Update volume slider tooltip
        if self._volume_slider:
            vol_up_shortcut = get_action_tooltip("volume-up", settings)
            vol_down_shortcut = get_action_tooltip("volume-down", settings)
            tooltip_parts = ["Volume"]
            if vol_up_shortcut:
                self._cached_shortcuts["volume-up"] = vol_up_shortcut
                tooltip_parts.append(f"Up: {vol_up_shortcut}")
            if vol_down_shortcut:
                self._cached_shortcuts["volume-down"] = vol_down_shortcut
                tooltip_parts.append(f"Down: {vol_down_shortcut}")
            
            if len(tooltip_parts) > 1:
                self._volume_slider.set_tooltip_text("\n".join(tooltip_parts))
            else:
                self._volume_slider.set_tooltip_text("Volume")


    def set_connection_status(self, status: str | None) -> None:
        """Set connection status message.
        
        Args:
            status: Status message to display, or None to hide the status.
        """
        if status:
            self._status_label.set_text(status)
            self._status_spinner.start()
            self._status_box.set_visible(True)
        else:
            self._status_spinner.stop()
            self._status_box.set_visible(False)

    def update_device_info(self, info: DeviceInfo | None = None, ip: str | None = None) -> None:
        if info and ip:
            self._title.set_title(GLib.markup_escape_text(f"{info.manufacturer} {info.model}"))
            self._device_subtitle = f"Connected to {ip}"
        else:
            self._title.set_title("Remote")
            self._device_subtitle = None
        self._refresh_subtitle()

    def set_input_route(self, route: str | None) -> None:
        """Show which route buttons take (None = directly over the TV's remote service)."""
        self._input_route = route
        self._refresh_subtitle()

    def _refresh_subtitle(self) -> None:
        device = self._device_subtitle
        route = self._input_route
        if route:
            text = f"{device} · buttons via {route}" if device else f"Buttons via {route}"
        else:
            text = device or "Connect to a device to enable controls"
        self._title.set_subtitle(GLib.markup_escape_text(text))


    def _on_keyboard_focus_enter(self, *_args) -> None:
        """Called when keyboard input area gains focus."""
        self._keyboard_focused = True
        self._keyboard_entry.set_placeholder_text("Type or Ctrl+V to paste (Esc to exit)")
        if self._on_keyboard_focus:
            self._on_keyboard_focus()

    def _on_keyboard_focus_leave(self, *_args) -> None:
        """Called when keyboard input area loses focus."""
        self._keyboard_focused = False
        self._keyboard_entry.set_placeholder_text(f"Press {self._focus_keyboard_shortcut_text} to focus keyboard")
        # Clear the displayed text when leaving focus
        self._keyboard_entry.set_text("")

    def _append_entry_text(self, char: str) -> None:
        """Append a character to the entry text (read-only display)."""
        current = self._keyboard_entry.get_text()
        self._keyboard_entry.set_text(current + char)
        # Move cursor to end
        self._keyboard_entry.set_position(-1)

    def append_text(self, text: str) -> None:
        """Append text to the keyboard entry display (public method for paste)."""
        current = self._keyboard_entry.get_text()
        self._keyboard_entry.set_text(current + text)
        self._keyboard_entry.set_position(-1)

    def _delete_last_char(self) -> None:
        """Delete the last character from entry text."""
        current = self._keyboard_entry.get_text()
        if current:
            self._keyboard_entry.set_text(current[:-1])
            self._keyboard_entry.set_position(-1)

    @property
    def keyboard_focused(self) -> bool:
        """Returns True if the keyboard input area is focused."""
        return self._keyboard_focused

    def focus_keyboard(self) -> None:
        """Focus the keyboard input area."""
        self._keyboard_entry.grab_focus()

    def handle_keyboard_key(self, keyval: int) -> bool:
        """Handle keystrokes in keyboard mode - send them directly to Android TV.
        
        Called by MainWindow when keyboard is focused. Returns True if key was handled.
        """
        # Escape: return focus to the OK button (center of D-pad)
        if keyval == Gdk.KEY_Escape:
            ok_btn = self._keycode_buttons.get("KEYCODE_DPAD_CENTER")
            if ok_btn:
                ok_btn.grab_focus()
            return True
        
        # Arrow keys: send D-pad navigation commands
        arrow_keymap = {
            Gdk.KEY_Up: "KEYCODE_DPAD_UP",
            Gdk.KEY_Down: "KEYCODE_DPAD_DOWN",
            Gdk.KEY_Left: "KEYCODE_DPAD_LEFT",
            Gdk.KEY_Right: "KEYCODE_DPAD_RIGHT",
        }
        if keyval in arrow_keymap:
            if self._on_keyevent:
                self._on_keyevent(arrow_keymap[keyval])
            return True
        
        # Enter: send KEYCODE_ENTER for text confirmation
        if keyval in (Gdk.KEY_Return, Gdk.KEY_KP_Enter):
            if self._on_keyevent:
                self._on_keyevent("KEYCODE_ENTER")
            return True
        
        # Backspace: send KEYCODE_DEL to delete character on TV
        if keyval == Gdk.KEY_BackSpace:
            if self._on_keyevent:
                self._on_keyevent("KEYCODE_DEL")
            # Also delete from displayed text
            self._delete_last_char()
            return True
        
        # Delete key: also send KEYCODE_DEL
        if keyval == Gdk.KEY_Delete:
            if self._on_keyevent:
                self._on_keyevent("KEYCODE_FORWARD_DEL")
            return True
        
        # Tab: send Tab keycode
        if keyval == Gdk.KEY_Tab:
            if self._on_keyevent:
                self._on_keyevent("KEYCODE_TAB")
            return True
        
        # Ignore modifier keys alone
        if keyval in (Gdk.KEY_Shift_L, Gdk.KEY_Shift_R, Gdk.KEY_Control_L, 
                      Gdk.KEY_Control_R, Gdk.KEY_Alt_L, Gdk.KEY_Alt_R,
                      Gdk.KEY_Super_L, Gdk.KEY_Super_R, Gdk.KEY_Caps_Lock):
            return False
        
        # Convert keyval to character and send as text
        char = chr(Gdk.keyval_to_unicode(keyval)) if Gdk.keyval_to_unicode(keyval) else None
        if char and char.isprintable():
            if self._on_text:
                self._on_text(char)
            # Display the character in the entry
            self._append_entry_text(char)
            return True
        
        return False

    def flash_button(self, keycode: str) -> None:
        """Flash the button corresponding to the keycode to show visual feedback.
        
        This is called when a keyboard shortcut triggers a key event,
        so the user can see which button was activated.
        """
        btn = self._keycode_buttons.get(keycode)
        if not btn:
            return
        flash_button(btn)

    def flash_notifications_button(self) -> None:
        """Flash the notifications button to show visual feedback."""
        if self._notifications_button:
            flash_button(self._notifications_button)

    def _add_key_button_with_repeat(self, label: str, keycode: str, col: int, row: int, icon_name: str | list[str] | None = None) -> None:
        """Add a key button that supports hold-to-repeat for mouse input.
        
        This is used for D-pad buttons where holding down the button should
        continuously send key events (e.g., for fast seeking in videos).
        """
        # Create button
        btn = Gtk.Button()
        btn.add_css_class("remote-button")
        btn.set_tooltip_text(label)
        btn.set_hexpand(True)
        btn.set_vexpand(True)
        
        # Create vertical box for content and shortcut
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        
        # Main content (Icon or Label)
        if icon_name:
            if isinstance(icon_name, list):
                icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                icon_box.set_halign(Gtk.Align.CENTER)
                for name in icon_name:
                    image = create_icon(name)
                    icon_box.append(image)
                box.append(icon_box)
            else:
                image = create_icon(icon_name)
                box.append(image)
        else:
            main_label = Gtk.Label(label=label)
            main_label.set_markup(f"<b>{label}</b>")
            box.append(main_label)
        
        # Shortcut label
        shortcut_label = Gtk.Label()
        shortcut_label.add_css_class("caption")
        shortcut_label.set_visible(False)
        shortcut_label.set_max_width_chars(12)
        shortcut_label.set_wrap(True)
        shortcut_label.set_justify(Gtk.Justification.CENTER)
        box.append(shortcut_label)
        
        btn.set_child(box)
        
        # Add GestureClick for press-and-hold detection
        # Use CAPTURE phase so we receive events before the button's internal handler
        gesture = Gtk.GestureClick()
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self._on_repeat_button_pressed, keycode)
        gesture.connect("released", self._on_repeat_button_released)
        gesture.connect("unpaired-release", self._on_repeat_button_unpaired_release)
        # Note: We don't connect 'stopped' as it fires before 'released' and would stop our timer early
        btn.add_controller(gesture)
        
        # Connect clicked signal for single click (this handles keyboard activation)
        btn.connect("clicked", lambda *_: self._on_keyevent and self._on_keyevent(keycode))
        
        self._grid.attach(btn, col, row, 1, 1)
        
        # Register button and shortcut label for updates
        self._keycode_buttons[keycode] = btn
        self._keycode_shortcut_labels[keycode] = shortcut_label

    def _on_repeat_button_pressed(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, keycode: str) -> None:
        """Handle mouse press on a repeat-enabled button.
        
        We don't send a key event here - single clicks are handled by the
        "clicked" signal. We only start a timer for hold-to-repeat.
        """
        # Stop any existing repeat timer
        self._stop_key_repeat_timer()
        
        # Store the keycode and start the repeat timer after initial delay
        # The first key event is sent by the "clicked" signal for single clicks.
        # For held buttons, we start repeating after 500ms delay.
        self._key_repeat_keycode = keycode
        self._key_repeat_timer_id = GLib.timeout_add(500, self._on_key_repeat_start)
    
    def _on_repeat_button_released(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        """Handle mouse release on a repeat-enabled button."""
        self._stop_key_repeat_timer()
    
    def _on_repeat_button_unpaired_release(self, gesture: Gtk.GestureClick, x: float, y: float, button: int, sequence) -> None:
        """Handle unpaired release (e.g., when pointer left button while pressed)."""
        self._stop_key_repeat_timer()
    
    def _on_key_repeat_start(self) -> bool:
        """Called after the initial delay to start fast key repeat."""
        # Start fast repeat (every 50ms, ~20 keys/sec for smooth seeking)
        self._key_repeat_timer_id = GLib.timeout_add(50, self._on_key_repeat_tick)
        return False  # Don't repeat the initial delay timer
    
    def _on_key_repeat_tick(self) -> bool:
        """Called on each repeat interval to send the key event."""
        if self._key_repeat_keycode and self._on_keyevent:
            self._on_keyevent(self._key_repeat_keycode)
            return True  # Continue repeating
        return False  # Stop if no keycode (shouldn't happen)
    
    def _stop_key_repeat_timer(self) -> None:
        """Stop the key repeat timer and clear state."""
        if self._key_repeat_timer_id is not None:
            GLib.source_remove(self._key_repeat_timer_id)
            self._key_repeat_timer_id = None
        self._key_repeat_keycode = None

    def _add_key_button_with_long_press(self, label: str, keycode: str, col: int, row: int, icon_name: str | list[str] | None = None) -> None:
        """Add a key button that supports long-press detection.
        
        This is used for the OK/Enter button where a long-press (500ms+)
        triggers a different action (context menu on Android TV).
        Short presses send the normal keycode.
        """
        # Create button
        btn = Gtk.Button()
        btn.add_css_class("remote-button")
        btn.set_tooltip_text(label)
        btn.set_hexpand(True)
        btn.set_vexpand(True)
        
        # Create vertical box for content and shortcut
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        
        # Main content (Icon or Label)
        if icon_name:
            if isinstance(icon_name, list):
                icon_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=4)
                icon_box.set_halign(Gtk.Align.CENTER)
                for name in icon_name:
                    image = create_icon(name)
                    icon_box.append(image)
                box.append(icon_box)
            else:
                image = create_icon(icon_name)
                box.append(image)
        else:
            main_label = Gtk.Label(label=label)
            main_label.set_markup(f"<b>{label}</b>")
            box.append(main_label)
        
        # Shortcut label
        shortcut_label = Gtk.Label()
        shortcut_label.add_css_class("caption")
        shortcut_label.set_visible(False)
        shortcut_label.set_max_width_chars(12)
        shortcut_label.set_wrap(True)
        shortcut_label.set_justify(Gtk.Justification.CENTER)
        box.append(shortcut_label)
        
        btn.set_child(box)
        
        # Add GestureClick for long-press detection
        gesture = Gtk.GestureClick()
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self._on_long_press_button_pressed, keycode)
        gesture.connect("released", self._on_long_press_button_released, keycode)
        gesture.connect("unpaired-release", self._on_long_press_button_unpaired_release)
        btn.add_controller(gesture)
        
        self._grid.attach(btn, col, row, 1, 1)
        
        # Register button and shortcut label for updates
        self._keycode_buttons[keycode] = btn
        self._keycode_shortcut_labels[keycode] = shortcut_label

    def _on_long_press_button_pressed(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, keycode: str) -> None:
        """Handle mouse press on a long-press-enabled button.
        
        Starts a timer for long-press detection (500ms).
        """
        # Stop any existing long-press timer
        self._stop_long_press_timer()
        
        # Reset state
        self._long_press_keycode = keycode
        self._long_press_triggered = False
        
        # Start timer - if it fires, it's a long-press
        self._long_press_timer_id = GLib.timeout_add(500, self._on_long_press_timer_fired)
    
    def _on_long_press_button_released(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float, keycode: str) -> None:
        """Handle mouse release on a long-press-enabled button.
        
        If long-press was triggered, do nothing (long-press callback was already called).
        If not triggered, it's a normal short press - send the keycode.
        """
        if not self._long_press_triggered:
            # Normal short press - send keycode
            if self._on_keyevent:
                self._on_keyevent(keycode)
        
        # Clean up
        self._stop_long_press_timer()
    
    def _on_long_press_button_unpaired_release(self, gesture: Gtk.GestureClick, x: float, y: float, button: int, sequence) -> None:
        """Handle unpaired release (e.g., when pointer left button while pressed)."""
        self._stop_long_press_timer()
    
    def _on_long_press_timer_fired(self) -> bool:
        """Called when long-press timer fires (500ms held).
        
        This means the button was held long enough - trigger long-press action.
        """
        self._long_press_triggered = True
        
        if self._long_press_keycode and self._on_long_press:
            self._on_long_press(self._long_press_keycode)
        
        self._long_press_timer_id = None
        return False  # Don't repeat timer
    
    def _stop_long_press_timer(self) -> None:
        """Stop the long-press timer and clear state."""
        if self._long_press_timer_id is not None:
            GLib.source_remove(self._long_press_timer_id)
            self._long_press_timer_id = None
        self._long_press_keycode = None
        self._long_press_triggered = False


    def _add_search_button(self, col: int, row: int, icon_name: str | None = None) -> None:
        """Add Search button that sends text 's' for YouTube search."""
        # Create button
        btn = Gtk.Button()
        btn.add_css_class("remote-button")
        btn.set_hexpand(True)
        btn.set_vexpand(True)
        btn.set_tooltip_text("Find (YouTube)")
        btn.connect("clicked", lambda *_: self._on_text and self._on_text("s"))
        
        # Create vertical box for label and shortcut
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        
        # Main content
        if icon_name:
            image = create_icon(icon_name)
            box.append(image)
        else:
            main_label = Gtk.Label(label="Find (YouTube)")
            box.append(main_label)
        
        # Shortcut label (bold, same as Enter button)
        shortcut_label = Gtk.Label()
        shortcut_label.add_css_class("caption")
        shortcut_label.set_visible(False)  # Will be shown when shortcuts are loaded
        # Limit width and enable wrapping for long shortcuts
        shortcut_label.set_max_width_chars(12)
        shortcut_label.set_wrap(True)
        shortcut_label.set_justify(Gtk.Justification.CENTER)
        box.append(shortcut_label)
        
        # Set box as button child
        btn.set_child(box)
        
        # Attach to grid, single column width
        self._grid.attach(btn, col, row, 1, 1)
        
        # Store references
        self._search_button = btn
        self._search_shortcut_label = shortcut_label

    def _add_notifications_button(self, col: int, row: int, icon_name: str | None = None) -> None:
        """Add Notifications button that expands the notification panel."""
        self._on_notifications = None  # Callback to be set by MainWindow
        
        # Create button
        btn = Gtk.Button()
        btn.add_css_class("remote-button")
        btn.set_hexpand(True)
        btn.set_vexpand(True)
        btn.set_tooltip_text("Notifications")
        btn.connect("clicked", lambda *_: self._on_notifications and self._on_notifications())
        
        # Create vertical box for icon and shortcut
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        
        # Main content (icon)
        if icon_name:
            image = create_icon(icon_name)
            box.append(image)
        
        # Shortcut label (hidden by default, same style as other buttons)
        shortcut_label = Gtk.Label()
        shortcut_label.add_css_class("caption")
        shortcut_label.set_visible(False)  # Will be shown when shortcuts are loaded
        shortcut_label.set_max_width_chars(12)
        shortcut_label.set_wrap(True)
        shortcut_label.set_justify(Gtk.Justification.CENTER)
        box.append(shortcut_label)
        
        # Set box as button child
        btn.set_child(box)
        
        # Attach to grid
        self._grid.attach(btn, col, row, 1, 1)
        
        # Store references
        self._notifications_button = btn
        self._notifications_shortcut_label = shortcut_label

    def _add_action_button(self, label: str, col: int, row: int, icon_name: str | None = None, callback=None) -> Gtk.Button:
        """Add a button that calls a callback when clicked (not a keycode).
        
        Args:
            label: Button tooltip/label.
            col: Grid column.
            row: Grid row.
            icon_name: Optional icon name.
            callback: Function to call when clicked.
            
        Returns:
            The created button.
        """
        btn = Gtk.Button()
        btn.add_css_class("remote-button")
        btn.set_hexpand(True)
        btn.set_vexpand(True)
        btn.set_tooltip_text(label)
        if callback:
            btn.connect("clicked", lambda *_: callback())
        
        # Create vertical box for icon and label
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.set_valign(Gtk.Align.CENTER)
        box.set_halign(Gtk.Align.CENTER)
        
        if icon_name:
            image = create_icon(icon_name)
            box.append(image)
        
        # Add text label below icon
        text_label = Gtk.Label(label=label)
        text_label.add_css_class("caption")
        box.append(text_label)
        
        btn.set_child(box)
        self._grid.attach(btn, col, row, 1, 1)
        
        return btn

    def _create_media_controls_section(self) -> None:
        """Create the media controls section with now playing bar and playback/volume controls."""
        # Main container for media controls section
        section_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        section_box.add_css_class("media-controls-section")
        
        # First: Now Playing widget (shows current media)
        self._now_playing_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        self._now_playing_box.add_css_class("now-playing-box")
        self._now_playing_box.set_hexpand(True)
        
        # Album art placeholder icon
        icon = Gtk.Image.new_from_icon_name("audio-x-generic-symbolic")
        icon.set_pixel_size(32)
        icon.add_css_class("now-playing-icon")
        self._now_playing_box.append(icon)
        
        # Text container
        text_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        text_box.set_valign(Gtk.Align.CENTER)
        text_box.set_hexpand(True)
        
        self._now_playing_title = Gtk.Label(label="")
        self._now_playing_title.add_css_class("now-playing-title")
        self._now_playing_title.set_halign(Gtk.Align.START)
        self._now_playing_title.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self._now_playing_title.set_hexpand(True)
        self._now_playing_title.set_xalign(0)
        text_box.append(self._now_playing_title)
        
        self._now_playing_artist = Gtk.Label(label="")
        self._now_playing_artist.add_css_class("now-playing-artist")
        self._now_playing_artist.set_halign(Gtk.Align.START)
        self._now_playing_artist.set_ellipsize(3)  # Pango.EllipsizeMode.END
        self._now_playing_artist.set_hexpand(True)
        self._now_playing_artist.set_xalign(0)
        text_box.append(self._now_playing_artist)
        
        self._now_playing_box.append(text_box)
        self._now_playing_box.set_visible(False)  # Hidden until media is playing
        
        section_box.append(self._now_playing_box)
        
        # Second: Player controls row (Prev, Play/Pause, Next) + Volume slider + Mute button
        controls_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        controls_row.add_css_class("volume-slider-box")
        controls_row.set_hexpand(True)
        
        # Left side: Prev, Play/Pause, Next buttons
        prev_btn = self._create_media_button("Prev", "KEYCODE_MEDIA_PREVIOUS", "media-skip-backward-symbolic")
        controls_row.append(prev_btn)
        
        # Play/Pause button - starts with play icon, will be updated based on playback status
        play_btn = self._create_media_button("Play/Pause", "KEYCODE_MEDIA_PLAY_PAUSE", "media-playback-start-symbolic")
        self._play_pause_button = play_btn
        # Store reference to the icon for dynamic updates
        self._play_pause_icon = play_btn.get_child()
        controls_row.append(play_btn)
        
        next_btn = self._create_media_button("Next", "KEYCODE_MEDIA_NEXT", "media-skip-forward-symbolic")
        controls_row.append(next_btn)
        
        # Center: Volume slider
        # Start with middle value (7/15) to avoid showing 0 before real value is fetched
        adjustment = Gtk.Adjustment(value=7, lower=0, upper=15, step_increment=1, page_increment=1)
        slider = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=adjustment)
        slider.set_sensitive(False)  # Disable until real volume is fetched
        slider.set_hexpand(True)
        slider.set_draw_value(False)
        slider.set_tooltip_text("Volume")
        slider.connect("value-changed", self._on_slider_changed)
        
        # Add click gesture to allow clicking directly on the slider track
        click_gesture = Gtk.GestureClick()
        click_gesture.connect("released", self._on_slider_clicked)
        slider.add_controller(click_gesture)
        
        controls_row.append(slider)
        self._volume_slider = slider
        
        # Right side: Mute button
        mute_btn = Gtk.Button()
        mute_btn.add_css_class("volume-mute-button")
        mute_btn.set_tooltip_text("Mute")
        mute_btn.connect("clicked", self._on_mute_clicked)
        
        mute_icon = Gtk.Image.new_from_icon_name("audio-volume-high-symbolic")
        mute_icon.set_pixel_size(24)
        mute_btn.set_child(mute_icon)
        mute_btn.set_sensitive(False)  # Disable until real volume is fetched
        controls_row.append(mute_btn)
        self._mute_button = mute_btn
        self._keycode_buttons["KEYCODE_VOLUME_MUTE"] = mute_btn
        
        section_box.append(controls_row)
        
        # Add section to main panel (at bottom)
        self.append(section_box)

    def _create_media_button(self, label: str, keycode: str, icon_name: str) -> Gtk.Button:
        """Create a media control button with long-press support.
        
        Args:
            label: Button tooltip text.
            keycode: Android keycode to send when clicked.
            icon_name: Icon name to display (single icon, not a list).
        
        Returns:
            The created button.
        """
        btn = Gtk.Button()
        btn.set_tooltip_text(label)
        
        image = create_icon(icon_name)
        btn.set_child(image)
        
        # Add GestureClick for long-press detection (same as other buttons)
        gesture = Gtk.GestureClick()
        gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        gesture.connect("pressed", self._on_long_press_button_pressed, keycode)
        gesture.connect("released", self._on_long_press_button_released, keycode)
        gesture.connect("unpaired-release", self._on_long_press_button_unpaired_release)
        btn.add_controller(gesture)
        
        # Register button for flash feedback
        self._keycode_buttons[keycode] = btn
        
        return btn

    def _on_slider_changed(self, slider: Gtk.Scale) -> None:
        """Called when slider value changes."""
        if self._updating_slider:
            return
        
        new_value = int(slider.get_value())
        
        # Unmute when volume is adjusted
        if self._is_muted:
            self._is_muted = False
            self._update_mute_button_icon()
        
        # Notify callback
        if self._on_volume_change:
            self._on_volume_change(new_value)

    def _on_slider_clicked(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        """Handle click on slider to set value directly at click position."""
        if not self._volume_slider:
            return
        
        # Get slider dimensions
        slider_width = self._volume_slider.get_width()
        if slider_width <= 0:
            return
        
        # Calculate the relative position (0.0 to 1.0)
        # Account for some padding on the slider ends
        padding = 12  # Approximate padding for the slider thumb area
        effective_width = slider_width - (padding * 2)
        if effective_width <= 0:
            effective_width = slider_width
        
        # Clamp x to valid range
        adjusted_x = max(0, min(x - padding, effective_width))
        ratio = adjusted_x / effective_width
        
        # Calculate the new value based on the slider's range
        adjustment = self._volume_slider.get_adjustment()
        lower = adjustment.get_lower()
        upper = adjustment.get_upper()
        new_value = lower + (ratio * (upper - lower))
        
        # Round to nearest integer
        new_value = round(new_value)
        
        # Clamp to valid range
        new_value = max(int(lower), min(int(upper), int(new_value)))
        
        # Set the slider value (this will trigger _on_slider_changed)
        self._volume_slider.set_value(new_value)

    def update_volume(self, current: int, max_vol: int, is_muted: bool = False) -> None:
        """Update volume slider from device state.
        
        Args:
            current: Current volume level (0 to max_vol).
            max_vol: Maximum volume level.
            is_muted: Whether the audio is muted.
        """
        self._volume_max = max_vol
        self._is_muted = is_muted
        self._volume_initialized = True
        
        if self._volume_slider:
            self._updating_slider = True
            adjustment = self._volume_slider.get_adjustment()
            adjustment.set_upper(max_vol)
            adjustment.set_value(current)
            self._volume_slider.set_sensitive(True)  # Enable slider after fetching real volume
            self._updating_slider = False
        
        # Enable mute button after volume is fetched
        if self._mute_button:
            self._mute_button.set_sensitive(True)
        
        # Update mute button icon based on muted state
        self._update_mute_button_icon()

    def _update_mute_button_icon(self) -> None:
        """Update the mute button icon based on current muted state."""
        if self._mute_button:
            child = self._mute_button.get_child()
            if isinstance(child, Gtk.Image):
                if self._is_muted:
                    child.set_from_icon_name("audio-volume-muted-symbolic")
                    self._mute_button.add_css_class("destructive-action")
                else:
                    child.set_from_icon_name("audio-volume-high-symbolic")
                    self._mute_button.remove_css_class("destructive-action")

    def toggle_mute(self) -> None:
        """Toggle the mute state and update UI."""
        self._is_muted = not self._is_muted
        self._update_mute_button_icon()

    def _on_mute_clicked(self, *_args) -> None:
        """Handle mute button click - delegate to keyevent handler."""
        if self._on_keyevent:
            self._on_keyevent("KEYCODE_VOLUME_MUTE")

    def set_input_button_sensitive(self, sensitive: bool) -> None:
        """Enable or disable the Input button.
        
        This is used to disable the button while the TV connection is being established.
        """
        btn = self._keycode_buttons.get("KEYCODE_TV_INPUT")
        if btn:
            btn.set_sensitive(sensitive)

    def set_input_button_tooltip(self, tooltip: str) -> None:
        """Set custom tooltip for the Input button.
        
        Args:
            tooltip: The tooltip text to display.
        """
        btn = self._keycode_buttons.get("KEYCODE_TV_INPUT")
        if btn:
            shortcut = self._cached_shortcuts.get("tv-input")
            if shortcut:
                tooltip += f": {shortcut}"
            btn.set_tooltip_text(tooltip)

    def update_now_playing(
        self,
        title: str | None = None,
        artist: str | None = None,
        playback_status: str = "Stopped",
    ) -> None:
        """Update the Now Playing widget.
        
        Args:
            title: Track/video title.
            artist: Artist/channel name.
            playback_status: One of "Playing", "Paused", "Stopped".
        """
        if not title or playback_status == "Stopped":
            self._now_playing_box.set_visible(False)
            return
        
        # Update title
        self._now_playing_title.set_label(title or "")
        
        # Update artist
        if artist:
            self._now_playing_artist.set_label(artist)
            self._now_playing_artist.set_visible(True)
        else:
            self._now_playing_artist.set_visible(False)
        
        # Update Play/Pause button icon based on playback status
        if playback_status == "Playing":
            # Update Play/Pause button to show Pause icon when playing
            if self._play_pause_icon and isinstance(self._play_pause_icon, Gtk.Image):
                self._play_pause_icon.set_from_icon_name("media-playback-pause-symbolic")
        elif playback_status == "Paused":
            # Update Play/Pause button to show Play icon when paused
            if self._play_pause_icon and isinstance(self._play_pause_icon, Gtk.Image):
                self._play_pause_icon.set_from_icon_name("media-playback-start-symbolic")
        else:
            # When stopped, show Play icon
            if self._play_pause_icon and isinstance(self._play_pause_icon, Gtk.Image):
                self._play_pause_icon.set_from_icon_name("media-playback-start-symbolic")
        
        # Set tooltip with full info (shown on hover)
        tooltip = title
        if artist:
            tooltip += f"\n{artist}"
        self._now_playing_box.set_tooltip_text(tooltip)
        
        self._now_playing_box.set_visible(True)
