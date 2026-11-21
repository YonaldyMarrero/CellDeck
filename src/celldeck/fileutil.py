"""Utilidades de archivos: escritura atómica tolerante a bloqueos de Windows."""

from __future__ import annotations

import os
import time
from pathlib import Path


def atomic_write_text(path: Path, text: str, retries: int = 5) -> None:
    """Escribe ``text`` en ``path`` sin dejar nunca un archivo a medias.

    Se escribe a un temporal y se renombra con ``os.replace``. En Windows el renombrado
    puede fallar si otro programa (antivirus, editor) tiene el archivo abierto, así que
    se reintenta unas cuantas veces.
    """
    atomic_write_bytes(path, text.encode("utf-8"), retries)


def atomic_write_bytes(path: Path, data: bytes, retries: int = 5) -> None:
    """Como :func:`atomic_write_text`, para datos binarios (p. ej. imágenes)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    for attempt in range(retries):
        try:
            os.replace(tmp, path)
            return
        except PermissionError:
            if attempt == retries - 1:
                raise
            time.sleep(0.1 * (attempt + 1))
