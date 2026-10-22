"""
MPRIS D-Bus Service for Android TV Remote.

This module implements the MPRIS (Media Player Remote Interfacing Specification)
D-Bus interface, allowing desktop environments like GNOME to control media
playback on the connected Android TV device.

MPRIS Interfaces Implemented:
- org.mpris.MediaPlayer2 - Basic player identity
- org.mpris.MediaPlayer2.Player - Playback controls
"""

from __future__ import annotations

import logging
from typing import Callable, Optional

import gi

gi.require_version("Gio", "2.0")
gi.require_version("GLib", "2.0")

from gi.repository import Gio, GLib

logger = logging.getLogger(__name__)

# D-Bus bus name for our MPRIS player
MPRIS_BUS_NAME = "org.mpris.MediaPlayer2.AndroidTVRemote"
MPRIS_OBJECT_PATH = "/org/mpris/MediaPlayer2"

# MPRIS interface definitions
MPRIS_INTROSPECTION_XML = """
<!DOCTYPE node PUBLIC "-//freedesktop//DTD D-BUS Object Introspection 1.0//EN"
  "http://www.freedesktop.org/standards/dbus/1.0/introspect.dtd">
<node>
  <interface name="org.mpris.MediaPlayer2">
    <method name="Raise"/>
    <method name="Quit"/>
    <property name="CanQuit" type="b" access="read"/>
    <property name="CanRaise" type="b" access="read"/>
    <property name="HasTrackList" type="b" access="read"/>
    <property name="Identity" type="s" access="read"/>
    <property name="DesktopEntry" type="s" access="read"/>
    <property name="SupportedUriSchemes" type="as" access="read"/>
    <property name="SupportedMimeTypes" type="as" access="read"/>
  </interface>
  <interface name="org.mpris.MediaPlayer2.Player">
    <method name="Next"/>
    <method name="Previous"/>
    <method name="Pause"/>
    <method name="PlayPause"/>
    <method name="Stop"/>
    <method name="Play"/>
    <method name="Seek">
      <arg direction="in" name="Offset" type="x"/>
    </method>
    <method name="SetPosition">
      <arg direction="in" name="TrackId" type="o"/>
      <arg direction="in" name="Position" type="x"/>
    </method>
    <method name="OpenUri">
      <arg direction="in" name="Uri" type="s"/>
    </method>
    <property name="PlaybackStatus" type="s" access="read"/>
    <property name="LoopStatus" type="s" access="readwrite"/>
    <property name="Rate" type="d" access="readwrite"/>
    <property name="Shuffle" type="b" access="readwrite"/>
    <property name="Metadata" type="a{sv}" access="read"/>
    <property name="Volume" type="d" access="readwrite"/>
    <property name="Position" type="x" access="read"/>
    <property name="MinimumRate" type="d" access="read"/>
    <property name="MaximumRate" type="d" access="read"/>
    <property name="CanGoNext" type="b" access="read"/>
    <property name="CanGoPrevious" type="b" access="read"/>
    <property name="CanPlay" type="b" access="read"/>
    <property name="CanPause" type="b" access="read"/>
    <property name="CanSeek" type="b" access="read"/>
    <property name="CanControl" type="b" access="read"/>
    <signal name="Seeked">
      <arg name="Position" type="x"/>
    </signal>
  </interface>
  <interface name="org.freedesktop.DBus.Properties">
    <method name="Get">
      <arg direction="in" name="interface_name" type="s"/>
      <arg direction="in" name="property_name" type="s"/>
      <arg direction="out" type="v"/>
    </method>
    <method name="GetAll">
      <arg direction="in" name="interface_name" type="s"/>
      <arg direction="out" type="a{sv}"/>
    </method>
    <method name="Set">
      <arg direction="in" name="interface_name" type="s"/>
      <arg direction="in" name="property_name" type="s"/>
      <arg direction="in" name="value" type="v"/>
    </method>
    <signal name="PropertiesChanged">
      <arg name="interface_name" type="s"/>
      <arg name="changed_properties" type="a{sv}"/>
      <arg name="invalidated_properties" type="as"/>
    </signal>
  </interface>
</node>
"""


