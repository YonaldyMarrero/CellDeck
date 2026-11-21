"""Teclado simulado en Linux: atajos (``ctrl+shift+t``) y escritura de texto Unicode.

- **X11** (y apps X11 bajo XWayland): extensión XTEST con ``python-xlib``, sin permisos
  especiales. Para caracteres que no están en la distribución de teclado actual (``ñ`` en un
  teclado US, emojis...) se reasigna un momento un código de tecla libre, como hace xdotool.
- **Wayland**: los compositores no permiten inyectar teclas a otras apps desde X11. Se usa
  ``ydotool`` si está instalado (necesita el servicio ``ydotoold``); si no, se explica cómo
  instalarlo.

Las combinaciones llegan como códigos virtuales de Windows (``celldeck.platform.keymap``) y
aquí se traducen a *keysyms* de X11 o a códigos ``KEY_*`` de evdev (Wayland).
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Sequence
from typing import Any

from celldeck.platform.base import PlatformError

assert sys.platform == "linux"  # noqa: S101 - mypy omite el resto del archivo en otros sistemas

log = logging.getLogger(__name__)

TYPE_DELAY_S = 0.004  # entre caracteres: algunas apps pierden pulsaciones si van muy seguidas
REMAP_DELAY_S = 0.02  # tras reasignar una tecla, el servidor X tarda un instante en avisar

# VK de Windows → nombre de keysym de X11.
VK_TO_KEYSYM: dict[int, str] = {
    0x10: "Shift_L",
    0x11: "Control_L",
    0x12: "Alt_L",
    0x5B: "Super_L",
    0x08: "BackSpace",
    0x09: "Tab",
    0x0D: "Return",
    0x13: "Pause",
    0x14: "Caps_Lock",
    0x1B: "Escape",
    0x20: "space",
    0x21: "Prior",
    0x22: "Next",
    0x23: "End",
    0x24: "Home",
    0x25: "Left",
    0x26: "Up",
    0x27: "Right",
    0x28: "Down",
    0x2C: "Print",
    0x2D: "Insert",
    0x2E: "Delete",
    0x5D: "Menu",
    0x90: "Num_Lock",
    0x91: "Scroll_Lock",
    0x6A: "KP_Multiply",
    0x6B: "KP_Add",
    0x6D: "KP_Subtract",
    0x6E: "KP_Decimal",
    0x6F: "KP_Divide",
    0xBA: "semicolon",
    0xBB: "equal",
    0xBC: "comma",
    0xBD: "minus",
    0xBE: "period",
    0xBF: "slash",
    0xC0: "grave",
    0xDB: "bracketleft",
    0xDC: "backslash",
    0xDD: "bracketright",
    0xDE: "apostrophe",
    0xAD: "XF86AudioMute",
    0xAE: "XF86AudioLowerVolume",
    0xAF: "XF86AudioRaiseVolume",
    0xB0: "XF86AudioNext",
    0xB1: "XF86AudioPrev",
    0xB2: "XF86AudioStop",
    0xB3: "XF86AudioPlay",
}
for _vk in range(0x41, 0x5B):  # letras
    VK_TO_KEYSYM[_vk] = chr(_vk).lower()
for _vk in range(0x30, 0x3A):  # dígitos
    VK_TO_KEYSYM[_vk] = chr(_vk)
for _i in range(1, 25):
    VK_TO_KEYSYM[0x6F + _i] = f"F{_i}"
for _i in range(10):
    VK_TO_KEYSYM[0x60 + _i] = f"KP_{_i}"

# VK de Windows → código KEY_* de evdev (linux/input-event-codes.h), para ydotool.
VK_TO_EVDEV: dict[int, int] = {
    0x10: 42,  # KEY_LEFTSHIFT
    0x11: 29,  # KEY_LEFTCTRL
    0x12: 56,  # KEY_LEFTALT
    0x5B: 125,  # KEY_LEFTMETA
    0x08: 14,
    0x09: 15,
    0x0D: 28,
    0x13: 119,
    0x14: 58,
    0x1B: 1,
    0x20: 57,
    0x21: 104,
    0x22: 109,
    0x23: 107,
    0x24: 102,
    0x25: 105,
    0x26: 103,
    0x27: 106,
    0x28: 108,
    0x2C: 99,
    0x2D: 110,
    0x2E: 111,
    0x5D: 127,
    0x90: 69,
    0x91: 70,
    0x6A: 55,
    0x6B: 78,
    0x6D: 74,
    0x6E: 83,
    0x6F: 98,
    0xBA: 39,
    0xBB: 13,
    0xBC: 51,
    0xBD: 12,
    0xBE: 52,
    0xBF: 53,
    0xC0: 41,
    0xDB: 26,
    0xDC: 43,
    0xDD: 27,
    0xDE: 40,
    0xAD: 113,
    0xAE: 114,
    0xAF: 115,
    0xB0: 163,
    0xB1: 165,
    0xB2: 166,
    0xB3: 164,
}
_EVDEV_LETTERS = "qwertyuiop", "asdfghjkl", "zxcvbnm"
for _row, _start in zip(_EVDEV_LETTERS, (16, 30, 44), strict=True):
    for _offset, _c in enumerate(_row):
        VK_TO_EVDEV[ord(_c.upper())] = _start + _offset
VK_TO_EVDEV[0x30] = 11
for _d in range(1, 10):
    VK_TO_EVDEV[0x30 + _d] = 1 + _d
for _i, _code in enumerate((59, 60, 61, 62, 63, 64, 65, 66, 67, 68, 87, 88), start=1):
    VK_TO_EVDEV[0x6F + _i] = _code
for _i in range(13, 25):  # KEY_F13 (183) … KEY_F24 (194)
    VK_TO_EVDEV[0x6F + _i] = 170 + _i
for _i, _code in enumerate((82, 79, 80, 81, 75, 76, 77, 71, 72, 73)):
    VK_TO_EVDEV[0x60 + _i] = _code


def session_type() -> str:
    """``x11``, ``wayland`` o ``tty``."""
    kind = os.environ.get("XDG_SESSION_TYPE", "").lower()
    if kind in ("x11", "wayland"):
        return kind
    if os.environ.get("WAYLAND_DISPLAY"):
        return "wayland"
    return "x11" if os.environ.get("DISPLAY") else "tty"


def char_keysym(char: str) -> int:
    """Keysym de un carácter: Latin-1 coincide con el código; el resto, ``0x01000000 + U``."""
    code = ord(char)
    if 0x20 <= code <= 0x7E or 0xA0 <= code <= 0xFF:
        return code
    return 0x01000000 | code


class X11Keyboard:
    """Pulsaciones con XTEST. Solo se usa desde un único hilo (Xlib no es seguro entre hilos)."""

    def __init__(self) -> None:
        from Xlib import XK, display

        XK.load_keysym_group("xf86")
        try:
            self._d: Any = display.Display()
        except Exception as exc:
            raise PlatformError("No se pudo abrir la pantalla X11 (¿DISPLAY?)") from exc
        if not self._d.has_extension("XTEST"):
            raise PlatformError("El servidor X no tiene la extensión XTEST")
        self._xk = XK
        self._scratch: int | None = None

    def close(self) -> None:
        try:
            self._d.close()
        except Exception:
            log.debug("Error cerrando la pantalla X11", exc_info=True)

    def _fake(self, keycode: int, down: bool) -> None:
        from Xlib import X
        from Xlib.ext import xtest

        xtest.fake_input(self._d, X.KeyPress if down else X.KeyRelease, keycode)

    def _lookup(self, keysym: int) -> tuple[int, bool] | None:
        """``(keycode, necesita Shift)`` si la distribución actual tiene ese keysym."""
        for keycode, index in self._d.keysym_to_keycodes(keysym):
            if index in (0, 1):
                return keycode, index == 1
        return None

    def _scratch_keycode(self) -> int:
        """Un código de tecla sin símbolos asignados, para escribir caracteres "raros"."""
        if self._scratch is None:
            info = self._d.display.info
            first, last = info.min_keycode, info.max_keycode
            mapping = self._d.get_keyboard_mapping(first, last - first + 1)
            for offset, syms in enumerate(mapping):
                if not any(syms):
                    self._scratch = first + offset
                    break
            else:
                raise PlatformError("No hay ninguna tecla libre para escribir ese carácter")
        return self._scratch

    def _keycode(self, name: str) -> int:
        keysym = self._xk.string_to_keysym(name)
        found = self._lookup(keysym) if keysym else None
        if found is None:
            raise PlatformError(f"La tecla '{name}' no existe en este teclado")
        return found[0]

    def combo(self, vks: Sequence[int]) -> None:
        keycodes = [self._keycode(VK_TO_KEYSYM[vk]) for vk in vks]
        for keycode in keycodes:
            self._fake(keycode, True)
        for keycode in reversed(keycodes):
            self._fake(keycode, False)
        self._d.sync()

    def type_text(self, text: str) -> None:
        shift = self._keycode("Shift_L")
        for char in text.replace("\r\n", "\n"):
            if char == "\n":
                self.combo([0x0D])
                continue
            if char == "\t":
                self.combo([0x09])
                continue
            keysym = char_keysym(char)
            found = self._lookup(keysym)
            if found is not None:
                keycode, needs_shift = found
                if needs_shift:
                    self._fake(shift, True)
                self._fake(keycode, True)
                self._fake(keycode, False)
                if needs_shift:
                    self._fake(shift, False)
                self._d.sync()
            else:
                self._type_remapped(keysym)
            time.sleep(TYPE_DELAY_S)

    def _type_remapped(self, keysym: int) -> None:
        keycode = self._scratch_keycode()
        self._d.change_keyboard_mapping(keycode, [(keysym, keysym)])
        self._d.sync()
        time.sleep(REMAP_DELAY_S)
        try:
            self._fake(keycode, True)
            self._fake(keycode, False)
            self._d.sync()
            time.sleep(REMAP_DELAY_S)
        finally:
            self._d.change_keyboard_mapping(keycode, [(0, 0)])
            self._d.sync()


class YdotoolKeyboard:
    """Wayland: delega en ``ydotool`` (los argumentos son fijos o números, nunca un shell)."""

    def __init__(self) -> None:
        exe = shutil.which("ydotool")
        if exe is None:
            raise PlatformError(
                "En Wayland, CellDeck necesita 'ydotool' para simular el teclado. Instálalo "
                "(sudo apt install ydotool) y activa su servicio ydotoold, o inicia sesión "
                'en una sesión X11 ("GNOME en Xorg").'
            )
        self._exe = exe

    def _run(self, args: list[str], stdin: str | None = None) -> None:
        try:
            result = subprocess.run(  # noqa: S603 - ruta absoluta y argumentos controlados
                [self._exe, *args],
                input=stdin,
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise PlatformError(f"ydotool falló: {exc}") from exc
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().splitlines()
            raise PlatformError(
                "ydotool falló" + (f": {detail[-1]}" if detail else "") + ". ¿Está ydotoold activo?"
            )

    def combo(self, vks: Sequence[int]) -> None:
        codes = [VK_TO_EVDEV[vk] for vk in vks]
        args = [f"{c}:1" for c in codes] + [f"{c}:0" for c in reversed(codes)]
        self._run(["key", *args])

    def type_text(self, text: str) -> None:
        # Por stdin: el texto no aparece en la lista de procesos ni se interpreta como opción.
        self._run(["type", "--file", "-"], stdin=text.replace("\r\n", "\n"))

    def close(self) -> None:
        pass


def create_keyboard() -> X11Keyboard | YdotoolKeyboard:
    if session_type() == "wayland":
        return YdotoolKeyboard()
    return X11Keyboard()
