"""Implementación real de ``PlatformBackend`` para Linux.

- Volumen y mezclador por aplicación: PulseAudio / PipeWire (``linux_audio``).
- Ahora suena y teclas multimedia: MPRIS por D-Bus (``linux_mpris``).
- Teclado: XTEST en X11, ``ydotool`` en Wayland (``linux_input``).
- Programas, carpetas y URIs: ``gio`` / ``xdg-open`` y archivos ``.desktop`` (``linux_apps``).
- Sistema: ``loginctl`` y ``systemctl`` (los permisos los decide polkit, como en el menú
  del escritorio: un usuario local puede suspender o apagar sin contraseña).

Nunca se invoca un shell: los programas se lanzan con su ruta y una lista de argumentos.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import time
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

from celldeck.platform.base import (
    AppLocation,
    AudioApp,
    MediaInfo,
    MediaKey,
    PlatformError,
    SystemCommand,
    VolumeState,
)
from celldeck.platform.keymap import KeyCombo

if sys.platform != "linux":  # pragma: no cover - solo se importa en Linux
    raise ImportError("celldeck.platform.linux solo funciona en Linux")

from celldeck.platform import linux_apps, linux_input
from celldeck.platform.linux_audio import PulseAudio
from celldeck.platform.linux_mpris import NO_PLAYER_MESSAGE, Mpris

assert sys.platform == "linux"  # noqa: S101 - mypy omite el resto del archivo en otros sistemas

log = logging.getLogger(__name__)
T = TypeVar("T")

APP_CACHE_TTL_S = 300.0
# Los lanzadores (gio, xdg-open, systemctl) terminan enseguida y su código dice si fallaron.
LAUNCH_CHECK_S = 5.0
# Un programa lanzado directamente sigue en marcha: solo se espera a ver si muere al arrancar.
PROGRAM_CHECK_S = 0.3
MEDIA_VOLUME_STEP = 2  # como cada pulsación de la tecla de volumen en Windows

_MPRIS_METHODS = {
    MediaKey.PLAY_PAUSE: "PlayPause",
    MediaKey.NEXT: "Next",
    MediaKey.PREV: "Previous",
    MediaKey.STOP: "Stop",
}
_MEDIA_VKS = {
    MediaKey.PLAY_PAUSE: 0xB3,
    MediaKey.NEXT: 0xB0,
    MediaKey.PREV: 0xB1,
    MediaKey.STOP: 0xB2,
}


def _which(name: str) -> str:
    found = shutil.which(name)
    if found is None:
        raise PlatformError(f"No se encontró el programa '{name}' en este sistema")
    return found


async def spawn(args: Sequence[str], cwd: str | None = None, wait: float = LAUNCH_CHECK_S) -> None:
    """Lanza un proceso desacoplado de CellDeck y comprueba que no falle al arrancar.

    El proceso hijo sigue vivo aunque CellDeck se cierre (nueva sesión) y no hereda su
    entrada/salida: con un pipe, la app que abre ``gio``/``xdg-open`` lo heredaría y podría
    bloquearse al llenarlo. Si termina con error en los primeros ``wait`` segundos, se
    informa; si sigue en marcha, es el propio programa y se da por abierto.
    """
    try:
        proc = await asyncio.create_subprocess_exec(
            *args,
            cwd=cwd or None,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        raise PlatformError(f"No se pudo abrir '{args[0]}': {exc.strerror or exc}") from exc
    try:
        code = await asyncio.wait_for(proc.wait(), timeout=wait)
    except TimeoutError:
        log.info("Abierto: %s", " ".join(args))
        return
    if code != 0:
        raise PlatformError(f"No se pudo abrir '{Path(args[-1]).name}' (código {code})")
    log.info("Abierto: %s", " ".join(args))


class LinuxBackend:
    """Backend nativo de Linux (X11 y Wayland; GNOME, KDE, XFCE, Cinnamon...)."""

    name = "linux"

    def __init__(self) -> None:
        # Un hilo para audio y D-Bus, y otro para el teclado: escribir un texto largo no
        # debe congelar el slider de volumen. Ninguna de esas conexiones es segura entre hilos.
        self._native = ThreadPoolExecutor(max_workers=1, thread_name_prefix="celldeck-native")
        self._input = ThreadPoolExecutor(max_workers=1, thread_name_prefix="celldeck-input")
        self._pulse = PulseAudio()
        self._mpris = Mpris()
        self._keyboard: Any = None
        self._app_cache: dict[str, tuple[float, AppLocation | None]] = {}
        self._icon_cache: dict[str, bytes | None] = {}

    async def _on(self, pool: ThreadPoolExecutor, fn: Callable[[], T]) -> T:
        return await asyncio.get_running_loop().run_in_executor(pool, fn)

    # ---------------------------------------------------------------- teclado
    def _kbd(self) -> Any:
        if self._keyboard is None:
            self._keyboard = linux_input.create_keyboard()
        return self._keyboard

    async def send_media_key(self, key: MediaKey) -> None:
        if key is MediaKey.MUTE:
            current = await self.get_volume()
            await self.set_mute(not current.muted)
            return
        if key in (MediaKey.VOLUME_UP, MediaKey.VOLUME_DOWN):
            step = MEDIA_VOLUME_STEP if key is MediaKey.VOLUME_UP else -MEDIA_VOLUME_STEP
            current = await self.get_volume()
            await self.set_volume(current.level + step)
            return
        # MPRIS habla directamente con el reproductor: funciona también en Wayland y sin foco.
        if await self._on(self._native, lambda: self._mpris.command(_MPRIS_METHODS[key])):
            return
        try:
            await self._on(self._input, lambda: self._kbd().combo([_MEDIA_VKS[key]]))
        except PlatformError:
            raise PlatformError(NO_PLAYER_MESSAGE) from None

    async def send_combo(self, combo: KeyCombo) -> None:
        await self._on(self._input, lambda: self._kbd().combo(combo.keys))

    async def type_text(self, text: str) -> None:
        await self._on(self._input, lambda: self._kbd().type_text(text))

    # ------------------------------------------------------ programas y URIs
    async def open_path(self, path: str, args: Sequence[str] = (), cwd: str | None = None) -> None:
        # Consultar el disco (expanduser, isdir...) no debe frenar el bucle de eventos.
        kind, target, work_dir = await asyncio.to_thread(self._plan_open, path, cwd)
        extra = [os.path.expandvars(a) for a in args]
        if kind == "desktop":
            await self._launch_desktop(Path(target), extra)
        elif kind == "default":
            await self._open_with_default(target)
        elif kind == "program":
            await spawn([target, *extra], work_dir, wait=PROGRAM_CHECK_S)
        else:
            raise PlatformError(f"No se encontró '{path}'")

    @staticmethod
    def _plan_open(path: str, cwd: str | None) -> tuple[str, str, str | None]:
        """``(cómo, qué, carpeta)``: ``desktop``, ``default``, ``program`` o ``missing``."""
        target = os.path.expandvars(os.path.expanduser(path.strip()))
        work_dir = os.path.expandvars(os.path.expanduser(cwd)) if cwd else None
        translated = linux_apps.translate_windows_program(target)
        if translated is not None:
            if translated.folder is not None:
                return "default", str(translated.folder), None
            if translated.desktop is not None:
                return "desktop", str(translated.desktop), None
        if target.endswith(".desktop"):
            return "desktop", target, None
        if os.path.isdir(target):
            return "default", target, None
        if os.path.isfile(target) and os.access(target, os.X_OK):
            return "program", target, work_dir
        if "/" not in target and (found := shutil.which(target)):
            return "program", found, work_dir
        if os.path.exists(target):
            return "default", target, None  # documento: con su app predeterminada
        return "missing", target, None

    async def open_uri(self, uri: str) -> None:
        settings = linux_apps.translate_windows_uri(uri)
        if settings is not None:
            await self._launch_desktop(settings, [])
            return
        await self._open_with_default(uri)

    async def _open_with_default(self, target: str) -> None:
        if gio := shutil.which("gio"):
            await spawn([gio, "open", target])
        else:
            await spawn([_which("xdg-open"), target])

    async def _launch_desktop(self, desktop: Path, args: Sequence[str]) -> None:
        if gio := shutil.which("gio"):
            await spawn([gio, "launch", str(desktop), *args])
        elif gtk_launch := shutil.which("gtk-launch"):
            await spawn([gtk_launch, desktop.stem, *args])
        else:
            raise PlatformError("Para abrir aplicaciones hace falta 'gio' o 'gtk-launch'")

    # ---------------------------------------------------------------- volumen
    async def get_volume(self) -> VolumeState:
        return await self._on(self._native, self._pulse.get_volume)

    async def set_volume(self, level: int) -> None:
        await self._on(self._native, lambda: self._pulse.set_volume(level))

    async def set_mute(self, muted: bool) -> None:
        await self._on(self._native, lambda: self._pulse.set_mute(muted))

    async def list_audio_apps(self) -> list[AudioApp]:
        return await self._on(self._native, self._pulse.list_apps)

    async def set_app_volume(self, key: str, level: int) -> None:
        await self._on(self._native, lambda: self._pulse.set_level(key, level))

    async def set_app_mute(self, key: str, muted: bool) -> None:
        await self._on(self._native, lambda: self._pulse.set_muted(key, muted))

    # ------------------------------------------------------------ ahora suena
    async def get_now_playing(self) -> MediaInfo | None:
        try:
            return await self._on(self._native, self._mpris.now_playing)
        except PlatformError:
            return None

    # ---------------------------------------------------------------- sistema
    async def system_command(self, command: SystemCommand) -> None:
        if command is SystemCommand.LOCK:
            await self._first_working(
                [
                    ["loginctl", "lock-session"],
                    ["xdg-screensaver", "lock"],
                ],
                "No se pudo bloquear la sesión",
            )
        elif command is SystemCommand.MONITOR_OFF:
            if linux_input.session_type() != "x11" or not shutil.which("xset"):
                raise PlatformError("Apagar la pantalla solo está disponible en sesiones X11")
            await spawn([_which("xset"), "dpms", "force", "off"], wait=3)
        elif command is SystemCommand.LOGOFF:
            session = os.environ.get("XDG_SESSION_ID", "")
            await self._first_working(
                [
                    ["gnome-session-quit", "--logout", "--no-prompt"],
                    ["qdbus", "org.kde.ksmserver", "/KSMServer", "logout", "0", "0", "0"],
                    *([["loginctl", "terminate-session", session]] if session else []),
                ],
                "No se pudo cerrar la sesión",
            )
        else:
            verb = {
                SystemCommand.SLEEP: "suspend",
                SystemCommand.HIBERNATE: "hibernate",
                SystemCommand.SHUTDOWN: "poweroff",
                SystemCommand.RESTART: "reboot",
            }[command]
            # Argumentos fijos: nunca se construyen desde datos de la red.
            await spawn([_which("systemctl"), verb], wait=5)
        log.info("Comando de sistema ejecutado: %s", command.value)

    @staticmethod
    async def _first_working(options: list[list[str]], message: str) -> None:
        errors: list[str] = []
        for option in options:
            exe = shutil.which(option[0])
            if exe is None:
                continue
            try:
                await spawn([exe, *option[1:]], wait=5)
                return
            except PlatformError as exc:
                errors.append(str(exc))
        raise PlatformError(message + (f" ({errors[-1]})" if errors else ""))

    # ------------------------------------------------------ apps e iconos
    async def find_app(self, app_id: str) -> AppLocation | None:
        from celldeck.apps import get_app

        spec = get_app(app_id)
        if spec is None:
            return None
        cached = self._app_cache.get(app_id)
        if cached is not None and time.monotonic() - cached[0] < APP_CACHE_TTL_S:
            return cached[1]
        location = await asyncio.to_thread(linux_apps.locate, spec)
        self._app_cache[app_id] = (time.monotonic(), location)
        log.debug("App %s: %s", app_id, location)
        return location

    def forget_app(self, app_id: str) -> None:
        self._app_cache.pop(app_id, None)
        linux_apps.forget_index()

    async def target_icon(self, target: str, size: int = 160) -> bytes | None:
        key = f"{target}|{size}"
        if key not in self._icon_cache:
            try:
                self._icon_cache[key] = await asyncio.to_thread(linux_apps.icon_png, target, size)
            except (OSError, ValueError):
                log.debug("No se pudo obtener el icono de %s", target, exc_info=True)
                self._icon_cache[key] = None
        return self._icon_cache[key]

    async def close(self) -> None:
        def shutdown() -> None:
            self._pulse.close()
            self._mpris.close()

        try:
            await self._on(self._native, shutdown)
        finally:
            if self._keyboard is not None:
                keyboard = self._keyboard
                self._input.submit(keyboard.close)
            self._native.shutdown(wait=False, cancel_futures=True)
            self._input.shutdown(wait=False, cancel_futures=True)
