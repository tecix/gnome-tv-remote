"""
Shared UI Utilities.

This module centralizes common UI functionality used across multiple dialogs
and panels, including icon loading and button visual feedback.
"""

from __future__ import annotations

import os

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gio, GLib, Gtk  # noqa: E402


def get_icons_dir() -> str:
    """Get the path to the material icons directory.
    
    Checks Flatpak path first, then falls back to local development path.
    
    Returns:
        Absolute path to the icons directory.
    """
    # Check Flatpak path first
    flatpak_path = "/app/share/io.github.tecix.TvRemote/icons/material"
    if os.path.exists(flatpak_path):
        return flatpak_path
    # Fallback to local development path
    return os.path.abspath(os.path.join(os.path.dirname(__file__), "../../../data/icons/material"))


# Cached icons directory path
ICONS_DIR = get_icons_dir()


def create_icon(icon_name: str, pixel_size: int = 24) -> Gtk.Image:
    """Create an icon from a file path or icon name.
    
    Uses Gio.FileIcon to load SVG files as symbolic icons,
    which allows them to adapt their color to the current theme.
    
    Args:
        icon_name: Either a theme icon name (e.g., "user-home-symbolic") 
                   or an SVG filename (e.g., "keyboard_arrow_up-symbolic.svg")
        pixel_size: The size of the icon in pixels.
    
    Returns:
        A Gtk.Image widget with the icon.
    """
    if icon_name.endswith(".svg"):
        path = os.path.join(ICONS_DIR, icon_name)
        if os.path.exists(path):
            # Load as GIcon to get proper symbolic icon theming
            file = Gio.File.new_for_path(path)
            gicon = Gio.FileIcon.new(file)
            image = Gtk.Image.new_from_gicon(gicon)
            image.set_pixel_size(pixel_size)
            return image
    
    image = Gtk.Image.new_from_icon_name(icon_name)
    image.set_pixel_size(pixel_size)
    return image


def flash_button(button: Gtk.Button, duration_ms: int = 150) -> None:
    """Flash a button to show visual feedback.
    
    Adds the "suggested-action" CSS class temporarily to
    indicate the button was activated.
    
    Args:
        button: The button to flash.
        duration_ms: How long to show the flash effect in milliseconds.
    """
    button.add_css_class("suggested-action")

    def remove_flash() -> bool:
        button.remove_css_class("suggested-action")
        return False  # Don't repeat

    GLib.timeout_add(duration_ms, remove_flash)
