"""Volumen maestro y mezclador por aplicación en Linux (PulseAudio o PipeWire con pulse).

Se usa ``pulsectl`` (ctypes sobre ``libpulse``), que habla con el servidor PulseAudio y con
``pipewire-pulse``, el que traen por defecto Ubuntu, Fedora, Zorin, Mint, etc.

Cada aplicación puede tener varios flujos (un navegador crea uno por pestaña con sonido): se
agrupan por ejecutable, igual que en Windows, y un cambio se aplica a todos.

:class:`PulseAudio` guarda una conexión que no es segura entre hilos: el backend solo la usa
desde su hilo nativo.
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
from collections.abc import Callable
from typing import Any

from celldeck.platform.audio import SYSTEM_KEY, SessionRow, app_key, group_sessions
from celldeck.platform.base import AudioApp, PlatformError, VolumeState

assert sys.platform == "linux"  # noqa: S101 - mypy omite el resto del archivo en otros sistemas

log = logging.getLogger(__name__)

SYSTEM_NAME = "Sonidos del sistema"
GONE_MESSAGE = "Esa aplicación ya no tiene audio"
NO_SERVER_MESSAGE = "No se pudo conectar con el audio (PulseAudio / PipeWire)"
CLIENT_NAME = "CellDeck"
# Flujos de avisos del escritorio (libcanberra, notificaciones): equivalen a los sonidos del
# sistema de Windows.
_EVENT_ROLES = frozenset({"event", "notification", "a11y"})


def _exe_of(pid: str | None, binary: str | None) -> str | None:
    """Ruta real del ejecutable (para su icono). ``None`` si no se puede averiguar."""
    if pid and pid.isdigit():
        try:
            return os.readlink(f"/proc/{pid}/exe")
        except OSError:
            pass
    return shutil.which(binary) if binary else None


def identify_stream(proplist: dict[str, str]) -> tuple[str, str, str | None]:
    """``(clave, nombre, ejecutable)`` de un flujo a partir de sus propiedades."""
    role = proplist.get("media.role", "")
    app_id = proplist.get("application.id", "")
    if role in _EVENT_ROLES or app_id.startswith("org.PulseAudio.libcanberra"):
        return SYSTEM_KEY, SYSTEM_NAME, None
    binary = proplist.get("application.process.binary") or None
    name = proplist.get("application.name") or binary or "Aplicación"
    identifier = proplist.get("application.flatpak.id") or binary or name
    path = _exe_of(proplist.get("application.process.id"), binary)
    return app_key(identifier), name, path


class PulseAudio:
    """Conexión perezosa con el servidor de audio, que se rehace si se cae."""

    def __init__(self, factory: Callable[[], Any] | None = None) -> None:
        self._factory = factory or self._connect
        self._pulse: Any = None

    @staticmethod
    def _connect() -> Any:
        import pulsectl

        return pulsectl.Pulse(CLIENT_NAME, connect=True)

    def _call(self, fn: Callable[[Any], Any]) -> Any:
        """Ejecuta ``fn(pulse)``; si la conexión murió (reinicio de PipeWire), reconecta."""
        for attempt in (1, 2):
            try:
                if self._pulse is None:
                    self._pulse = self._factory()
                return fn(self._pulse)
            except PlatformError:
                raise
            except Exception as exc:
                self.close()
                if attempt == 2:
                    log.debug("Fallo del servidor de audio", exc_info=True)
                    raise PlatformError(NO_SERVER_MESSAGE) from exc
        raise AssertionError("inalcanzable")

    def close(self) -> None:
        if self._pulse is not None:
            try:
                self._pulse.close()
            except Exception:
                log.debug("Error cerrando la conexión de audio", exc_info=True)
            self._pulse = None

    # ------------------------------------------------------------ maestro
    @staticmethod
    def _default_sink(pulse: Any) -> Any:
        name = pulse.server_info().default_sink_name
        if not name:
            raise PlatformError("No hay dispositivo de audio de salida")
        return pulse.get_sink_by_name(name)

    def get_volume(self) -> VolumeState:
        def read(pulse: Any) -> VolumeState:
            sink = self._default_sink(pulse)
            # PulseAudio admite más del 100 % (amplificación): el panel va de 0 a 100.
            level = max(0, min(100, round(sink.volume.value_flat * 100)))
            return VolumeState(level=level, muted=bool(sink.mute))

        result: VolumeState = self._call(read)
        return result

    def set_volume(self, level: int) -> None:
        value = max(0, min(100, level)) / 100.0
        self._call(lambda p: p.volume_set_all_chans(self._default_sink(p), value))

    def set_mute(self, muted: bool) -> None:
        self._call(lambda p: p.mute(self._default_sink(p), bool(muted)))

    # ---------------------------------------------------------- mezclador
    def _streams(self, pulse: Any) -> list[tuple[Any, tuple[str, str, str | None]]]:
        result = []
        for stream in pulse.sink_input_list():
            proplist = dict(stream.proplist or {})
            if proplist.get("application.name") == CLIENT_NAME:
                continue
            result.append((stream, identify_stream(proplist)))
        return result

    def list_apps(self) -> list[AudioApp]:
        def read(pulse: Any) -> list[AudioApp]:
            rows = [
                SessionRow(key, name, path, float(stream.volume.value_flat), bool(stream.mute))
                for stream, (key, name, path) in self._streams(pulse)
            ]
            return group_sessions(rows)

        apps: list[AudioApp] = self._call(read)
        return apps

    def _apply(self, key: str, change: Callable[[Any, Any], None]) -> None:
        def run(pulse: Any) -> None:
            targets = [stream for stream, (k, _, _) in self._streams(pulse) if k == key]
            if not targets:
                raise PlatformError(GONE_MESSAGE)
            for stream in targets:
                change(pulse, stream)

        self._call(run)

    def set_level(self, key: str, level: int) -> None:
        value = max(0, min(100, level)) / 100.0
        self._apply(key, lambda p, s: p.volume_set_all_chans(s, value))

    def set_muted(self, key: str, muted: bool) -> None:
        self._apply(key, lambda p, s: p.mute(s, bool(muted)))
