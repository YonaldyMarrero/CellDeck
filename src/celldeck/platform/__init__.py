"""Capa de abstracción del sistema operativo."""

from __future__ import annotations

import logging
import sys

from celldeck.platform.base import (
    MediaInfo,
    MediaKey,
    PlatformBackend,
    PlatformError,
    SystemCommand,
    VolumeState,
)
from celldeck.platform.fake import FakeBackend

__all__ = [
    "FakeBackend",
    "MediaInfo",
    "MediaKey",
    "PlatformBackend",
    "PlatformError",
    "SystemCommand",
    "VolumeState",
    "create_backend",
]

log = logging.getLogger(__name__)


def create_backend(fake: bool = False) -> PlatformBackend:
    """Crea el backend del sistema: Windows, Linux o, en otro caso (o con ``fake``), el simulado."""
    if fake:
        return FakeBackend(demo=True)
    if sys.platform == "win32":
        from celldeck.platform.windows import WindowsBackend

        return WindowsBackend()
    if sys.platform == "linux":
        from celldeck.platform.linux import LinuxBackend

        return LinuxBackend()
    log.warning("Sistema no soportado (%s): se usa el backend simulado", sys.platform)
    return FakeBackend(demo=True)
