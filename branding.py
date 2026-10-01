"""
Branding configuration for Saveit and white-labeled custom builds.
Provides application names, display titles, versions, icons, and descriptions.
Can be overridden dynamically via environment variables or a branding.json file.
"""

import json
import os
import sys
from pathlib import Path
from typing import Any, Dict, Optional, Union

# Default Brand Identity
DEFAULT_APP_NAME = "Saveit"
DEFAULT_APP_TITLE = "Saveit — Telegram Media Saver & Group Tutorial Forwarder"
DEFAULT_APP_SUBTITLE = "Telegram Userbot"
DEFAULT_APP_VERSION = "2.1.0"
DEFAULT_APP_AUTHOR = "DevURANIUM"
DEFAULT_APP_DESCRIPTION = "Telegram Timed Media Saver & Group Tutorial Forwarder"
DEFAULT_APP_COPYRIGHT = "Copyright © 2026"
DEFAULT_ICON_PATH = "assets/icon.ico" if sys.platform == "win32" else "assets/icon.png"

_CONFIG_CACHE: Optional[Dict[str, Any]] = None


def get_base_dir() -> Path:
    """Returns application root directory, handling PyInstaller frozen bundles."""
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent


def load_branding(force_reload: bool = False) -> Dict[str, Any]:
    """
    Loads branding metadata in priority order:
    1. Environment variables (SAVEIT_APP_NAME, etc.)
    2. branding.json file (in bundle root or project root)
    3. Built-in defaults
    """
    global _CONFIG_CACHE
    if _CONFIG_CACHE is not None and not force_reload:
        return _CONFIG_CACHE

    config = {
        "app_name": DEFAULT_APP_NAME,
        "app_title": DEFAULT_APP_TITLE,
        "app_subtitle": DEFAULT_APP_SUBTITLE,
        "app_version": DEFAULT_APP_VERSION,
        "app_author": DEFAULT_APP_AUTHOR,
        "app_description": DEFAULT_APP_DESCRIPTION,
        "app_copyright": DEFAULT_APP_COPYRIGHT,
        "icon": DEFAULT_ICON_PATH,
    }

    # Search for branding.json in bundle dir, current working dir, or module dir
    possible_paths = [
        get_base_dir() / "branding.json",
        Path.cwd() / "branding.json",
        Path(__file__).resolve().parent / "branding.json",
    ]

    for p in possible_paths:
        if p.exists() and p.is_file():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    file_data = json.load(f)
                    if isinstance(file_data, dict):
                        config.update(file_data)
                break
            except Exception:
                pass

    # Environment variable overrides
    env_mappings = {
        "SAVEIT_APP_NAME": "app_name",
        "SAVEIT_APP_TITLE": "app_title",
        "SAVEIT_APP_SUBTITLE": "app_subtitle",
        "SAVEIT_APP_VERSION": "app_version",
        "SAVEIT_APP_AUTHOR": "app_author",
        "SAVEIT_APP_DESCRIPTION": "app_description",
        "SAVEIT_APP_COPYRIGHT": "app_copyright",
        "SAVEIT_APP_ICON": "icon",
    }
    for env_k, cfg_k in env_mappings.items():
        val = os.getenv(env_k)
        if val:
            config[cfg_k] = val.strip()

    _CONFIG_CACHE = config
    return _CONFIG_CACHE


def get_name() -> str:
    return load_branding().get("app_name", DEFAULT_APP_NAME)


def get_title() -> str:
    return load_branding().get("app_title", DEFAULT_APP_TITLE)


def get_subtitle() -> str:
    return load_branding().get("app_subtitle", DEFAULT_APP_SUBTITLE)


def get_version() -> str:
    return load_branding().get("app_version", DEFAULT_APP_VERSION)


def get_author() -> str:
    return load_branding().get("app_author", DEFAULT_APP_AUTHOR)


def get_description() -> str:
    return load_branding().get("app_description", DEFAULT_APP_DESCRIPTION)


def get_copyright() -> str:
    return load_branding().get("app_copyright", DEFAULT_APP_COPYRIGHT)


def get_icon() -> Optional[str]:
    raw_icon = load_branding().get("icon")
    if not raw_icon:
        return None
    p = Path(raw_icon)
    if not p.is_absolute():
        p = get_base_dir() / p
    return str(p) if p.exists() else None


def save_branding_file(target_path: Union[str, Path], branding_data: Dict[str, Any]):
    """Writes a branding.json file."""
    p = Path(target_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        json.dump(branding_data, f, indent=4)
