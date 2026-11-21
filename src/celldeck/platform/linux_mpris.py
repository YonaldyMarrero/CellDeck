""" "Ahora suena" y controles multimedia en Linux con MPRIS (D-Bus).

Spotify, Firefox, Chrome/Brave (YouTube), VLC, Rhythmbox, mpv (con mpv-mpris)... publican
``org.mpris.MediaPlayer2.<nombre>`` en el bus de sesión. Es el equivalente a los
"controles multimedia del sistema" de Windows, y no depende de X11 ni de Wayland.

Se usa ``jeepney`` (D-Bus en Python puro, sin compilar nada). La conexión no es segura entre
hilos: el backend solo la usa desde su hilo nativo.
"""

from __future__ import annotations

import hashlib
import logging
import sys
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from celldeck.platform.base import MediaInfo, PlatformError

assert sys.platform == "linux"  # noqa: S101 - mypy omite el resto del archivo en otros sistemas

log = logging.getLogger(__name__)

MPRIS_PREFIX = "org.mpris.MediaPlayer2."
MPRIS_PATH = "/org/mpris/MediaPlayer2"
PLAYER_IFACE = "org.mpris.MediaPlayer2.Player"
CALL_TIMEOUT_S = 2.0
MAX_ARTWORK_BYTES = 5 * 1024 * 1024
ARTWORK_TIMEOUT_S = 3.0
NO_PLAYER_MESSAGE = "No hay ningún reproductor abierto"

_STATUS = {"Playing": "playing", "Paused": "paused", "Stopped": "stopped"}
# Prioridad al elegir reproductor: el que suena gana al pausado, y este al detenido.
_RANK = {"playing": 0, "paused": 1, "stopped": 2, "unknown": 3}


def unwrap(value: Any) -> Any:
    """jeepney entrega los ``variant`` como ``(firma, valor)``: se queda con el valor."""
    if isinstance(value, tuple) and len(value) == 2 and isinstance(value[0], str):
        return unwrap(value[1])
    return value


@dataclass(frozen=True, slots=True)
class PlayerState:
    bus_name: str
    status: str
    metadata: dict[str, Any]
    position_us: int | None


def parse_media(state: PlayerState) -> tuple[MediaInfo, str]:
    """``MediaInfo`` sin portada y la URL de la portada (``mpris:artUrl``) del reproductor."""
    meta = {key: unwrap(value) for key, value in state.metadata.items()}
    artists = meta.get("xesam:artist") or []
    artist = ", ".join(str(a) for a in artists) if isinstance(artists, list) else str(artists)
    length = meta.get("mpris:length")
    duration = int(length) / 1e6 if isinstance(length, int) and length > 0 else None
    position = (
        state.position_us / 1e6 if duration is not None and state.position_us is not None else None
    )
    info = MediaInfo(
        title=str(meta.get("xesam:title") or ""),
        artist=artist,
        album=str(meta.get("xesam:album") or ""),
        app_id=state.bus_name.removeprefix(MPRIS_PREFIX).split(".instance")[0],
        status=state.status,
        position_s=position,
        duration_s=duration,
    )
    return info, str(meta.get("mpris:artUrl") or "")


def pick_player(players: list[PlayerState], last: str | None) -> PlayerState | None:
    """El que suena; si ninguno, el último usado; si no, el primero (orden estable)."""
    if not players:
        return None
    best = min(_RANK.get(p.status, 3) for p in players)
    candidates = [p for p in players if _RANK.get(p.status, 3) == best]
    for player in candidates:
        if player.bus_name == last:
            return player
    return sorted(candidates, key=lambda p: p.bus_name)[0]


def read_artwork(url: str, opener: Callable[..., Any] = urllib.request.urlopen) -> bytes | None:
    """Lee la portada de ``file://`` o ``http(s)://``. Otros esquemas se ignoran."""
    parsed = urllib.parse.urlparse(url)
    try:
        if parsed.scheme == "file":
            path = Path(urllib.parse.unquote(parsed.path))
            if not path.is_file() or path.stat().st_size > MAX_ARTWORK_BYTES:
                return None
            return path.read_bytes()
        if parsed.scheme in ("http", "https"):
            # La URL la da el reproductor local (p. ej. el CDN de Spotify), no el celular.
            with opener(url, timeout=ARTWORK_TIMEOUT_S) as res:
                data: bytes = res.read(MAX_ARTWORK_BYTES + 1)
            return data if len(data) <= MAX_ARTWORK_BYTES else None
    except (OSError, ValueError):
        log.debug("No se pudo leer la portada %s", url, exc_info=True)
    return None


