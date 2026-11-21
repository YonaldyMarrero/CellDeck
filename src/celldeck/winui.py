"""Pequeñas utilidades de interfaz nativa: cuadros de mensaje e instancia única.

- Windows: ``MessageBoxW`` y un mutex con nombre.
- Linux: ``zenity`` (GNOME, Cinnamon, XFCE...) o ``kdialog`` (KDE), y un ``flock`` sobre un
  archivo de ``$XDG_RUNTIME_DIR``. Sin ninguno de los dos programas, o sin escritorio (CI),
  los mensajes van al log y las preguntas responden "no" (opción segura).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

MB_OK = 0x0
MB_YESNO = 0x4
MB_ICONERROR = 0x10
MB_ICONWARNING = 0x30
MB_ICONINFO = 0x40
MB_SETFOREGROUND = 0x10000
MB_TOPMOST = 0x40000
IDYES = 6
ERROR_ALREADY_EXISTS = 183

_mutex: Any = None  # se mantiene vivo durante toda la ejecución


def _linux_dialog(text: str, title: str, flags: int) -> int:
    """Muestra el diálogo con zenity o kdialog. Devuelve ``IDYES`` si se aceptó."""
    question = flags & MB_YESNO
    if flags & MB_ICONERROR:
        kind = ("--error", "--error")
    elif question:
        kind = ("--question", "--warningyesno" if flags & MB_ICONWARNING else "--yesno")
    else:
        kind = ("--warning" if flags & MB_ICONWARNING else "--info", "--msgbox")
    if zenity := shutil.which("zenity"):
        args = [zenity, kind[0], f"--title={title}", f"--text={text}", "--no-markup"]
        if question:
            args += ["--ok-label=Sí", "--cancel-label=No"]
    elif kdialog := shutil.which("kdialog"):
        args = [kdialog, "--title", title, kind[1], text]
    else:
        log.warning("[%s] %s", title, text)
        return 0
    try:
        result = subprocess.run(args, check=False, timeout=600)  # noqa: S603 - sin shell
    except (OSError, subprocess.TimeoutExpired):
        log.warning("[%s] %s", title, text)
        return 0
    return IDYES if result.returncode == 0 and question else 0


def _message_box(text: str, title: str, flags: int) -> int:
    if sys.platform == "linux" and (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return _linux_dialog(text, title, flags)
    if sys.platform != "win32":
        log.warning("[%s] %s", title, text)
        return 0
    import ctypes

    result: int = ctypes.windll.user32.MessageBoxW(
        None, text, title, flags | MB_SETFOREGROUND | MB_TOPMOST
    )
    return result


def info(text: str, title: str = "CellDeck") -> None:
    _message_box(text, title, MB_OK | MB_ICONINFO)


def error(text: str, title: str = "CellDeck") -> None:
    _message_box(text, title, MB_OK | MB_ICONERROR)


def ask(text: str, title: str = "CellDeck", warning: bool = False) -> bool:
    """Pregunta Sí/No. Sin forma de preguntar devuelve False (opción segura)."""
    flags = MB_YESNO | (MB_ICONWARNING if warning else MB_ICONINFO)
    return _message_box(text, title, flags) == IDYES


def acquire_single_instance(name: str = "Local\\CellDeck") -> bool:
    """Crea un mutex con nombre. ``False`` si otra instancia ya lo tiene."""
    global _mutex
    if sys.platform == "linux":
        return _linux_single_instance()
    if sys.platform != "win32":
        return True
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    handle = kernel32.CreateMutexW(None, False, name)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        kernel32.CloseHandle(ctypes.c_void_p(handle))
        return False
    _mutex = handle
    return True


def _linux_single_instance() -> bool:
    """Bloqueo exclusivo sobre un archivo: lo libera el sistema si el proceso muere."""
    global _mutex
    if sys.platform == "win32":  # nunca ocurre; así mypy en Windows no analiza fcntl
        return True
    import fcntl

    runtime = os.environ.get("XDG_RUNTIME_DIR")
    folder = Path(runtime) if runtime else Path.home() / ".cache"
    try:
        folder.mkdir(parents=True, exist_ok=True)
        handle = (folder / "celldeck.lock").open("a")
    except OSError:
        log.debug("No se pudo crear el archivo de instancia única", exc_info=True)
        return True
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        handle.close()
        return False
    _mutex = handle
    return True
