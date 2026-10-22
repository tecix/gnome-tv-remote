"""
Device Discovery and Connection Dialog.

This module provides the DeviceDialog class, a modal dialog for discovering
Android TV devices on the local network and initiating connections. It includes
network scanning functionality and persists discovered devices across sessions.
"""

from __future__ import annotations

import json
import ipaddress

import threading
from typing import TYPE_CHECKING

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Adw", "1")
gi.require_version("Gdk", "4.0")

from gi.repository import Adw, Gdk, GLib, Gtk  # noqa: E402


from ..core.network_info import get_ipv4_interface_networks  # noqa: E402
from ..core.scanner import HostFound, ScanProgress, SubnetScanner  # noqa: E402
from ..core.remote_v2 import REMOTE_PORT, probe_paired_device  # noqa: E402

if TYPE_CHECKING:
    from .main_window import MainWindow


class DeviceDialog(Adw.Window):
    """Modal dialog for discovering and connecting to Android TV devices.
    
    Provides:
    - Manual IP address entry for direct connection
    - Network scanning to discover devices running the Android TV remote service
    - Pairing (via the code the TV shows) when a device doesn't know us yet
    - Persistence of discovered devices across app sessions
    - Device info display (model, latency)
    """

    def __init__(self, parent: MainWindow) -> None:
        super().__init__(
            transient_for=parent,
            modal=True,
            title="Connect to Device",
            default_width=400,
            default_height=500,
        )
        self._parent = parent
        self._scan_in_progress = False
        self._scan_cancel: threading.Event | None = None
        self._scan_thread: threading.Thread | None = None
        self._found_ips: set[str] = set()
        self._discovered_devices: list[dict[str, str | float | None]] = []
        self._device_rows: dict[str, Adw.ActionRow] = {}
        
        self._connect_in_progress = False
        self._ip_entry_button: Gtk.Button | None = None

        self._build_ui()
        self._load_discovered_devices()
        self.connect("close-request", self._on_close_request)
        
        # Add keyboard controller for Escape key
        key_controller = Gtk.EventControllerKey()
        key_controller.connect("key-pressed", self._on_key_pressed)
        self.add_controller(key_controller)

    def _on_close_request(self, *_args) -> bool:
        self.hide()
        return True

    def _on_key_pressed(self, _controller: Gtk.EventControllerKey, keyval: int, _keycode: int, _state: Gdk.ModifierType) -> bool:
        """Handle keyboard shortcuts in the dialog."""
        if keyval == Gdk.KEY_Escape:
            self.close()
            return True
        return False

    def update_last_ip(self) -> None:
        """Refresh the IP entry with the last connected IP from settings."""
        last_ip = self._parent._settings.get_string("last-connected-ip")
        if last_ip:
            self._ip_entry.set_text(last_ip)

    def _load_discovered_devices(self) -> None:
        """Load previously discovered devices from settings."""
        json_data = self._parent._settings.get_string("discovered-devices")
        if not json_data:
            return
        try:
            devices = json.loads(json_data)
            for device in devices:
                ip = device.get("ip")
                latency = device.get("latency_ms", 0.0)
                model = device.get("model")
                device_name = device.get("device_name")
                if ip:
                    self._on_scan_found_ui(ip, latency, save=False, model=model, device_name=device_name)
        except Exception:
            pass

    def _save_discovered_devices(self) -> None:
        """Save currently found devices to settings."""
        json_data = json.dumps(self._discovered_devices)
        self._parent._settings.set_string("discovered-devices", json_data)

    def _build_ui(self) -> None:
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        self._scan_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        self._scan_btn.set_tooltip_text("Scan for devices")
        self._scan_btn.connect("clicked", self._on_scan_clicked)
        header.pack_start(self._scan_btn)

        self._scan_spinner = Gtk.Spinner(spinning=False)
        header.pack_start(self._scan_spinner)

        # Content
        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        content.set_margin_top(12)
        content.set_margin_bottom(12)
        content.set_margin_start(12)
        content.set_margin_end(12)
        toolbar_view.set_content(content)

        # IP Entry Row
        ip_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        self._ip_entry = Gtk.Entry(placeholder_text="Device IP (e.g. 192.168.1.50)")
        self._ip_entry.set_input_purpose(Gtk.InputPurpose.URL)
        self._ip_entry.set_hexpand(True)
        self._ip_entry.connect("activate", self._on_connect_clicked)
        
        # Pre-fill with last connected IP if available
        last_ip = self._parent._settings.get_string("last-connected-ip")
        if last_ip:
            self._ip_entry.set_text(last_ip)

        ip_row.append(self._ip_entry)

        self._ip_entry_button = Gtk.Button(label="Connect")
        self._ip_entry_button.add_css_class("suggested-action")
        self._ip_entry_button.connect("clicked", self._on_connect_clicked)
        ip_row.append(self._ip_entry_button)
        content.append(ip_row)



        # Scan Progress
        self._scan_progress = Gtk.ProgressBar(show_text=True)
        self._scan_progress.set_visible(False)
        content.append(self._scan_progress)

        # Device List
        self._device_list = Gtk.ListBox()
        self._device_list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._device_list.add_css_class("boxed-list")

        device_scroll = Gtk.ScrolledWindow()
        device_scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        device_scroll.set_vexpand(True)
        device_scroll.set_child(self._device_list)
        content.append(device_scroll)

    def _on_connect_clicked(self, *_args) -> None:
        ip = self._ip_entry.get_text().strip()
        if not ip:
            self._parent._toast("Enter an IP address")
            return
        
        try:
            ipaddress.ip_address(ip)
        except ValueError:
            self._parent._toast("Invalid IP address")
            return

        self._start_connect(ip, self._ip_entry_button)

    def _on_scan_clicked(self, *_args) -> None:
        if self._scan_in_progress:
            if self._scan_cancel:
                self._scan_cancel.set()
                self._parent._toast("Stopping scan…")
            return

        self._set_scanning(True)
        while child := self._device_list.get_first_child():
            self._device_list.remove(child)
        self._found_ips.clear()
        self._discovered_devices.clear()
        self._device_rows.clear()
        self._save_discovered_devices()

        nets = [n.network for n in get_ipv4_interface_networks(limit_to_slash24_if_broader=True)]
        if not nets:
            self._set_scanning(False)
            self._parent._toast("No private IPv4 networks found to scan")
            return

        self._scan_cancel = threading.Event()
        scanner = SubnetScanner(port=REMOTE_PORT, timeout_s=0.35, concurrency=256)

        def on_progress(p: ScanProgress) -> None:
            GLib.idle_add(self._on_scan_progress_ui, p.scanned, p.total)

        def on_found(found: HostFound) -> None:
            # First add the device to UI with basic info
            GLib.idle_add(self._on_scan_found_ui, str(found.ip), found.latency_ms)
            # Then fetch device info in background if it is already paired
            self._check_and_fetch_device_info(str(found.ip), found.latency_ms)

        def run() -> None:
            try:
                scanner.scan(nets, cancel_event=self._scan_cancel, on_progress=on_progress, on_found=on_found)
            finally:
                cancelled = bool(self._scan_cancel and self._scan_cancel.is_set())
                GLib.idle_add(self._on_scan_finished_ui, cancelled)

        self._scan_thread = threading.Thread(target=run, name="subnet-scan", daemon=True)
        self._scan_thread.start()

    def _set_scanning(self, scanning: bool) -> None:
        self._scan_in_progress = scanning
        self._scan_spinner.set_spinning(scanning)
        self._scan_progress.set_visible(scanning)
        if scanning:
            self._scan_progress.set_fraction(0.0)
            self._scan_progress.set_text("Scanning…")
            self._scan_btn.set_icon_name("process-stop-symbolic")
        else:
            self._scan_btn.set_icon_name("view-refresh-symbolic")

    def _on_scan_progress_ui(self, scanned: int, total: int) -> None:
        if not self._scan_in_progress:
            return
        frac = 0.0 if total <= 0 else min(1.0, scanned / total)
        self._scan_progress.set_fraction(frac)
        self._scan_progress.set_text(f"Scanning {scanned}/{total}")

    def _on_scan_found_ui(self, ip: str, latency_ms: float, save: bool = True, model: str | None = None, device_name: str | None = None) -> None:
        if ip in self._found_ips:
            return
        self._found_ips.add(ip)

        if save:
            device_data: dict[str, str | float | None] = {"ip": ip, "latency_ms": latency_ms}
            if model:
                device_data["model"] = model
            if device_name:
                device_data["device_name"] = device_name
            self._discovered_devices.append(device_data)
            self._save_discovered_devices()

        row = Adw.ActionRow()
        self._set_row_text(row, ip, latency_ms, model, device_name)

        btn = Gtk.Button(label="Connect")
        btn.set_valign(Gtk.Align.CENTER)
        btn.add_css_class("suggested-action")
        btn.connect("clicked", lambda *_, b=btn: self._connect_from_row(ip, b))
        row.add_suffix(btn)
        row.set_activatable_widget(btn)

        self._device_list.append(row)
        self._device_rows[ip] = row

    @staticmethod
    def _set_row_text(row: Adw.ActionRow, ip: str, latency_ms: float, model: str | None, device_name: str | None) -> None:
        # Title: device_name if available, otherwise IP
        if device_name:
            subtitle = f"{ip} • {latency_ms:.0f} ms"
        elif model:
            subtitle = f"{model} • {latency_ms:.0f} ms"
        else:
            subtitle = f"Remote service found ({latency_ms:.0f} ms), not paired yet"
        row.set_title(GLib.markup_escape_text(device_name or ip))
        row.set_subtitle(GLib.markup_escape_text(subtitle))

    def _check_and_fetch_device_info(self, ip: str, latency_ms: float) -> None:
        """Fetch device info in a background thread if the TV already trusts us."""
        def check_and_fetch() -> None:
            info = probe_paired_device(ip, timeout_s=2.0)
            if info:
                GLib.idle_add(self._update_device_row, ip, latency_ms, info.model, f"{info.manufacturer} {info.model}")

        threading.Thread(target=check_and_fetch, daemon=True).start()

    def _update_device_row(self, ip: str, latency_ms: float, model: str | None, device_name: str | None) -> None:
        """Update a device row with fetched device information."""
        row = self._device_rows.get(ip)
        if not row:
            return
        self._set_row_text(row, ip, latency_ms, model, device_name)

        # Update saved data
        for device in self._discovered_devices:
            if device.get("ip") == ip:
                device["model"] = model
                device["device_name"] = device_name
                break
        self._save_discovered_devices()

    def _connect_from_row(self, ip: str, btn: Gtk.Button) -> None:
        self._start_connect(ip, btn)

    def _start_connect(self, ip: str, btn: Gtk.Button) -> None:
        """Connect to the TV, pairing first if it asks for it."""
        if self._connect_in_progress:
            self._parent._toast("Already connecting to another device")
            return

        self._connect_in_progress = True
        btn.set_label("Connecting")
        btn.set_sensitive(False)
        btn.remove_css_class("suggested-action")
        btn.add_css_class("accent")

        def on_done(success: bool) -> None:
            self._connect_in_progress = False
            btn.set_label("Connect")
            btn.set_sensitive(True)
            btn.remove_css_class("accent")
            btn.add_css_class("suggested-action")
            if success:
                remote = self._parent._remote
                if remote and remote.device_info:
                    info = remote.device_info
                    latency_ms = next(
                        (float(d.get("latency_ms") or 0.0) for d in self._discovered_devices if d.get("ip") == ip),
                        0.0,
                    )
                    self._update_device_row(ip, latency_ms, info.model, f"{info.manufacturer} {info.model}")
                self.close()

        self._parent._connect_ip(ip, on_done=on_done, dialog_parent=self)

    def _on_scan_finished_ui(self, cancelled: bool) -> None:
        self._set_scanning(False)
        self._scan_cancel = None
        self._scan_thread = None
        if cancelled:
            self._parent._toast("Scan stopped")
        else:
            self._parent._toast(f"Scan finished ({len(self._found_ips)} found)")