class Mpris:
    """Consulta y controla los reproductores MPRIS del bus de sesión."""

    def __init__(self) -> None:
        self._conn: Any = None
        self._last: str | None = None
        self._artwork: tuple[str, bytes | None] | None = None

    def _connection(self) -> Any:
        if self._conn is None:
            from jeepney.io.blocking import open_dbus_connection

            try:
                self._conn = open_dbus_connection(bus="SESSION")
            except Exception as exc:
                raise PlatformError("No se pudo conectar con el bus de sesión (D-Bus)") from exc
        return self._conn

    def _call(self, bus_name: str, path: str, iface: str, method: str, *args: Any) -> Any:
        from jeepney import DBusAddress, new_method_call

        signature = {
            ("org.freedesktop.DBus.Properties", "GetAll"): "s",
            ("org.freedesktop.DBus.Properties", "Get"): "ss",
        }.get((iface, method))
        addr = DBusAddress(path, bus_name=bus_name, interface=iface)
        msg = (
            new_method_call(addr, method, signature, args)
            if signature
            else (new_method_call(addr, method))
        )
        try:
            reply = self._connection().send_and_get_reply(msg, timeout=CALL_TIMEOUT_S)
        except Exception:
            self.close()  # conexión rota o reproductor colgado: se rehace en la siguiente
            raise
        from jeepney import MessageType

        if reply.header.message_type == MessageType.error:
            raise PlatformError(f"El reproductor respondió con un error: {reply.body!r}")
        return reply.body

    def close(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                log.debug("Error cerrando D-Bus", exc_info=True)
            self._conn = None

    # ---------------------------------------------------------------- lectura
    def _bus_names(self) -> list[str]:
        body = self._call(
            "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "ListNames"
        )
        return sorted(n for n in body[0] if n.startswith(MPRIS_PREFIX))

    def _state(self, bus_name: str) -> PlayerState | None:
        try:
            props = self._call(
                bus_name, MPRIS_PATH, "org.freedesktop.DBus.Properties", "GetAll", PLAYER_IFACE
            )[0]
        except Exception:
            log.debug("Reproductor %s no responde", bus_name, exc_info=True)
            return None
        status = _STATUS.get(str(unwrap(props.get("PlaybackStatus", ""))), "unknown")
        metadata = unwrap(props.get("Metadata")) or {}
        position = unwrap(props.get("Position"))
        return PlayerState(
            bus_name,
            status,
            metadata if isinstance(metadata, dict) else {},
            int(position) if isinstance(position, int) else None,
        )

    def _current(self) -> PlayerState | None:
        players = [s for s in (self._state(n) for n in self._bus_names()) if s is not None]
        player = pick_player(players, self._last)
        if player is not None:
            self._last = player.bus_name
        return player

    def now_playing(self) -> MediaInfo | None:
        player = self._current()
        if player is None:
            return None
        info, art_url = parse_media(player)
        if not (info.title or info.artist):
            return None
        artwork = self._cached_artwork(art_url) if art_url else None
        if artwork is None:
            return info
        digest = hashlib.sha1(artwork, usedforsecurity=False).hexdigest()[:12]
        return MediaInfo(
            title=info.title,
            artist=info.artist,
            album=info.album,
            app_id=info.app_id,
            status=info.status,
            position_s=info.position_s,
            duration_s=info.duration_s,
            artwork=artwork,
            artwork_hash=digest,
        )

    def _cached_artwork(self, url: str) -> bytes | None:
        """La portada se lee una vez por URL, no en cada sondeo."""
        if self._artwork is None or self._artwork[0] != url:
            self._artwork = (url, read_artwork(url))
        return self._artwork[1]

    # ---------------------------------------------------------------- control
    def command(self, method: str) -> bool:
        """``PlayPause``, ``Next``, ``Previous`` o ``Stop``. ``False`` si no hay reproductor."""
        player = self._current()
        if player is None:
            return False
        self._call(player.bus_name, MPRIS_PATH, PLAYER_IFACE, method)
        return True