class MprisService:
    """MPRIS D-Bus service for controlling Android TV media playback.
    
    This service exposes standard MPRIS interfaces on the session bus,
    allowing desktop environments to show media controls and forward
    play/pause/next/previous commands to the connected Android TV.
    """

    def __init__(
        self,
        on_play_pause: Optional[Callable[[], None]] = None,
        on_play: Optional[Callable[[], None]] = None,
        on_pause: Optional[Callable[[], None]] = None,
        on_stop: Optional[Callable[[], None]] = None,
        on_next: Optional[Callable[[], None]] = None,
        on_previous: Optional[Callable[[], None]] = None,
        on_raise: Optional[Callable[[], None]] = None,
        on_quit: Optional[Callable[[], None]] = None,
    ) -> None:
        """Initialize the MPRIS service.
        
        Args:
            on_play_pause: Callback for PlayPause action.
            on_play: Callback for Play action.
            on_pause: Callback for Pause action.
            on_stop: Callback for Stop action.
            on_next: Callback for Next action.
            on_previous: Callback for Previous action.
            on_raise: Callback for Raise (show window) action.
            on_quit: Callback for Quit action.
        """
        self._on_play_pause = on_play_pause
        self._on_play = on_play
        self._on_pause = on_pause
        self._on_stop = on_stop
        self._on_next = on_next
        self._on_previous = on_previous
        self._on_raise = on_raise
        self._on_quit = on_quit

        self._connection: Optional[Gio.DBusConnection] = None
        self._bus_name_id: int = 0
        self._object_id: int = 0
        self._node_info: Optional[Gio.DBusNodeInfo] = None
        
        # Player state
        self._playback_status = "Stopped"  # "Playing", "Paused", "Stopped"
        self._volume = 1.0  # 0.0 to 1.0
        self._can_control = False  # True when connected to a device
        self._device_name = "Android TV"
        
        # Media info
        self._track_title: str | None = None
        self._track_artist: str | None = None
        self._track_album: str | None = None
        self._position_us: int = 0  # Position in microseconds (MPRIS uses microseconds)
        
    @property
    def connected(self) -> bool:
        """Check if the MPRIS service is registered on D-Bus."""
        return self._bus_name_id != 0
    
    def start(self) -> None:
        """Start the MPRIS service and register on the session bus."""
        if self._bus_name_id != 0:
            logger.debug("MPRIS service already started")
            return
        
        try:
            self._node_info = Gio.DBusNodeInfo.new_for_xml(MPRIS_INTROSPECTION_XML)
            
            # Get the session bus
            self._connection = Gio.bus_get_sync(Gio.BusType.SESSION, None)
            if not self._connection:
                logger.error("Failed to get session bus")
                return
            
            # Register the object
            interface_info = self._node_info.lookup_interface("org.mpris.MediaPlayer2")
            player_interface_info = self._node_info.lookup_interface("org.mpris.MediaPlayer2.Player")
            properties_interface_info = self._node_info.lookup_interface("org.freedesktop.DBus.Properties")
            
            # Register all interfaces on the same object path
            self._object_id = self._connection.register_object(
                MPRIS_OBJECT_PATH,
                properties_interface_info,
                self._handle_method_call,
                self._handle_get_property,
                self._handle_set_property,
            )
            
            if self._object_id == 0:
                logger.error("Failed to register MPRIS object")
                return
                
            # Register additional interfaces
            self._connection.register_object(
                MPRIS_OBJECT_PATH,
                interface_info,
                self._handle_method_call,
                self._handle_get_property,
                self._handle_set_property,
            )
            
            self._connection.register_object(
                MPRIS_OBJECT_PATH,
                player_interface_info,
                self._handle_method_call,
                self._handle_get_property,
                self._handle_set_property,
            )
            
            # Own the bus name
            self._bus_name_id = Gio.bus_own_name_on_connection(
                self._connection,
                MPRIS_BUS_NAME,
                Gio.BusNameOwnerFlags.NONE,
                self._on_name_acquired,
                self._on_name_lost,
            )
            
            logger.info(f"MPRIS service started: {MPRIS_BUS_NAME}")
            
        except Exception as e:
            logger.error(f"Failed to start MPRIS service: {e}")
    
    def stop(self) -> None:
        """Stop the MPRIS service and unregister from the session bus."""
        if self._bus_name_id != 0:
            Gio.bus_unown_name(self._bus_name_id)
            self._bus_name_id = 0
            
        if self._object_id != 0 and self._connection:
            self._connection.unregister_object(self._object_id)
            self._object_id = 0
            
        self._connection = None
        logger.info("MPRIS service stopped")
    
    def set_device_connected(self, connected: bool, device_name: str = "Android TV") -> None:
        """Update the connection state and emit property changes.
        
        Args:
            connected: Whether a device is connected.
            device_name: Name of the connected device (for metadata).
        """
        self._can_control = connected
        self._device_name = device_name
        if not connected:
            self._playback_status = "Stopped"
        
        self._emit_properties_changed("org.mpris.MediaPlayer2.Player", {
            "CanControl": GLib.Variant("b", self._can_control),
            "CanPlay": GLib.Variant("b", self._can_control),
            "CanPause": GLib.Variant("b", self._can_control),
            "CanGoNext": GLib.Variant("b", self._can_control),
            "CanGoPrevious": GLib.Variant("b", self._can_control),
            "PlaybackStatus": GLib.Variant("s", self._playback_status),
            "Metadata": self._get_metadata(),
        })
    
    def set_playback_status(self, status: str) -> None:
        """Update the playback status.
        
        Args:
            status: One of "Playing", "Paused", or "Stopped".
        """
        if status not in ("Playing", "Paused", "Stopped"):
            logger.warning(f"Invalid playback status: {status}")
            return
            
        if self._playback_status != status:
            self._playback_status = status
            self._emit_properties_changed("org.mpris.MediaPlayer2.Player", {
                "PlaybackStatus": GLib.Variant("s", status),
            })
    
    def set_volume(self, volume: float) -> None:
        """Update the volume level.
        
        Args:
            volume: Volume level from 0.0 to 1.0.
        """
        volume = max(0.0, min(1.0, volume))
        if self._volume != volume:
            self._volume = volume
            self._emit_properties_changed("org.mpris.MediaPlayer2.Player", {
                "Volume": GLib.Variant("d", volume),
            })
    
    def set_media_info(
        self,
        title: str | None = None,
        artist: str | None = None,
        album: str | None = None,
        playback_status: str | None = None,
        position_ms: int = 0,
    ) -> None:
        """Update the currently playing media information.
        
        Args:
            title: Track/video title.
            artist: Artist/channel name.
            album: Album name (if available).
            playback_status: One of "Playing", "Paused", "Stopped".
            position_ms: Current playback position in milliseconds.
        """
        changed = False
        
        if title != self._track_title:
            self._track_title = title
            changed = True
        if artist != self._track_artist:
            self._track_artist = artist
            changed = True
        if album != self._track_album:
            self._track_album = album
            changed = True
        
        # Convert milliseconds to microseconds for MPRIS
        new_position_us = position_ms * 1000
        if new_position_us != self._position_us:
            self._position_us = new_position_us
            # Position doesn't trigger PropertiesChanged signal per MPRIS spec
        
        # Update playback status if provided
        if playback_status and playback_status in ("Playing", "Paused", "Stopped"):
            if self._playback_status != playback_status:
                self._playback_status = playback_status
                changed = True
        
        if changed:
            self._emit_properties_changed("org.mpris.MediaPlayer2.Player", {
                "PlaybackStatus": GLib.Variant("s", self._playback_status),
                "Metadata": self._get_metadata(),
            })
    
    def _on_name_acquired(self, connection: Gio.DBusConnection, name: str) -> None:
        """Called when the bus name is acquired."""
        logger.debug(f"MPRIS bus name acquired: {name}")
    
    def _on_name_lost(self, connection: Gio.DBusConnection, name: str) -> None:
        """Called when the bus name is lost."""
        logger.warning(f"MPRIS bus name lost: {name}")
        self._bus_name_id = 0
    
    def _handle_method_call(
        self,
        connection: Gio.DBusConnection,
        sender: str,
        object_path: str,
        interface_name: str,
        method_name: str,
        parameters: GLib.Variant,
        invocation: Gio.DBusMethodInvocation,
    ) -> None:
        """Handle D-Bus method calls."""
        try:
            if interface_name == "org.mpris.MediaPlayer2":
                if method_name == "Raise":
                    if self._on_raise:
                        self._on_raise()
                    invocation.return_value(None)
                elif method_name == "Quit":
                    if self._on_quit:
                        self._on_quit()
                    invocation.return_value(None)
                else:
                    invocation.return_error_literal(
                        Gio.dbus_error_quark(),
                        Gio.DBusError.UNKNOWN_METHOD,
                        f"Unknown method: {method_name}",
                    )
                    
            elif interface_name == "org.mpris.MediaPlayer2.Player":
                if method_name == "PlayPause":
                    if self._on_play_pause:
                        self._on_play_pause()
                    # Toggle playback status
                    if self._playback_status == "Playing":
                        self.set_playback_status("Paused")
                    else:
                        self.set_playback_status("Playing")
                    invocation.return_value(None)
                elif method_name == "Play":
                    if self._on_play:
                        self._on_play()
                    self.set_playback_status("Playing")
                    invocation.return_value(None)
                elif method_name == "Pause":
                    if self._on_pause:
                        self._on_pause()
                    self.set_playback_status("Paused")
                    invocation.return_value(None)
                elif method_name == "Stop":
                    if self._on_stop:
                        self._on_stop()
                    self.set_playback_status("Stopped")
                    invocation.return_value(None)
                elif method_name == "Next":
                    if self._on_next:
                        self._on_next()
                    invocation.return_value(None)
                elif method_name == "Previous":
                    if self._on_previous:
                        self._on_previous()
                    invocation.return_value(None)
                elif method_name in ("Seek", "SetPosition", "OpenUri"):
                    # These methods are not fully implemented for a remote control
                    invocation.return_value(None)
                else:
                    invocation.return_error_literal(
                        Gio.dbus_error_quark(),
                        Gio.DBusError.UNKNOWN_METHOD,
                        f"Unknown method: {method_name}",
                    )
                    
            elif interface_name == "org.freedesktop.DBus.Properties":
                if method_name == "Get":
                    iface, prop = parameters.unpack()
                    value = self._get_property(iface, prop)
                    if value is not None:
                        invocation.return_value(GLib.Variant("(v)", (value,)))
                    else:
                        invocation.return_error_literal(
                            Gio.dbus_error_quark(),
                            Gio.DBusError.UNKNOWN_PROPERTY,
                            f"Unknown property: {prop}",
                        )
                elif method_name == "GetAll":
                    iface = parameters.unpack()[0]
                    props = self._get_all_properties(iface)
                    invocation.return_value(GLib.Variant("(a{sv})", (props,)))
                elif method_name == "Set":
                    iface, prop, value = parameters.unpack()
                    self._set_property_value(iface, prop, value)
                    invocation.return_value(None)
                else:
                    invocation.return_error_literal(
                        Gio.dbus_error_quark(),
                        Gio.DBusError.UNKNOWN_METHOD,
                        f"Unknown method: {method_name}",
                    )
            else:
                invocation.return_error_literal(
                    Gio.dbus_error_quark(),
                    Gio.DBusError.UNKNOWN_INTERFACE,
                    f"Unknown interface: {interface_name}",
                )
        except Exception as e:
            logger.error(f"Error handling method call {interface_name}.{method_name}: {e}")
            invocation.return_error_literal(
                Gio.dbus_error_quark(),
                Gio.DBusError.FAILED,
                str(e),
            )
    
    def _handle_get_property(
        self,
        connection: Gio.DBusConnection,
        sender: str,
        object_path: str,
        interface_name: str,
        property_name: str,
    ) -> GLib.Variant:
        """Handle D-Bus property get requests."""
        return self._get_property(interface_name, property_name)
    
    def _handle_set_property(
        self,
        connection: Gio.DBusConnection,
        sender: str,
        object_path: str,
        interface_name: str,
        property_name: str,
        value: GLib.Variant,
    ) -> bool:
        """Handle D-Bus property set requests."""
        return self._set_property_value(interface_name, property_name, value)
    
    def _get_property(self, interface_name: str, property_name: str) -> Optional[GLib.Variant]:
        """Get a property value."""
        if interface_name == "org.mpris.MediaPlayer2":
            if property_name == "CanQuit":
                return GLib.Variant("b", True)
            elif property_name == "CanRaise":
                return GLib.Variant("b", True)
            elif property_name == "HasTrackList":
                return GLib.Variant("b", False)
            elif property_name == "Identity":
                return GLib.Variant("s", "TV Remote")
            elif property_name == "DesktopEntry":
                return GLib.Variant("s", "io.github.tecix.TvRemote")
            elif property_name == "SupportedUriSchemes":
                return GLib.Variant("as", [])
            elif property_name == "SupportedMimeTypes":
                return GLib.Variant("as", [])
                
        elif interface_name == "org.mpris.MediaPlayer2.Player":
            if property_name == "PlaybackStatus":
                return GLib.Variant("s", self._playback_status)
            elif property_name == "LoopStatus":
                return GLib.Variant("s", "None")
            elif property_name == "Rate":
                return GLib.Variant("d", 1.0)
            elif property_name == "Shuffle":
                return GLib.Variant("b", False)
            elif property_name == "Metadata":
                return self._get_metadata()
            elif property_name == "Volume":
                return GLib.Variant("d", self._volume)
            elif property_name == "Position":
                return GLib.Variant("x", 0)
            elif property_name == "MinimumRate":
                return GLib.Variant("d", 1.0)
            elif property_name == "MaximumRate":
                return GLib.Variant("d", 1.0)
            elif property_name == "CanGoNext":
                return GLib.Variant("b", self._can_control)
            elif property_name == "CanGoPrevious":
                return GLib.Variant("b", self._can_control)
            elif property_name == "CanPlay":
                return GLib.Variant("b", self._can_control)
            elif property_name == "CanPause":
                return GLib.Variant("b", self._can_control)
            elif property_name == "CanSeek":
                return GLib.Variant("b", False)
            elif property_name == "CanControl":
                return GLib.Variant("b", self._can_control)
        
        return None
    
    def _get_all_properties(self, interface_name: str) -> dict:
        """Get all properties for an interface."""
        props = {}
        
        if interface_name == "org.mpris.MediaPlayer2":
            for prop in ["CanQuit", "CanRaise", "HasTrackList", "Identity", 
                         "DesktopEntry", "SupportedUriSchemes", "SupportedMimeTypes"]:
                value = self._get_property(interface_name, prop)
                if value:
                    props[prop] = value
                    
        elif interface_name == "org.mpris.MediaPlayer2.Player":
            for prop in ["PlaybackStatus", "LoopStatus", "Rate", "Shuffle", "Metadata",
                         "Volume", "Position", "MinimumRate", "MaximumRate", 
                         "CanGoNext", "CanGoPrevious", "CanPlay", "CanPause", 
                         "CanSeek", "CanControl"]:
                value = self._get_property(interface_name, prop)
                if value:
                    props[prop] = value
        
        return props
    
    def _set_property_value(self, interface_name: str, property_name: str, value: GLib.Variant) -> bool:
        """Set a property value."""
        if interface_name == "org.mpris.MediaPlayer2.Player":
            if property_name == "Volume":
                self._volume = value.get_double()
                return True
            elif property_name == "LoopStatus":
                # Not implemented - TV doesn't support loop control
                return True
            elif property_name == "Rate":
                # Not implemented - no playback rate control
                return True
            elif property_name == "Shuffle":
                # Not implemented - no shuffle control
                return True
        return False
    
    def _get_metadata(self) -> GLib.Variant:
        """Get the metadata dictionary."""
        metadata: dict[str, GLib.Variant] = {
            "mpris:trackid": GLib.Variant("o", "/org/mpris/MediaPlayer2/CurrentTrack"),
        }
        
        if self._can_control:
            # Use track info if available, otherwise use device name
            if self._track_title:
                metadata["xesam:title"] = GLib.Variant("s", self._track_title)
            else:
                metadata["xesam:title"] = GLib.Variant("s", self._device_name)
            
            if self._track_artist:
                metadata["xesam:artist"] = GLib.Variant("as", [self._track_artist])
            else:
                metadata["xesam:artist"] = GLib.Variant("as", ["TV Remote"])
            
            if self._track_album:
                metadata["xesam:album"] = GLib.Variant("s", self._track_album)
        
        return GLib.Variant("a{sv}", metadata)
    
    def _emit_properties_changed(self, interface_name: str, changed_properties: dict) -> None:
        """Emit the PropertiesChanged signal."""
        if not self._connection:
            return
            
        try:
            self._connection.emit_signal(
                None,  # destination (None = broadcast)
                MPRIS_OBJECT_PATH,
                "org.freedesktop.DBus.Properties",
                "PropertiesChanged",
                GLib.Variant("(sa{sv}as)", (
                    interface_name,
                    changed_properties,
                    [],  # invalidated properties
                )),
            )
        except Exception as e:
            logger.error(f"Failed to emit PropertiesChanged: {e}")
