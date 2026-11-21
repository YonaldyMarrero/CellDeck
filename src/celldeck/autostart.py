"""Iniciar CellDeck al iniciar sesión.

- Windows: clave ``Run`` de HKCU (no requiere administrador).
- Linux: ``~/.config/autostart/celldeck.desktop`` (estándar XDG: GNOME, KDE, XFCE...).
"""

from __future__ import annotations

import contextlib
import logging
import os
import shlex
import sys
from pathlib import Path

from celldeck.paths import is_frozen

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CellDeck"
LEGACY_VALUE_NAME = "PhoneDeck"  # nombre anterior del proyecto


DESKTOP_NAME = "celldeck.desktop"


def is_supported() -> bool:
    """Solo empaquetado: en desarrollo no hay un ejecutable estable que registrar."""
    if sys.platform == "linux":
        return is_frozen() or bool(os.environ.get("APPIMAGE"))
    return sys.platform == "win32" and is_frozen()


def executable() -> str:
    # Dentro de un AppImage, sys.executable vive en un montaje temporal: vale $APPIMAGE.
    return os.environ.get("APPIMAGE") or sys.executable


def command() -> str:
    return f'"{executable()}"'


# ---------------------------------------------------------------------- Linux
def autostart_file() -> Path:
    config = os.environ.get("XDG_CONFIG_HOME")
    return (Path(config) if config else Path.home() / ".config") / "autostart" / DESKTOP_NAME


def desktop_entry() -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=CellDeck\n"
        "Comment=Panel de control para tu PC desde el celular\n"
        f"Exec={shlex.quote(executable())}\n"
        "Icon=celldeck\n"
        "Terminal=false\n"
        "X-GNOME-Autostart-enabled=true\n"
    )


def _linux_is_enabled() -> bool:
    try:
        text = autostart_file().read_text(encoding="utf-8")
    except OSError:
        return False
    return "X-GNOME-Autostart-enabled=false" not in text and "Hidden=true" not in text


def _linux_set_enabled(enabled: bool) -> None:
    from celldeck.fileutil import atomic_write_text

    path = autostart_file()
    if enabled:
        path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, desktop_entry())
        log.info("Inicio automático activado: %s", path)
    else:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        log.info("Inicio automático desactivado")


def _linux_refresh() -> None:
    """Si el AppImage o el binario se movió de carpeta, actualiza la ruta registrada."""
    if not (is_supported() and _linux_is_enabled()):
        return
    try:
        if autostart_file().read_text(encoding="utf-8") != desktop_entry():
            _linux_set_enabled(True)
    except OSError:
        log.debug("No se pudo comprobar el inicio automático", exc_info=True)


# -------------------------------------------------------------------- Windows
def is_enabled() -> bool:
    if sys.platform == "linux":
        return _linux_is_enabled()
    if sys.platform != "win32":
        return False
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return bool(value)
    except OSError:
        return False


def set_enabled(enabled: bool) -> None:
    if sys.platform == "linux":
        _linux_set_enabled(enabled)
        return
    if sys.platform != "win32":
        raise OSError("Solo disponible en Windows y Linux")
    import winreg

    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command())
            log.info("Inicio con Windows activado: %s", command())
        else:
            with contextlib.suppress(FileNotFoundError):
                winreg.DeleteValue(key, VALUE_NAME)
            log.info("Inicio con Windows desactivado")


def migrate_legacy() -> None:
    """Sustituye la entrada "PhoneDeck" de la clave Run por la de CellDeck.

    Si el usuario tenía activado el inicio con Windows con el nombre anterior, la entrada vieja
    apuntaría a PhoneDeck.exe y se abrirían los dos. Se borra y se registra este ejecutable.
    """
    if sys.platform != "win32" or not is_supported():
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_ALL_ACCESS) as key:
            try:
                winreg.QueryValueEx(key, LEGACY_VALUE_NAME)
            except FileNotFoundError:
                return
            winreg.DeleteValue(key, LEGACY_VALUE_NAME)
        set_enabled(True)
        log.info("Inicio con Windows migrado de PhoneDeck a CellDeck")
    except OSError:
        log.warning("No se pudo migrar la entrada de inicio de PhoneDeck", exc_info=True)


def refresh_path() -> None:
    """Si el .exe se movió de carpeta, actualiza la ruta registrada."""
    if sys.platform == "linux":
        _linux_refresh()
        return
    migrate_legacy()
    if sys.platform != "win32" or not (is_supported() and is_enabled()):
        return
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
        if value != command():
            set_enabled(True)
    except OSError:
        log.debug("No se pudo comprobar la clave de inicio", exc_info=True)
