"""
Main Application Window.

This module contains the MainWindow class which serves as the primary window
for the TV Remote application. It coordinates device connections and remote
control input over the Android TV Remote protocol v2 (the protocol used by the
Google TV phone app).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Callable

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, Gio, GLib, Gtk, Pango  # noqa: E402

from ..core.remote_v2 import (  # noqa: E402
    REMOTE_PORT,
    AndroidTvRemote,
    DeviceInfo,
    NotPairedError,
    RemoteError,
    VolumeInfo,
    probe_paired_device,
)
from ..core.home_assistant import HomeAssistantError, HomeAssistantRemote, load_token  # noqa: E402
from ..core.scanner import SubnetScanner, HostFound  # noqa: E402
from ..core.network_info import get_ipv4_interface_networks  # noqa: E402
from .device_dialog import DeviceDialog  # noqa: E402
from .pairing_dialog import run_pairing  # noqa: E402
from .preferences_dialog import (  # noqa: E402
    PreferencesDialog,
    load_shortcuts_from_settings,
    get_focus_keyboard_keys,
    get_search_keys,
    get_notifications_keys,
    get_toggle_home_assistant_keys,
)
from .remote_panel import RemotePanel  # noqa: E402
from .info_dialog import InfoDialog  # noqa: E402
from .tv_remote_dialog import TvRemoteDialog  # noqa: E402
from .airplay_dialog import AirPlayDialog, PlaybackSummary, _format_time  # noqa: E402
from ..core.mpris_service import MprisService  # noqa: E402

logger = logging.getLogger(__name__)

# Either input route: both offer send_keycode/send_long_press/send_text/...
InputBackend = AndroidTvRemote | HomeAssistantRemote



class MainWindow(Adw.ApplicationWindow):
    """Main application window for TV Remote.

    Handles:
    - Device connection lifecycle (pairing and the remote control link)
    - Keyboard shortcut processing
    - Coordination between UI components (RemotePanel, DeviceDialog, PreferencesDialog)
    - Window state persistence
    """

    def __init__(self, application: Adw.Application) -> None:
        super().__init__(application=application, title="TV Remote")

        self._connected_ip: str | None = None
        self._remote: AndroidTvRemote | None = None
        # Secondary connection for TV Input Routing
        self._tv_remote: AndroidTvRemote | None = None
        self._connect_thread: threading.Thread | None = None
        self._connect_silent: bool = False
        self._device_dialog: DeviceDialog | None = None
        self._preferences_dialog: PreferencesDialog | None = None
        self._info_dialog: InfoDialog | None = None
        self._tv_remote_dialog: TvRemoteDialog | None = None
        self._airplay_dialog: AirPlayDialog | None = None
        self._tv_device_info: DeviceInfo | None = None
        self._tv_remote_required: bool = False  # True when the TV connection is needed but not yet up
        self._ip_discovery_in_progress: bool = False  # Flag to prevent reconnect loop during IP discovery
        self._tv_discovery_attempted: bool = False  # Flag to prevent retrying TV discovery infinitely
        # Home Assistant route: kept warm so switching to it is instant
        self._ha: HomeAssistantRemote | None = None
        self._ha_mode: bool = False
        self._last_ha_error_time: float = 0.0

        # Initialize MPRIS service for desktop media control integration
        self._mpris = MprisService(
            on_play_pause=self._on_mpris_play_pause,
            on_play=self._on_mpris_play,
            on_pause=self._on_mpris_pause,
            on_stop=self._on_mpris_stop,
            on_next=self._on_mpris_next,
            on_previous=self._on_mpris_previous,
            on_raise=self._on_mpris_raise,
            on_quit=self._on_mpris_quit,
        )
        self._mpris.start()

        # Initialize GSettings
        self._settings = Gio.Settings.new("io.github.tecix.TvRemote")

        # Load window size
        width = self._settings.get_int("window-width")
        height = self._settings.get_int("window-height")
        self.set_default_size(width, height)

        # Set minimum window size to ensure all controls remain visible
        self.set_size_request(350, 680)
        if self._settings.get_boolean("window-is-maximized"):
            self.maximize()

        self.connect("close-request", self._on_close_request)

        # Load keyboard shortcuts from settings
        self._key_map: dict[int, str] = {}
        self._focus_keyboard_keys: list[int] = []
        self._search_keys: list[int] = []
        self.reload_shortcuts()

        self._build_ui()
        self._create_actions()
        self._remote_panel.set_handlers(
            on_keyevent=self._on_remote_keyevent,
            on_text=self._on_remote_text,
            on_volume_change=self._on_volume_change,
            on_notifications=self._on_notifications,
            on_long_press=self._on_remote_long_press,
            on_keyboard_focus=self._on_keyboard_focus,
        )

        # Track current volume for slider changes
        self._current_volume: int = 0
        self._last_volume_change_time: float = 0.0

        # Keyboard long-press state (for shortcuts)
        self._kb_long_press_timer_id: int | None = None
        self._kb_long_press_keyval: int | None = None
        self._kb_long_press_keycode: str | None = None
        self._kb_long_press_triggered: bool = False

        # Home Assistant route (restores the last used route)
        self.reload_home_assistant()
        self._set_ha_mode(self._settings.get_boolean("home-assistant-mode"), announce=False)

        self._remote_panel.update_tooltips(self._settings)
        # Update Power button tooltip
        self.reload_shortcuts()

        # Load last connected IP and auto-connect
        self._auto_connect_last_ip()

        # Listen for TV IP setting changes
        self._settings.connect("changed::tv-ip", self._on_tv_ip_setting_changed)

    def _on_close_request(self, *_args) -> bool:
        """Save window state before closing."""
        is_maximized = self.is_maximized()

        if not is_maximized:
            width = self.get_width()
            height = self.get_height()
            self._settings.set_int("window-width", width)
            self._settings.set_int("window-height", height)

        self._settings.set_boolean("window-is-maximized", is_maximized)

        # Stop the MPRIS service
        self._mpris.stop()
        if self._airplay_dialog:
            self._airplay_dialog.shutdown()

        return False  # allow closing

    def reload_shortcuts(self) -> None:
        """Reload keyboard shortcuts from settings."""
        self._key_map = load_shortcuts_from_settings(self._settings)
        self._focus_keyboard_keys = get_focus_keyboard_keys(self._settings)
        self._search_keys = get_search_keys(self._settings)
        self._notifications_keys = get_notifications_keys(self._settings)
        self._toggle_ha_keys = get_toggle_home_assistant_keys(self._settings)
        # Update button tooltips
        if hasattr(self, "_remote_panel"):
            self._remote_panel.update_tooltips(self._settings)
        if hasattr(self, "_ha_button"):
            self._update_ha_button_tooltip()
        # Update Power button tooltip
        if hasattr(self, "_power_button"):
            from .preferences_dialog import get_action_tooltip
            power_tooltip = get_action_tooltip("power", self._settings)
            if power_tooltip:
                self._power_button.set_tooltip_text(f"Power ({power_tooltip})")
            else:
                self._power_button.set_tooltip_text("Power")

    def _build_ui(self) -> None:
        self._toast_overlay = Adw.ToastOverlay()
        self.set_content(self._toast_overlay)

        toolbar_view = Adw.ToolbarView()
        self._toast_overlay.set_child(toolbar_view)

        header = Adw.HeaderBar()
        header.set_show_end_title_buttons(True)
        toolbar_view.add_top_bar(header)

        # Devices button in top bar
        devices_btn = Gtk.Button(icon_name="video-display-symbolic")
        devices_btn.set_tooltip_text("Manage devices")
        devices_btn.connect("clicked", self._on_devices_clicked)
        header.pack_start(devices_btn)

        # Info button in top bar
        info_btn = Gtk.Button(icon_name="help-about-symbolic")
        info_btn.set_tooltip_text("Instructions")
        info_btn.connect("clicked", self._on_info_clicked)
        header.pack_start(info_btn)

        # Power button in header bar with long-press support
        self._power_button = Gtk.Button(icon_name="system-shutdown-symbolic")
        self._power_button.add_css_class("power-button")

        # Add long-press gesture for power menu
        power_gesture = Gtk.GestureClick()
        power_gesture.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        power_gesture.connect("pressed", self._on_power_button_pressed)
        power_gesture.connect("released", self._on_power_button_released)
        power_gesture.connect("unpaired-release", self._on_power_button_unpaired_release)
        self._power_button.add_controller(power_gesture)

        # Long-press state for power button
        self._power_long_press_timer_id: int | None = None
        self._power_long_press_triggered: bool = False

        header.pack_start(self._power_button)

        # Route switch: Direct (Remote protocol) <-> Home Assistant
        self._ha_button = Gtk.ToggleButton(label="HA")
        self._ha_button.add_css_class("ha-toggle")
        self._ha_button.connect("toggled", self._on_ha_button_toggled)
        header.pack_start(self._ha_button)

        # Apply CSS for power button hover effect (red on hover) and the HA switch
        css_provider = Gtk.CssProvider()
        css_provider.load_from_string("""
            button.power-button:hover {
                background-color: #e74c3c;
                color: white;
            }
            button.ha-toggle {
                font-weight: bold;
                transition: background-color 200ms ease-out, color 200ms ease-out;
            }
            button.ha-toggle:checked {
                background-color: #03a9f4;
                color: white;
            }
        """)
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            css_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )

        # Preferences button
        prefs_btn = Gtk.Button(icon_name="preferences-system-symbolic")
        prefs_btn.set_tooltip_text("Configure Shortcuts")
        prefs_btn.connect("clicked", self._on_preferences_clicked)
        header.pack_end(prefs_btn)

        # AirPlay: send a video link (and subtitles) to the TV
        airplay_btn = Gtk.Button(icon_name="send-to-symbolic")
        airplay_btn.set_tooltip_text("Play a video link on the TV (AirPlay)")
        airplay_btn.connect("clicked", self._on_airplay_clicked)
        header.pack_end(airplay_btn)

        # A compact cast controller remains visible when the AirPlay dialog closes.
        self._build_airplay_bar(toolbar_view)

        # Content (remote)
        self._remote_panel = RemotePanel()
        remote_scroll = Gtk.ScrolledWindow()
        remote_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        remote_scroll.set_child(self._remote_panel)
        toolbar_view.set_content(remote_scroll)

        # Keyboard shortcuts controller
        # Use CAPTURE phase to catch keys before they're consumed by focused widgets
        key_controller = Gtk.EventControllerKey()
        key_controller.set_propagation_phase(Gtk.PropagationPhase.CAPTURE)
        key_controller.connect("key-pressed", self._on_key_pressed)
        key_controller.connect("key-released", self._on_key_released)
        self.add_controller(key_controller)

        self._set_connected(False)

    def _build_airplay_bar(self, toolbar_view: Adw.ToolbarView) -> None:
        self._airplay_revealer = Gtk.Revealer()
        self._airplay_revealer.set_transition_type(Gtk.RevealerTransitionType.SLIDE_DOWN)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.add_css_class("card")
        box.set_margin_top(6)
        box.set_margin_bottom(6)
        box.set_margin_start(8)
        box.set_margin_end(8)
        self._airplay_details = Gtk.Button()
        self._airplay_details.add_css_class("flat")
        self._airplay_details.set_hexpand(True)
        self._airplay_details.update_property(
            [Gtk.AccessibleProperty.LABEL], ["On the TV, playback details"]
        )
        self._airplay_details.connect("clicked", self._on_airplay_clicked)
        details = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        details.append(Gtk.Image(icon_name="video-display-symbolic"))
        labels = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        labels.set_hexpand(True)
        title = Gtk.Label(label="On the TV", xalign=0)
        title.add_css_class("heading")
        title.set_ellipsize(Pango.EllipsizeMode.END)
        title.set_width_chars(1)
        self._airplay_subtitle = Gtk.Label(xalign=0)
        self._airplay_subtitle.add_css_class("caption")
        self._airplay_subtitle.add_css_class("dim-label")
        self._airplay_subtitle.set_ellipsize(Pango.EllipsizeMode.END)
        self._airplay_subtitle.set_width_chars(1)
        labels.append(title)
        labels.append(self._airplay_subtitle)
        details.append(labels)
        self._airplay_details.set_child(details)
        box.append(self._airplay_details)

        def button(icon: str, label: str, action: Callable[[], None]) -> Gtk.Button:
            control = Gtk.Button(icon_name=icon, tooltip_text=label)
            control.add_css_class("flat")
            control.add_css_class("circular")
            control.set_valign(Gtk.Align.CENTER)
            control.update_property([Gtk.AccessibleProperty.LABEL], [label])
            control.connect("clicked", lambda *_: action())
            box.append(control)
            return control

        self._airplay_back = button(
            "media-seek-backward-symbolic", "Back 10 seconds",
            lambda: self._airplay_dialog and self._airplay_dialog._seek_relative(-10),
        )
        self._airplay_play = button(
            "media-playback-pause-symbolic", "Pause",
            lambda: self._airplay_dialog and self._airplay_dialog._on_play_pause_clicked(),
        )
        self._airplay_forward = button(
            "media-seek-forward-symbolic", "Forward 30 seconds",
            lambda: self._airplay_dialog and self._airplay_dialog._seek_relative(30),
        )
        self._airplay_stop = button(
            "media-playback-stop-symbolic", "Stop casting",
            lambda: self._airplay_dialog and self._airplay_dialog._on_stop_clicked(),
        )
        self._airplay_revealer.set_child(box)
        toolbar_view.add_top_bar(self._airplay_revealer)

    def _update_airplay_bar(self, summary: PlaybackSummary) -> None:
        status = summary.status
        active = status.active
        self._airplay_revealer.set_reveal_child(active or summary.preparing)
        if not (active or summary.preparing):
            return
        if summary.error:
            detail = "Receiver unavailable"
        elif summary.preparing:
            detail = summary.detail or "Preparing video…"
        else:
            detail = {"LOADING": "Loading…", "PLAYING": "Playing", "PAUSED": "Paused"}.get(
                status.state, status.state.title()
            )
            if summary.converting:
                detail += " · " + (summary.detail or "Converting for TV…")
            elif status.duration > 0:
                detail += f" · {_format_time(status.position)} / {_format_time(status.duration)}"
        self._airplay_subtitle.set_label(detail)
        self._airplay_details.set_tooltip_text(summary.error or summary.media or "Playback details")
        self._airplay_details.update_property([Gtk.AccessibleProperty.DESCRIPTION], [detail])
        playing = status.state == "PLAYING"
        label = "Pause" if playing else "Play"
        self._airplay_play.set_icon_name(
            "media-playback-pause-symbolic" if playing else "media-playback-start-symbolic"
        )
        self._airplay_play.set_tooltip_text(label)
        self._airplay_play.update_property([Gtk.AccessibleProperty.LABEL], [label])
        self._airplay_play.set_sensitive(
            active and status.state in ("PLAYING", "PAUSED")
            and not summary.preparing and not summary.error
        )
        seekable = (
            active and status.duration > 0 and not summary.preparing
            and not summary.converting and not summary.error
        )
        self._airplay_back.set_sensitive(seekable)
        self._airplay_forward.set_sensitive(seekable)
        self._airplay_stop.set_sensitive(True)

    def _on_devices_clicked(self, *_args) -> None:
        if self._device_dialog is None:
            self._device_dialog = DeviceDialog(self)
        else:
            self._device_dialog.update_last_ip()
        self._device_dialog.present()

    def _on_preferences_clicked(self, *_args) -> None:
        if self._preferences_dialog is None:
            self._preferences_dialog = PreferencesDialog(self)
        self._preferences_dialog.present(self)

    def _on_airplay_clicked(self, *_args) -> None:
        if self._airplay_dialog is None:
            # Empty receiver setting means the connected device (TinyTV runs its receiver there)
            self._airplay_dialog = AirPlayDialog(
                self._settings,
                lambda: self._connected_ip or self._settings.get_string("last-connected-ip") or None,
                on_status=self._update_airplay_bar,
                on_message=self._toast,
            )
        self._airplay_dialog.open(self)

    def _on_info_clicked(self, *_args) -> None:
        if self._info_dialog is None:
            self._info_dialog = InfoDialog(self)
        self._info_dialog.present()


    def _auto_connect_last_ip(self) -> None:
        """Load the last successfully connected IP address from settings and auto-connect.

        If connection fails, triggers network discovery to find paired devices
        that may have changed IP addresses.
        """
        last_ip = self._settings.get_string("last-connected-ip")
        if last_ip:
            # Automatically attempt connection after UI is fully initialized
            GLib.idle_add(lambda: (self._connect_ip_with_discovery_fallback(last_ip), False)[1])

    def _connect_ip_with_discovery_fallback(self, ip: str) -> None:
        """Attempt to connect to IP, falling back to IP discovery on failure.

        This is used for auto-connect on startup. If the saved IP is no longer
        reachable or a different device is at that IP, we scan the network to
        find paired devices that may have changed IP addresses.
        """
        if not ip:
            return

        if self._connect_thread:
            return

        self._connect_silent = True
        self._remote_panel.set_connection_status("Connecting…")
        remote = self._new_remote(ip)

        # Get expected device model from settings
        expected_model = self._settings.get_string("last-connected-device-model") or None

        def worker() -> None:
            try:
                try:
                    device_info = remote.connect()
                except Exception:
                    # Connection failed - start IP discovery in background
                    GLib.idle_add(self._on_auto_connect_failed_start_discovery)
                    return

                # Verify device model matches expected device
                if expected_model and device_info.model != expected_model:
                    logger.info(
                        f"Device model mismatch: expected '{expected_model}', "
                        f"got '{device_info.model}'. Starting discovery."
                    )
                    remote.disconnect()
                    GLib.idle_add(self._on_auto_connect_failed_start_discovery)
                    return

                GLib.idle_add(self._on_connect_success_ui, ip, remote, device_info)
            finally:
                GLib.idle_add(self._on_connect_done_ui)

        self._connect_thread = threading.Thread(target=worker, name="remote-auto-connect", daemon=True)
        self._connect_thread.start()

    def _on_auto_connect_failed_start_discovery(self) -> None:
        """Called when auto-connect fails. Starts network discovery to find paired devices."""
        self._connect_thread = None
        self._connect_silent = False
        self._remote_panel.set_connection_status("Searching for device…")
        logger.info("Auto-connect failed, starting network discovery for paired devices")

        # Start discovery in background thread
        threading.Thread(
            target=self._discover_and_update_device_ips,
            name="ip-discovery",
            daemon=True
        ).start()

    @staticmethod
    def _find_paired_devices() -> list[tuple[HostFound, DeviceInfo]]:
        """Scan the local networks for TVs with the remote port open that already trust us.

        Blocking; run it in a worker thread.
        """
        nets = [n.network for n in get_ipv4_interface_networks(limit_to_slash24_if_broader=True)]
        if not nets:
            return []

        found_hosts: list[HostFound] = []
        SubnetScanner(port=REMOTE_PORT, timeout_s=0.35, concurrency=256).scan(
            nets, on_found=found_hosts.append
        )

        paired: list[tuple[HostFound, DeviceInfo]] = []
        for host in found_hosts:
            info = probe_paired_device(str(host.ip))
            if info:
                paired.append((host, info))
        return paired

    def _discover_and_update_device_ips(self) -> None:
        """Scan network for paired devices and update stored IPs.

        This runs in a background thread. It:
        1. Scans for devices with the remote port (6466) open
        2. Keeps the ones that accept our client certificate (already paired)
        3. Matches device models with stored settings
        4. Updates both last-connected-ip and tv-ip if their devices are found
        5. Attempts to connect to the previously saved device
        """
        # Load stored device info for updating IP-to-model mapping
        stored_devices_json = self._settings.get_string("discovered-devices")
        stored_devices: list[dict] = []
        try:
            if stored_devices_json:
                stored_devices = json.loads(stored_devices_json)
        except Exception:
            pass

        # Get device models directly from settings
        last_ip = self._settings.get_string("last-connected-ip")
        last_device_model = self._settings.get_string("last-connected-device-model") or None

        tv_ip = self._settings.get_string("tv-ip")
        tv_device_model = self._settings.get_string("tv-device-model") or None

        try:
            found = self._find_paired_devices()
        except Exception as e:
            logger.error(f"Network scan failed: {e}")
            GLib.idle_add(self._on_discovery_complete, None, None, "Scan failed")
            return

        paired_devices: list[dict] = []
        new_ip_for_last_device: str | None = None
        new_ip_for_tv_device: str | None = None

        for host, device_info in found:
            ip = str(host.ip)
            paired_devices.append({
                "ip": ip,
                "latency_ms": host.latency_ms,
                "model": device_info.model,
                "device_name": f"{device_info.manufacturer} {device_info.model}",
            })

            # Check if this matches our last connected device
            if last_device_model and device_info.model == last_device_model:
                new_ip_for_last_device = ip
                logger.info(f"Found main device '{last_device_model}' at new IP: {ip}")

            # Check if this matches our TV device
            if tv_device_model and device_info.model == tv_device_model:
                new_ip_for_tv_device = ip
                logger.info(f"Found TV device '{tv_device_model}' at new IP: {ip}")

        # Update stored devices with new IP addresses
        if paired_devices:
            # Build a map of model -> paired device entry
            model_to_device = {pd["model"]: pd for pd in paired_devices}

            # Update stored devices
            updated_devices: list[dict] = []
            for stored_dev in stored_devices:
                pd = model_to_device.get(stored_dev.get("model"))
                if pd:
                    stored_dev["ip"] = pd["ip"]
                    stored_dev["latency_ms"] = pd["latency_ms"]
                updated_devices.append(stored_dev)

            # Also add any new paired devices not in stored list
            stored_models = {d.get("model") for d in stored_devices if d.get("model")}
            for pd in paired_devices:
                if pd["model"] not in stored_models:
                    updated_devices.append(pd)

            # Save updated devices - wrap in function that returns False to prevent GLib loop
            def save_devices():
                self._settings.set_string("discovered-devices", json.dumps(updated_devices))
                return False
            GLib.idle_add(save_devices)

        # Update TV IP if found at new address
        # Set flag to prevent _on_tv_ip_setting_changed from triggering reconnection loop
        if new_ip_for_tv_device and new_ip_for_tv_device != tv_ip:
            def update_tv_ip(new_tv_ip: str) -> None:
                self._ip_discovery_in_progress = True
                self._settings.set_string("tv-ip", new_tv_ip)
                self._ip_discovery_in_progress = False
                return False  # For GLib.idle_add compatibility
            GLib.idle_add(update_tv_ip, new_ip_for_tv_device)
            logger.info(f"Updated tv-ip from {tv_ip} to {new_ip_for_tv_device}")

        # If we found the last connected device at a new IP, update and connect
        if new_ip_for_last_device and new_ip_for_last_device != last_ip:
            GLib.idle_add(self._save_last_device, new_ip_for_last_device)
            GLib.idle_add(self._on_discovery_complete, new_ip_for_last_device, new_ip_for_tv_device, None)
        elif paired_devices:
            # Connect to first available paired device
            first_ip = paired_devices[0]["ip"]
            GLib.idle_add(self._on_discovery_complete, first_ip, new_ip_for_tv_device, None)
        else:
            GLib.idle_add(self._on_discovery_complete, None, new_ip_for_tv_device, "No paired devices found")

    def _on_discovery_complete(self, found_ip: str | None, found_tv_ip: str | None, error_msg: str | None) -> None:
        """Called when IP discovery completes.

        Args:
            found_ip: New IP for main device (last-connected-ip)
            found_tv_ip: New IP for TV device (tv-ip)
            error_msg: Error message if discovery failed
        """
        self._remote_panel.set_connection_status(None)

        if error_msg:
            logger.info(f"IP discovery: {error_msg}")
            return

        if found_tv_ip:
            logger.info(f"IP discovery updated TV device IP to {found_tv_ip}")

        if found_ip:
            logger.info(f"IP discovery found device at {found_ip}, connecting...")
            self._connect_ip(found_ip, silent=False)

    def _save_last_device(self, ip: str, device_info: DeviceInfo | None = None) -> None:
        """Save the successfully connected device info to settings.

        Args:
            ip: The device's IP address.
            device_info: Optional device info to save name and model.
        """
        self._settings.set_string("last-connected-ip", ip)
        if device_info:
            self._settings.set_string("last-connected-device-name", device_info.manufacturer or "")
            self._settings.set_string("last-connected-device-model", device_info.model or "")
        return False  # For GLib.idle_add compatibility

    def _create_actions(self) -> None:
        connect_ip = Gio.SimpleAction.new("connect_ip", GLib.VariantType.new("s"))
        connect_ip.connect("activate", self._on_connect_ip_action)
        self.add_action(connect_ip)

        disconnect = Gio.SimpleAction.new("disconnect", None)
        disconnect.connect("activate", self._on_disconnect)
        self.add_action(disconnect)

    def _toast(self, text: str) -> None:
        self._toast_overlay.add_toast(Adw.Toast(title=text))

    @property
    def is_connected(self) -> bool:
        return bool(self._remote and self._remote.connected)

    def _new_remote(self, ip: str) -> AndroidTvRemote:
        """Create the main connection; its events are forwarded to the main thread."""
        remote = AndroidTvRemote(ip, timeout_s=8.0)
        remote.set_handlers(
            on_disconnect=lambda: GLib.idle_add(self._on_remote_disconnected, remote),
            on_volume=lambda v: GLib.idle_add(self._on_volume_fetched, v),
        )
        return remote

    def _set_connected(self, connected: bool, ip: str | None = None) -> None:
        self._connected_ip = ip if connected else None
        if not connected:
            self._remote_panel.update_device_info(None, None)
            for remote in (self._remote, self._tv_remote):
                if remote:
                    remote.disconnect()
            self._remote = None
            self._tv_remote = None
        self._refresh_controls()

    def _on_connect_ip_action(self, _action: Gio.SimpleAction, parameter: GLib.Variant) -> None:
        """Handler for the connect_ip action."""
        ip = parameter.get_string()
        self._connect_ip(ip, silent=False)

    def _connect_ip(
        self,
        ip: str,
        *,
        silent: bool = False,
        on_done: Callable[[bool], None] | None = None,
        dialog_parent: Gtk.Widget | None = None,
    ) -> None:
        """Connect to the given IP address, pairing first if the TV doesn't know us yet.

        Args:
            ip: The IP address to connect to.
            silent: If True, suppress "Connecting..." notification and never start pairing.
            on_done: Called with the final outcome (connected or not).
            dialog_parent: Widget to show the pairing dialog on (defaults to this window).
        """
        def done(success: bool) -> None:
            if on_done:
                on_done(success)

        if not ip:
            done(False)
            return

        if self._connect_thread:
            if not silent:
                self._toast("Already connecting…")
            done(False)
            return

        # If already connected to another device, disconnect first.
        if self._remote:
            self._set_connected(False)

        if not silent:
            self._toast(f"Connecting to {ip}…")
        self._connect_silent = silent
        self._remote_panel.set_connection_status("Connecting…")
        remote = self._new_remote(ip)

        def worker() -> None:
            try:
                try:
                    device_info = remote.connect()
                except NotPairedError:
                    GLib.idle_add(self._on_not_paired_ui, ip, silent, done, dialog_parent)
                    return
                except Exception as e:
                    GLib.idle_add(self._on_connect_failed_ui, str(e) or "Connection failed", done)
                    return

                GLib.idle_add(self._on_connect_success_ui, ip, remote, device_info, done)
            finally:
                GLib.idle_add(self._on_connect_done_ui)

        self._connect_thread = threading.Thread(target=worker, name="remote-connect", daemon=True)
        self._connect_thread.start()

    def _on_not_paired_ui(
        self,
        ip: str,
        silent: bool,
        done: Callable[[bool], None],
        dialog_parent: Gtk.Widget | None,
    ) -> None:
        """The TV refused our certificate: pair (shows a code on the TV), then connect again."""
        self._remote_panel.set_connection_status(None)
        if silent:
            done(False)
            return

        self._toast("Pairing: a code will appear on the TV…")

        def on_paired(success: bool, error: str | None) -> None:
            if success:
                self._toast("Paired!")
                self._connect_ip(ip, on_done=done, dialog_parent=dialog_parent)
            else:
                if error:
                    self._toast(error)
                done(False)

        run_pairing(dialog_parent or self, ip, on_paired)

    def _on_connect_success_ui(
        self,
        ip: str,
        remote: AndroidTvRemote,
        device_info: DeviceInfo,
        done: Callable[[bool], None] | None = None,
    ) -> None:
        if not remote.connected:
            # Dropped between the handshake and now; its disconnect event was ignored.
            self._on_connect_failed_ui("Connection lost", done)
            return
        self._remote = remote
        self._set_connected(True, ip=ip)
        self._remote_panel.update_device_info(device_info, ip)
        self._remote_panel.set_connection_status(None)  # Hide status on success
        self._save_last_device(ip, device_info)
        if not self._connect_silent:
            self._toast(f"Successfully connected to {ip}!")
        self._connect_silent = False
        logger.info(f"Remote connected to {ip} ({device_info.manufacturer} {device_info.model})")

        self._mpris.set_device_connected(True, device_info.model or "Android TV")

        # Start the secondary connection for TV Input Routing if configured
        # Skip if TV IP is the same as connected device (no separate routing needed)
        tv_ip = self._settings.get_string("tv-ip")
        if tv_ip and tv_ip.strip() and tv_ip.strip() != self._connected_ip:
            # Disable Input button until the TV connects
            self._tv_remote_required = True
            self._tv_discovery_attempted = False  # Reset discovery flag for fresh connection attempt
            self._remote_panel.set_input_button_sensitive(False)
            self._start_tv_remote_async(tv_ip.strip())
        else:
            self._tv_remote_required = False

        if done:
            done(True)

    def _on_remote_disconnected(self, remote: AndroidTvRemote) -> None:
        """Called when the main connection drops unexpectedly."""
        if remote is self._remote:
            self._handle_connection_drop()

    def _on_connect_failed_ui(self, msg: str, done: Callable[[bool], None] | None = None) -> None:
        self._set_connected(False)
        self._remote_panel.set_connection_status(None)  # Hide status on failure
        self._toast(msg)
        if done:
            done(False)

    def _on_connect_done_ui(self) -> None:
        self._connect_thread = None

    def _on_disconnect(self, *_args) -> None:
        if self._connect_thread:
            self._toast("Still connecting…")
            return
        self._set_connected(False)
        # Update MPRIS to reflect disconnection
        self._mpris.set_device_connected(False)
        self._toast("Disconnected from device.")

    def _on_key_pressed(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType) -> bool:
        """Handle global keyboard shortcuts with long-press support.

        For most shortcuts (except D-pad directions), we start a timer on key-press.
        If the key is released before 500ms, it's a normal short press.
        If held for 500ms+, it triggers a long-press action.
        """
        # Prepare lower-case keyval for fallback (handling Caps Lock)
        lower_keyval = Gdk.keyval_to_lower(keyval)

        # Route switch works whenever no text is being typed
        if not self._remote_panel.keyboard_focused and (
            keyval in self._toggle_ha_keys or lower_keyval in self._toggle_ha_keys
        ):
            focus = self.get_focus()
            if not (focus and isinstance(focus, (Gtk.Editable, Gtk.Entry))):
                self._ha_button.set_active(not self._ha_button.get_active())
                return True

        # Ignore keyboard shortcuts if there is no route to the TV
        if not self._input():
            return False

        # If keyboard input mode is active, handle keys here (before Entry can consume them)
        if self._remote_panel.keyboard_focused:
            # Handle Ctrl+V for clipboard paste
            # We check for CONTROL_MASK and the 'v' key (both lower and upper case)
            if (_state & Gdk.ModifierType.CONTROL_MASK) and keyval in (Gdk.KEY_v, Gdk.KEY_V):
                self._paste_clipboard()
                return True
            return self._remote_panel.handle_keyboard_key(keyval)

        # If an entry/editable is focused (e.g., in DeviceDialog), don't intercept keys
        focus = self.get_focus()
        if focus and isinstance(focus, (Gtk.Editable, Gtk.Entry)):
            return False

        # Handle focus keyboard shortcut (configurable) - no long-press needed
        if keyval in self._focus_keyboard_keys or lower_keyval in self._focus_keyboard_keys:
            self._remote_panel.focus_keyboard()
            return True

        # Handle search shortcut (sends text "s" for YouTube search, then activates keyboard) - no long-press
        if keyval in self._search_keys or lower_keyval in self._search_keys:
            self._on_remote_text("s")
            self._remote_panel.focus_keyboard()
            return True

        # Handle notifications shortcut (opens notification panel) - no long-press needed
        if keyval in self._notifications_keys or lower_keyval in self._notifications_keys:
            self._remote_panel.flash_notifications_button()
            self._on_notifications()
            return True

        # Handle TV Input shortcut (T key) - uses long-press support
        if keyval in (Gdk.KEY_t, Gdk.KEY_T):
            if self._tv_remote_required:
                return True  # Ignore keypress while the TV connection is being set up
            # Start long-press timer for TV Input
            self._start_kb_long_press_timer(keyval, "KEYCODE_TV_INPUT")
            return True

        # Handle keyboard shortcuts
        # Check original keyval first, then fallback to lower-case (ignores Caps Lock)
        keycode = self._key_map.get(keyval) or self._key_map.get(lower_keyval)

        if keycode:
            # D-pad directions: send immediately (they have their own repeat behavior)
            if keycode in ("KEYCODE_DPAD_UP", "KEYCODE_DPAD_DOWN", "KEYCODE_DPAD_LEFT", "KEYCODE_DPAD_RIGHT",
                           "KEYCODE_VOLUME_UP", "KEYCODE_VOLUME_DOWN"):
                self._remote_panel.flash_button(keycode)
                self._on_remote_keyevent(keycode)
                return True

            # All other keycodes: start long-press timer
            self._start_kb_long_press_timer(keyval, keycode)
            return True
        return False

    def _start_kb_long_press_timer(self, keyval: int, keycode: str) -> None:
        """Start the keyboard long-press timer for a key.

        If the same key is already being tracked (auto-repeat), don't restart the timer.
        """
        lower_keyval = Gdk.keyval_to_lower(keyval)

        # If we're already tracking this same key, don't restart timer (auto-repeat)
        if self._kb_long_press_keyval is not None:
            tracked_lower = Gdk.keyval_to_lower(self._kb_long_press_keyval)
            if lower_keyval == tracked_lower:
                # Same key, ignore auto-repeat
                return

        # Stop any existing timer for different key
        self._stop_kb_long_press_timer()

        # Store state
        self._kb_long_press_keyval = keyval
        self._kb_long_press_keycode = keycode
        self._kb_long_press_triggered = False

        # Start timer (500ms for long-press)
        self._kb_long_press_timer_id = GLib.timeout_add(500, self._on_kb_long_press_fired)

    def _on_kb_long_press_fired(self) -> bool:
        """Called when keyboard long-press timer fires (500ms held)."""
        self._kb_long_press_triggered = True

        if self._kb_long_press_keycode:
            # Flash button and send long-press
            self._remote_panel.flash_button(self._kb_long_press_keycode)
            self._on_remote_long_press(self._kb_long_press_keycode)

        self._kb_long_press_timer_id = None
        return False  # Don't repeat

    def _stop_kb_long_press_timer(self) -> None:
        """Stop the keyboard long-press timer and clear state."""
        if self._kb_long_press_timer_id is not None:
            GLib.source_remove(self._kb_long_press_timer_id)
            self._kb_long_press_timer_id = None

    def _on_key_released(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType) -> None:
        """Handle key release for long-press detection.

        If the key was released before the long-press timer fired,
        send the normal keyevent. If long-press was already triggered,
        do nothing (the long-press callback already handled it).
        """
        lower_keyval = Gdk.keyval_to_lower(keyval)

        # Check if this is the key we're tracking for long-press
        if self._kb_long_press_keyval is None:
            return

        # Match either original or lower-case keyval
        if keyval != self._kb_long_press_keyval and lower_keyval != Gdk.keyval_to_lower(self._kb_long_press_keyval):
            return

        keycode = self._kb_long_press_keycode
        was_triggered = self._kb_long_press_triggered

        # Stop timer and clear state
        self._stop_kb_long_press_timer()
        self._kb_long_press_keyval = None
        self._kb_long_press_keycode = None
        self._kb_long_press_triggered = False

        # If long-press was already triggered, don't send normal keyevent
        if was_triggered:
            return

        # Short press - send normal keyevent
        if keycode:
            self._remote_panel.flash_button(keycode)
            self._on_remote_keyevent(keycode)

    def _on_remote_keyevent(self, keycode: str) -> None:
        """Send a key event to the device.

        Special handling: If keycode is KEYCODE_TV_INPUT and a separate TV is
        configured for input routing, open the TV remote dialog instead.
        """
        # Special handling for Input button
        if keycode == "KEYCODE_TV_INPUT":
            tv_ip = self._settings.get_string("tv-ip")

            # If TV IP is configured and different from connected device, open TV remote dialog
            if tv_ip and tv_ip.strip() and tv_ip.strip() != self._connected_ip:
                # Open dialog - it will send Input command automatically when connected
                self._open_tv_remote_dialog(tv_ip.strip())
                return

        remote = self._input()
        if not remote:
            self._toast("Device is not connected.")
            return

        try:
            remote.send_keycode(keycode)

            # Optimistically update Play/Pause state in UI and MPRIS
            if keycode == "KEYCODE_MEDIA_PLAY_PAUSE":
                current = self._mpris._playback_status
                new_status = "Paused" if current == "Playing" else "Playing"
                self._mpris.set_playback_status(new_status)
                self._remote_panel.update_now_playing(
                    title=self._mpris._track_title,
                    artist=self._mpris._track_artist,
                    playback_status=new_status,
                )

            # Update volume state in UI when volume keys are pressed
            # (the TV reports the real level shortly after)
            if keycode == "KEYCODE_VOLUME_MUTE":
                self._remote_panel.toggle_mute()
            elif keycode == "KEYCODE_VOLUME_UP":
                self._last_volume_change_time = time.time()
                self._current_volume = min(self._current_volume + 1, self._remote_panel._volume_max)
                self._remote_panel.update_volume(self._current_volume, self._remote_panel._volume_max, False)
            elif keycode == "KEYCODE_VOLUME_DOWN":
                self._last_volume_change_time = time.time()
                self._current_volume = max(self._current_volume - 1, 0)
                self._remote_panel.update_volume(self._current_volume, self._remote_panel._volume_max, False)
        except (RemoteError, HomeAssistantError) as e:
            logger.error(f"Key event failed: {e}")
            self._toast("Failed to send command to TV.")

    def _on_remote_long_press(self, keycode: str) -> None:
        """Send a long-press event to the device.

        This is used for the OK/Enter button to trigger context menus on Android TV.
        For example, long-pressing on an app in the app drawer opens the app options menu.

        The long-press is sent in a background thread to avoid blocking the UI.
        """
        remote = self._input()
        if not remote:
            self._toast("Device is not connected.")
            return

        def worker():
            try:
                # Send long-press (600ms hold time for reliable detection)
                remote.send_long_press(keycode, duration_ms=600)
                logger.debug(f"Long-press sent: {keycode}")
            except (RemoteError, HomeAssistantError) as e:
                logger.error(f"Long-press failed: {e}")
                GLib.idle_add(self._toast, "Failed to send long-press to TV.")

        # Run in background thread to avoid UI freeze
        threading.Thread(target=worker, name="long-press", daemon=True).start()

    def _on_power_button_pressed(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        """Handle power button press - start long-press timer."""
        self._stop_power_long_press_timer()
        self._power_long_press_triggered = False
        self._power_long_press_timer_id = GLib.timeout_add(500, self._on_power_long_press_fired)

    def _on_power_button_released(self, gesture: Gtk.GestureClick, n_press: int, x: float, y: float) -> None:
        """Handle power button release."""
        if not self._power_long_press_triggered:
            # Normal short press - send power keycode
            self._on_remote_keyevent("KEYCODE_POWER")
        self._stop_power_long_press_timer()

    def _on_power_button_unpaired_release(self, gesture: Gtk.GestureClick, x: float, y: float, button: int, sequence) -> None:
        """Handle unpaired release of power button."""
        self._stop_power_long_press_timer()

    def _on_power_long_press_fired(self) -> bool:
        """Called when power button long-press timer fires (500ms held)."""
        self._power_long_press_triggered = True
        self._on_remote_long_press("KEYCODE_POWER")
        self._power_long_press_timer_id = None
        return False

    def _stop_power_long_press_timer(self) -> None:
        """Stop the power button long-press timer."""
        if self._power_long_press_timer_id is not None:
            GLib.source_remove(self._power_long_press_timer_id)
            self._power_long_press_timer_id = None
        self._power_long_press_triggered = False

    def _on_remote_text(self, text: str) -> None:
        """Send text input to the focused text field on the device."""
        remote = self._input()
        if not remote:
            self._toast("Device is not connected.")
            return

        try:
            remote.send_text(text)
        except (RemoteError, HomeAssistantError) as e:
            logger.error(f"Text input failed: {e}")
            self._toast("Failed to send text input to TV.")

    def _on_keyboard_focus(self) -> None:
        """The keyboard entry gained focus: start a fresh text mirror."""
        remote = self._input()
        if remote:
            remote.reset_text()

    def _on_notifications(self) -> None:
        """Open the notification panel on the device."""
        remote = self._input()
        if not remote:
            self._toast("Device is not connected.")
            return

        try:
            remote.expand_notification_panel()
        except (RemoteError, HomeAssistantError) as e:
            logger.error(f"Failed to open notification panel: {e}")
            self._toast("Failed to open notifications.")

    def _open_tv_remote_dialog(self, tv_ip: str) -> None:
        """Open the TV remote dialog for controlling the external TV device.

        This is used when a TV IP is configured separately from the connected device
        (e.g., when connected to Mi Box but want to control TV Input).
        """
        # Destroy existing dialog if it exists
        if self._tv_remote_dialog:
            self._tv_remote_dialog.destroy()
            self._tv_remote_dialog = None

        # Always create a fresh dialog, passing the TV connection for fast commands
        self._tv_remote_dialog = TvRemoteDialog(self, tv_ip, self._settings, self._tv_remote, self._tv_device_info)
        self._tv_remote_dialog.present()

    def _paste_clipboard(self) -> None:
        """Read text from clipboard and send it to the device."""
        display = Gdk.Display.get_default()
        if not display:
            return
        clipboard = display.get_clipboard()
        # In Gtk 4, clipboard reading is asynchronous
        clipboard.read_text_async(None, self._on_clipboard_read_ready, None)

    def _on_clipboard_read_ready(self, clipboard: Gdk.Clipboard, result: Gio.AsyncResult, _data: None) -> None:
        """Callback for clipboard.read_text_async."""
        try:
            text = clipboard.read_text_finish(result)
            if text:
                self._on_remote_text(text)
                # Display pasted text in the keyboard entry
                self._remote_panel.append_text(text)
        except Exception as e:
            logger.error(f"Failed to read clipboard: {e}")

    def _on_volume_fetched(self, volume: VolumeInfo) -> None:
        """Called when the TV reports its volume (after connecting and after each change)."""
        # Skip updating if user recently changed volume (within 1 second)
        # This prevents the slider from jumping back to old values
        if time.time() - self._last_volume_change_time < 1.0:
            return

        self._current_volume = volume.level
        self._remote_panel.update_volume(volume.level, volume.maximum, volume.muted)
        # Update MPRIS volume (0.0 to 1.0 range)
        if volume.maximum > 0:
            self._mpris.set_volume(volume.level / volume.maximum)

    def _on_volume_change(self, new_volume: int) -> None:
        """Handle volume slider change.

        The protocol has no "set volume" command, so we send VOLUME_UP or
        VOLUME_DOWN events to adjust the volume.
        """
        remote = self._input()
        if not remote:
            return

        # Record the time of this volume change
        self._last_volume_change_time = time.time()

        diff = new_volume - self._current_volume
        if diff == 0:
            return

        keycode = "KEYCODE_VOLUME_UP" if diff > 0 else "KEYCODE_VOLUME_DOWN"

        try:
            for _ in range(abs(diff)):
                remote.send_keycode(keycode)
            self._current_volume = new_volume
        except (RemoteError, HomeAssistantError) as e:
            logger.error(f"Failed to adjust volume: {e}")

    def _start_tv_remote_async(self, tv_ip: str) -> None:
        """Connect to the TV used for TV Input Routing in background.

        This establishes a second remote connection to the TV device specified
        in settings, enabling faster Input button operations.
        """
        # Get expected TV device model for verification
        expected_tv_model = self._settings.get_string("tv-device-model") or None

        def worker():
            tv_remote = AndroidTvRemote(tv_ip, timeout_s=8.0)
            try:
                tv_device_info = tv_remote.connect()
            except RemoteError as e:
                logger.warning(f"TV connection failed for {tv_ip}: {e}")
                GLib.idle_add(self._on_tv_remote_failed)
                return

            # Verify device model matches expected TV device
            if expected_tv_model and tv_device_info.model != expected_tv_model:
                logger.info(
                    f"TV device model mismatch: expected '{expected_tv_model}', "
                    f"got '{tv_device_info.model}'. Starting discovery."
                )
                tv_remote.disconnect()
                GLib.idle_add(self._on_tv_remote_failed)
                return

            GLib.idle_add(self._on_tv_remote_connected, tv_remote, tv_device_info)

        threading.Thread(target=worker, name="tv-remote-connect", daemon=True).start()

    def _on_tv_remote_connected(self, tv_remote: AndroidTvRemote, tv_device_info: DeviceInfo) -> None:
        """Called when the TV connection for input routing is established."""
        if not self.is_connected:
            # The main connection went away meanwhile
            tv_remote.disconnect()
            return
        self._tv_remote = tv_remote
        self._tv_device_info = tv_device_info
        self._tv_remote_required = False  # Connection established
        # Enable Input button now that the TV is ready
        self._remote_panel.set_input_button_sensitive(True)
        # Update tooltip to show connected TV device (manufacturer + model)
        device_name = f"{tv_device_info.manufacturer} {tv_device_info.model}"
        self._remote_panel.set_input_button_tooltip(f"Input ({device_name})")
        logger.info(f"TV connected at {tv_remote.host} ({device_name}) - Input button will use it")

        # Save TV device info to settings
        self._settings.set_string("tv-device-name", tv_device_info.manufacturer or "")
        self._settings.set_string("tv-device-model", tv_device_info.model or "")

        # Store TV device model info to discovered-devices for future IP discovery
        self._store_tv_device_model(tv_remote.host, tv_device_info)

    def _store_tv_device_model(self, ip: str, device_info: DeviceInfo) -> None:
        """Store TV device model info to discovered-devices for future IP discovery.

        This ensures that when the TV device's IP changes, we can identify it
        by its model name during network discovery.

        Args:
            ip: The TV device's IP address.
            device_info: Device info containing manufacturer and model.
        """
        stored_devices_json = self._settings.get_string("discovered-devices")
        stored_devices: list[dict] = []
        try:
            if stored_devices_json:
                stored_devices = json.loads(stored_devices_json)
        except Exception:
            pass

        # Check if this IP already exists in stored devices
        found = False
        for dev in stored_devices:
            if dev.get("ip") == ip:
                # Update model info
                dev["model"] = device_info.model
                found = True
                break

        # If not found, add as new entry
        if not found:
            stored_devices.append({
                "ip": ip,
                "model": device_info.model,
                "device_name": f"{device_info.manufacturer} {device_info.model}",
                "latency_ms": 0,  # Unknown latency
            })

        # Save updated devices
        self._settings.set_string("discovered-devices", json.dumps(stored_devices))
        logger.info(f"Stored TV device model '{device_info.model}' for IP {ip}")


    def _on_tv_remote_failed(self) -> None:
        """Called when the TV connection for input routing fails.

        Triggers IP discovery to find the TV device at its new IP address,
        then retries the connection. Only attempts discovery once per connection
        session to prevent infinite loops if the device is offline.
        """
        # Check if we already tried discovery - if so, the device is likely offline
        if self._tv_discovery_attempted:
            logger.info("TV connection failed after discovery - device likely offline")
            self._tv_remote_required = False
            self._remote_panel.set_input_button_sensitive(True)
            self._remote_panel.set_input_button_tooltip("Input (TV offline)")
            return

        self._tv_discovery_attempted = True
        logger.info("TV connection failed - starting IP discovery for TV device")

        # Keep Input button disabled during discovery
        self._remote_panel.set_input_button_sensitive(False)
        self._remote_panel.set_input_button_tooltip("Searching for TV device…")

        # Start discovery in background thread
        threading.Thread(
            target=self._discover_tv_device_ip,
            name="tv-ip-discovery",
            daemon=True
        ).start()

    def _discover_tv_device_ip(self) -> None:
        """Scan network to find the TV device's new IP address.

        This runs in a background thread. It:
        1. Gets the stored TV device model from settings
        2. Scans for paired devices with the remote port open
        3. Matches the TV device model
        4. Updates tv-ip setting and retries connection
        """
        tv_ip = self._settings.get_string("tv-ip")
        tv_device_model = self._settings.get_string("tv-device-model") or None

        if not tv_device_model:
            logger.warning("Cannot discover TV device - no model info stored")
            GLib.idle_add(self._on_tv_discovery_complete, None)
            return

        try:
            found = self._find_paired_devices()
        except Exception as e:
            logger.error(f"Network scan for TV device failed: {e}")
            GLib.idle_add(self._on_tv_discovery_complete, None)
            return

        new_tv_ip = next(
            (str(host.ip) for host, info in found if info.model == tv_device_model), None
        )

        # Update tv-ip setting if found at new address
        if new_tv_ip and new_tv_ip != tv_ip:
            logger.info(f"Found TV device '{tv_device_model}' at new IP: {new_tv_ip}")

            def update_tv_ip_setting() -> bool:
                self._ip_discovery_in_progress = True
                self._settings.set_string("tv-ip", new_tv_ip)
                self._ip_discovery_in_progress = False
                self._store_tv_device_model(new_tv_ip, next(info for _h, info in found if info.model == tv_device_model))
                return False
            GLib.idle_add(update_tv_ip_setting)

            logger.info(f"Updated tv-ip from {tv_ip} to {new_tv_ip}")

        GLib.idle_add(self._on_tv_discovery_complete, new_tv_ip)

    def _on_tv_discovery_complete(self, new_tv_ip: str | None) -> bool:
        """Called when TV device IP discovery completes.

        Args:
            new_tv_ip: New IP for TV device, or None if not found.

        Returns:
            False for GLib.idle_add compatibility.
        """
        if new_tv_ip:
            logger.info(f"TV device found at {new_tv_ip}, retrying connection...")
            # Retry the TV connection with new IP
            self._tv_remote_required = True
            self._remote_panel.set_input_button_tooltip("Connecting to TV…")
            self._start_tv_remote_async(new_tv_ip)
        else:
            # Discovery failed - give up on TV connection
            self._tv_remote_required = False
            self._remote_panel.set_input_button_sensitive(True)
            self._remote_panel.set_input_button_tooltip("Input")
            logger.info("TV device not found - Input button will use normal mode")

        return False

    def _on_tv_ip_setting_changed(self, settings: Gio.Settings, key: str) -> None:
        """Called when TV IP setting changes - reconnect to the TV if needed."""
        # Skip if this change is from IP discovery (to prevent reconnection loop)
        if self._ip_discovery_in_progress:
            return

        # Only process if we have an active main connection
        if not self.is_connected:
            return

        # Disconnect the existing TV connection
        if self._tv_remote:
            self._tv_remote.disconnect()
            self._tv_remote = None
        self._tv_device_info = None

        # Get new TV IP
        tv_ip = settings.get_string("tv-ip")

        # Start new connection if TV IP is set and different from connected device
        if tv_ip and tv_ip.strip() and tv_ip.strip() != self._connected_ip:
            # Disable Input button until the TV connects
            self._tv_remote_required = True
            self._tv_discovery_attempted = False  # Reset discovery flag for fresh connection attempt
            self._remote_panel.set_input_button_sensitive(False)
            self._start_tv_remote_async(tv_ip.strip())
            logger.info(f"TV IP changed to {tv_ip} - reconnecting to TV")
        else:
            # No TV routing needed - enable Input button
            self._tv_remote_required = False
            self._remote_panel.set_input_button_sensitive(True)
            logger.info("TV IP cleared or same as connected device - TV routing disabled")

    # -------------------------------------------------------------------------
    # Input route: Direct (Remote protocol v2) or Home Assistant
    # -------------------------------------------------------------------------

    def _input(self) -> InputBackend | None:
        """The backend buttons currently go to, or None if that route is unavailable."""
        if self._ha_mode:
            return self._ha
        remote = self._remote
        return remote if remote and remote.connected else None

    def _refresh_controls(self) -> None:
        self._remote_panel.set_sensitive(self._input() is not None)
        self._remote_panel.set_input_route("Home Assistant" if self._ha_mode else None)

    def reload_home_assistant(self) -> None:
        """(Re)create the Home Assistant backend from settings and warm up its connection."""
        if self._ha:
            self._ha.close()
            self._ha = None

        url = self._settings.get_string("home-assistant-url").strip()
        token = load_token()
        if url and token:
            try:
                self._ha = HomeAssistantRemote(
                    url, token, self._settings.get_string("home-assistant-entity"),
                    on_error=lambda msg: GLib.idle_add(self._on_ha_error, msg),
                )
            except HomeAssistantError as e:
                self._toast(str(e))
            else:
                self._ha.check_async(
                    lambda _name, error: error and GLib.idle_add(self._on_ha_error, error)
                )

        if self._ha_mode and not self._ha:
            self._set_ha_mode(False)
        else:
            self._refresh_controls()

    def _on_ha_button_toggled(self, button: Gtk.ToggleButton) -> None:
        if button.get_active() != self._ha_mode:
            self._set_ha_mode(button.get_active())

    def _set_ha_mode(self, enabled: bool, *, announce: bool = True) -> None:
        """Switch the button route. Both backends stay connected, so this is instant."""
        if enabled and not self._ha:
            enabled = False
            if announce:
                self._toast("Set up Home Assistant in Preferences first.")
                self._on_preferences_clicked()

        changed = enabled != self._ha_mode
        self._ha_mode = enabled
        if self._ha_button.get_active() != enabled:
            self._ha_button.set_active(enabled)
        self._settings.set_boolean("home-assistant-mode", enabled)

        # A held key or half-typed text must not continue on the other route
        self._stop_kb_long_press_timer()
        self._kb_long_press_keyval = None
        self._kb_long_press_keycode = None
        for backend in (self._remote, self._ha):
            if backend:
                backend.reset_text()

        self._refresh_controls()
        self._update_ha_button_tooltip()
        if announce and changed:
            if enabled:
                self._toast(f"Buttons → Home Assistant ({self._ha.entity_id})")
            elif self.is_connected:
                self._toast("Buttons → Direct (TV remote service)")
            else:
                self._toast("Buttons → Direct (not connected)")

    def _update_ha_button_tooltip(self) -> None:
        from .preferences_dialog import get_action_tooltip
        shortcut = get_action_tooltip("toggle-home-assistant", self._settings)
        route = "Home Assistant" if self._ha_mode else "Direct"
        text = f"Buttons go through: {route}. Click to switch"
        self._ha_button.set_tooltip_text(f"{text} ({shortcut})" if shortcut else text)

    def _on_ha_error(self, message: str) -> None:
        # One toast per burst: a held key would otherwise stack many
        now = time.time()
        if now - self._last_ha_error_time > 5.0:
            self._last_ha_error_time = now
            self._toast(f"Home Assistant: {message}")

    # -------------------------------------------------------------------------
    # MPRIS Callbacks - Desktop media control integration
    # -------------------------------------------------------------------------

    def _send_mpris_key(self, keycode: str, flash_keycode: str) -> None:
        remote = self._input()
        if remote:
            try:
                remote.send_keycode(keycode)
                self._remote_panel.flash_button(flash_keycode)
            except (RemoteError, HomeAssistantError) as e:
                logger.error(f"MPRIS {keycode} failed: {e}")

    def _on_mpris_play_pause(self) -> None:
        """Handle MPRIS PlayPause command - toggle play/pause on the TV."""
        self._send_mpris_key("KEYCODE_MEDIA_PLAY_PAUSE", "KEYCODE_MEDIA_PLAY_PAUSE")

    def _on_mpris_play(self) -> None:
        """Handle MPRIS Play command - start playback on the TV."""
        self._send_mpris_key("KEYCODE_MEDIA_PLAY", "KEYCODE_MEDIA_PLAY_PAUSE")

    def _on_mpris_pause(self) -> None:
        """Handle MPRIS Pause command - pause playback on the TV."""
        self._send_mpris_key("KEYCODE_MEDIA_PAUSE", "KEYCODE_MEDIA_PLAY_PAUSE")

    def _on_mpris_stop(self) -> None:
        """Handle MPRIS Stop command - stop playback on the TV."""
        self._send_mpris_key("KEYCODE_MEDIA_STOP", "KEYCODE_MEDIA_STOP")

    def _on_mpris_next(self) -> None:
        """Handle MPRIS Next command - skip to next track on the TV."""
        self._send_mpris_key("KEYCODE_MEDIA_NEXT", "KEYCODE_MEDIA_NEXT")

    def _on_mpris_previous(self) -> None:
        """Handle MPRIS Previous command - skip to previous track on the TV."""
        self._send_mpris_key("KEYCODE_MEDIA_PREVIOUS", "KEYCODE_MEDIA_PREVIOUS")

    def _on_mpris_raise(self) -> None:
        """Handle MPRIS Raise command - bring window to front."""
        self.present()

    def _on_mpris_quit(self) -> None:
        """Handle MPRIS Quit command - close the application."""
        self.get_application().quit()

    def _handle_connection_drop(self) -> None:
        """Called when the remote link drops (TV off, network loss, PC resumed from sleep).

        Triggers an automatic reconnection attempt.
        """
        # Only act if we think we are still connected
        if not self._connected_ip:
            return

        current_ip = self._connected_ip
        logger.warning(f"Connection lost to {current_ip}. Attempting to reconnect...")

        # Disconnect cleanly to update UI
        self._set_connected(False)
        self._mpris.set_device_connected(False)

        # Attempt reconnection with discovery fallback
        # This will verify the connection and if failed, start discovery
        self._connect_ip_with_discovery_fallback(current_ip)
