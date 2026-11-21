"""Aplicaciones en Linux: archivos ``.desktop``, iconos del tema y equivalentes de Windows.

- Las apps del catálogo (``celldeck.apps``) se buscan por su archivo ``.desktop`` en las
  carpetas XDG, incluidas las de Flatpak y Snap, y si no, por su ejecutable en el ``PATH``.
- Los iconos salen de la clave ``Icon=`` del ``.desktop`` y del tema de iconos (``hicolor``
  y compañía). PNG/XPM se leen con Pillow; los SVG (las apps de GNOME solo instalan SVG) se
  rasterizan con GdkPixbuf si PyGObject está disponible (el mismo que usa la bandeja).
- Una config hecha en Windows sigue funcionando: ``explorer.exe``, ``notepad.exe``,
  ``calc.exe``, ``cmd.exe``, ``taskmgr.exe`` y ``ms-settings:`` se traducen a la app
  equivalente del escritorio (Archivos, editor de texto, calculadora...).
"""

from __future__ import annotations

import configparser
import io
import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path, PurePath, PureWindowsPath
from typing import Any

from celldeck.apps import AppSpec
from celldeck.platform.base import AppLocation

assert sys.platform == "linux"  # noqa: S101 - mypy omite el resto del archivo en otros sistemas

log = logging.getLogger(__name__)

RASTER_EXTENSIONS = (".png", ".xpm")
ICON_EXTENSIONS = (*RASTER_EXTENSIONS, ".svg")
SCALABLE_SIZE = 10_000  # un SVG vale para cualquier tamaño
ICON_THEMES = ("hicolor", "Adwaita", "breeze", "Papirus", "Yaru", "elementary", "gnome")

# Programas de Windows → apps equivalentes (ids .desktop en orden de preferencia).
CALCULATORS = ("org.gnome.Calculator", "gnome-calculator", "org.kde.kcalc", "galculator")
SETTINGS = ("org.gnome.Settings", "gnome-control-center", "systemsettings", "xfce-settings-manager")
TERMINALS = ("org.gnome.Console", "org.gnome.Terminal", "org.kde.konsole", "xfce4-terminal")
MONITORS = ("org.gnome.SystemMonitor", "gnome-system-monitor", "org.kde.plasma-systemmonitor")
TEXT_EDITORS = ("org.gnome.TextEditor", "org.gnome.gedit", "org.kde.kate", "mousepad")
WINDOWS_PROGRAMS: dict[str, tuple[str, ...]] = {
    "calc": CALCULATORS,
    "cmd": TERMINALS,
    "powershell": TERMINALS,
    "wt": TERMINALS,
    "taskmgr": MONITORS,
    "notepad": TEXT_EDITORS,
    "control": SETTINGS,
}
FILE_MANAGER_PROGRAMS = frozenset({"explorer"})
WINDOWS_URIS: dict[str, tuple[str, ...]] = {"ms-settings": SETTINGS}


# ------------------------------------------------------------------ carpetas XDG
def data_dirs() -> list[Path]:
    """Carpetas de datos XDG (usuario primero) más las exportaciones de Flatpak y Snap."""
    home = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    system = os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share"
    dirs = [
        home,
        home / "flatpak" / "exports" / "share",
        *(Path(p) for p in system.split(":") if p),
        Path("/var/lib/flatpak/exports/share"),
        Path("/var/lib/snapd/desktop"),
    ]
    seen: list[Path] = []
    for d in dirs:
        if d not in seen:
            seen.append(d)
    return seen


def application_dirs() -> list[Path]:
    return [d / "applications" for d in data_dirs()]


@dataclass(frozen=True, slots=True)
class DesktopEntry:
    id: str
    path: Path
    name: str
    exec: str
    icon: str
    no_display: bool = False

    @property
    def exe_name(self) -> str:
        """Nombre del ejecutable de ``Exec=`` (sin ``env VAR=...`` ni argumentos)."""
        for part in self.exec.split():
            if part == "env" or "=" in part:
                continue
            return PurePath(part.strip('"')).name
        return ""


def parse_desktop(path: Path) -> DesktopEntry | None:
    parser = configparser.ConfigParser(interpolation=None, strict=False)
    parser.optionxform = str  # type: ignore[assignment,method-assign]
    try:
        parser.read(path, encoding="utf-8")
        section = parser["Desktop Entry"]
    except (configparser.Error, KeyError, OSError, UnicodeDecodeError):
        return None
    if section.get("Type", "Application") != "Application":
        return None
    return DesktopEntry(
        id=path.stem,
        path=path,
        name=section.get("Name", path.stem),
        exec=section.get("Exec", ""),
        icon=section.get("Icon", ""),
        no_display=section.get("NoDisplay", "false").lower() == "true",
    )


def find_desktop(desktop_id: str, dirs: Iterable[Path] | None = None) -> Path | None:
    name = desktop_id if desktop_id.endswith(".desktop") else f"{desktop_id}.desktop"
    for folder in dirs if dirs is not None else application_dirs():
        candidate = folder / name
        if candidate.is_file():
            return candidate
    return None


def first_desktop(ids: Iterable[str], dirs: Iterable[Path] | None = None) -> Path | None:
    folders = list(dirs) if dirs is not None else application_dirs()
    for desktop_id in ids:
        found = find_desktop(desktop_id, folders)
        if found is not None:
            return found
    return None


@lru_cache(maxsize=1)
def _desktop_index() -> tuple[DesktopEntry, ...]:
    entries: dict[str, DesktopEntry] = {}
    for folder in application_dirs():
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.desktop")):
            if path.stem in entries:  # el de la carpeta del usuario manda
                continue
            entry = parse_desktop(path)
            if entry is not None:
                entries[path.stem] = entry
    return tuple(entries.values())


def forget_index() -> None:
    _desktop_index.cache_clear()


def desktop_for_exe(exe: str) -> DesktopEntry | None:
    """El ``.desktop`` cuyo ``Exec=`` lanza ese ejecutable (para sacar su icono)."""
    name = PurePath(exe).name
    if not name:
        return None
    matches = [e for e in _desktop_index() if e.exe_name == name]
    matches.sort(key=lambda e: e.no_display)  # mejor la entrada visible del menú
    return matches[0] if matches else None


def default_handler(mime: str) -> Path | None:
    """``.desktop`` predeterminado para un tipo MIME o ``x-scheme-handler/<esquema>``."""
    exe = shutil.which("xdg-mime")
    if exe is None:
        return None
    try:
        result = subprocess.run(  # noqa: S603 - ruta absoluta y argumentos fijos
            [exe, "query", "default", mime],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    desktop_id = result.stdout.strip()
    return find_desktop(desktop_id) if desktop_id else None


# ----------------------------------------------------------------- catálogo
def locate(spec: AppSpec) -> AppLocation | None:
    desktop = first_desktop(spec.desktop_ids)
    if desktop is not None:
        return AppLocation(str(desktop), None, "desktop")
    for exe_name in spec.linux_exe_names:
        found = shutil.which(exe_name)
        if found:
            return AppLocation(found, None, "PATH")
    if spec.scheme and default_handler(f"x-scheme-handler/{spec.scheme}") is not None:
        return AppLocation(None, spec.uri, "protocolo")
    return None


# ----------------------------------------------------- equivalentes de Windows
@dataclass(frozen=True, slots=True)
class Translated:
    """Qué abrir en Linux para un programa o URI de Windows."""

    desktop: Path | None = None
    folder: Path | None = None


def translate_windows_program(target: str) -> Translated | None:
    """``explorer.exe`` → la carpeta personal; ``calc.exe`` → la calculadora; etc."""
    if "/" in target:
        return None  # una ruta de Linux nunca es un programa de Windows
    stem = PureWindowsPath(target).name.lower().removesuffix(".exe")
    if stem in FILE_MANAGER_PROGRAMS:
        return Translated(folder=Path.home())
    ids = WINDOWS_PROGRAMS.get(stem)
    if ids is None:
        return None
    desktop = first_desktop(ids)
    if desktop is None and stem == "notepad":
        desktop = default_handler("text/plain")
    return Translated(desktop=desktop) if desktop else None


def translate_windows_uri(uri: str) -> Path | None:
    scheme = uri.split(":", 1)[0].lower()
    ids = WINDOWS_URIS.get(scheme)
    return first_desktop(ids) if ids else None


# --------------------------------------------------------------------- iconos
def icon_dirs() -> list[Path]:
    return [Path.home() / ".icons", *(d / "icons" for d in data_dirs())]


def _size_of(folder: str) -> int:
    if folder in ("scalable", "symbolic"):
        return SCALABLE_SIZE if folder == "scalable" else 0
    head = folder.split("x", 1)[0].split("@", 1)[0]
    return int(head) if head.isdigit() else 0


def resolve_icon(name: str, size: int = 160) -> Path | None:
    """Archivo del icono ``name``: el más pequeño que llegue a ``size`` o el mayor.

    Con el mismo tamaño se prefiere un formato de mapa de bits (no hay que rasterizarlo), y
    los SVG solo cuentan si se pueden rasterizar.
    """
    extensions = ICON_EXTENSIONS if svg_supported() else RASTER_EXTENSIONS
    if not name:
        return None
    direct = Path(name)
    if direct.is_absolute():
        return direct if direct.is_file() and direct.suffix.lower() in extensions else None
    candidates: list[tuple[int, int, Path]] = []
    for base in icon_dirs():
        if not base.is_dir():
            continue
        themes = [t for t in ICON_THEMES if (base / t).is_dir()]
        for theme in themes:
            for size_dir in (base / theme).iterdir():
                apps = size_dir / "apps"
                for rank, ext in enumerate(extensions):
                    icon = apps / f"{name}{ext}"
                    if icon.is_file():
                        candidates.append((_size_of(size_dir.name), rank, icon))
        if candidates:
            break
    if not candidates:
        for ext in extensions:
            for folder in [d / "pixmaps" for d in data_dirs()]:
                icon = folder / f"{name}{ext}"
                if icon.is_file():
                    return icon
        return None
    big_enough = [c for c in candidates if c[0] >= size]
    if big_enough:
        return min(big_enough)[2]
    return max(candidates, key=lambda c: (c[0], -c[1]))[2]


def target_desktop(target: str) -> DesktopEntry | None:
    """``.desktop`` de lo que abre un botón: un ``.desktop``, un ejecutable o una URI."""
    if target.endswith(".desktop"):
        return parse_desktop(Path(target))
    translated = translate_windows_program(target)
    if translated is not None:
        desktop = translated.desktop or default_handler("inode/directory")
        return parse_desktop(desktop) if desktop else None
    if ":" in target and not target.startswith("/"):
        scheme = target.split(":", 1)[0].lower()
        handler = translate_windows_uri(target) or default_handler(f"x-scheme-handler/{scheme}")
        return parse_desktop(handler) if handler else None
    path = shutil.which(target) or target
    return desktop_for_exe(path)


def icon_png(target: str, size: int = 160) -> bytes | None:
    entry = target_desktop(target)
    icon = resolve_icon(entry.icon, size) if entry else None
    if icon is None:
        return None
    if icon.suffix.lower() == ".svg":
        return render_svg(icon, size)
    from PIL import Image

    with Image.open(icon) as img:
        rgba = img.convert("RGBA")
        if rgba.width != size:
            rgba = rgba.resize((size, size), Image.Resampling.LANCZOS)
        buf = io.BytesIO()
        rgba.save(buf, format="PNG")
        return buf.getvalue()


@lru_cache(maxsize=1)
def _pixbuf() -> Any:
    """``GdkPixbuf`` de PyGObject, o ``None`` si no está instalado."""
    try:
        import gi

        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return None
    return GdkPixbuf


def svg_supported() -> bool:
    return _pixbuf() is not None


def render_svg(path: Path, size: int) -> bytes | None:
    pixbuf = _pixbuf()
    if pixbuf is None:
        return None
    try:
        image = pixbuf.Pixbuf.new_from_file_at_size(str(path), size, size)
        ok, data = image.save_to_bufferv("png", [], [])
    except Exception:  # GLib.Error: SVG dañado o sin el cargador de librsvg
        log.debug("No se pudo rasterizar %s", path, exc_info=True)
        return None
    return bytes(data) if ok else None
