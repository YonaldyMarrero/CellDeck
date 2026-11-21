"""Iconos personalizados de los botones: imágenes que sube el usuario desde el editor.

Seguridad:

- Solo se aceptan PNG, JPEG, WebP y GIF (el primer fotograma). Nunca SVG: puede llevar scripts.
- La imagen **se vuelve a codificar** con Pillow como PNG de ``ICON_SIZE`` px: se descartan
  metadatos (EXIF con GPS, perfiles...) y cualquier contenido añadido al archivo original.
- Límites de tamaño del archivo y de píxeles (contra "bombas de descompresión").
- Se guardan por el hash de su contenido (``<id>.png``): el id no es una ruta y no se puede
  usar para leer otros archivos; dos subidas iguales comparten archivo.

En ``config.json`` un botón referencia su imagen como ``"icon": "img:<id>"``.
"""

from __future__ import annotations

import hashlib
import io
import logging
import re
import time
from collections.abc import Iterable
from pathlib import Path

from celldeck.fileutil import atomic_write_bytes

log = logging.getLogger(__name__)

ICON_SIZE = 160  # px: nítido hasta ~64 px CSS en pantallas de densidad 2,5x
MAX_UPLOAD_BYTES = 3 * 1024 * 1024
MAX_PIXELS = 40_000_000  # ~ foto de 40 MP; más grande se rechaza antes de descomprimirla
ALLOWED_FORMATS = frozenset({"PNG", "JPEG", "WEBP", "GIF"})
ICON_ID_PATTERN = r"^[0-9a-f]{16}$"
ICON_PREFIX = "img:"
# Una imagen recién subida aún no está en config.json hasta que el editor guarda: no se borra
# en la limpieza hasta pasado este margen.
PRUNE_GRACE_S = 3600.0

_ID_RE = re.compile(ICON_ID_PATTERN)


class InvalidIconError(ValueError):
    """La imagen no es válida (mensaje apto para mostrar al usuario)."""


def icon_id_of(value: str | None) -> str | None:
    """``"img:abc..."`` → ``"abc..."``; cualquier otra cosa → ``None``."""
    if value and value.startswith(ICON_PREFIX):
        candidate = value[len(ICON_PREFIX) :]
        if _ID_RE.match(candidate):
            return candidate
    return None


def normalize(data: bytes, size: int = ICON_SIZE) -> bytes:
    """Valida la imagen y devuelve un PNG cuadrado de ``size`` px con fondo transparente."""
    if not data:
        raise InvalidIconError("La imagen está vacía")
    if len(data) > MAX_UPLOAD_BYTES:
        raise InvalidIconError("La imagen es demasiado grande (máx. 3 MB)")
    from PIL import Image, ImageOps, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as img:
            if img.format not in ALLOWED_FORMATS:
                raise InvalidIconError("Formato no admitido: usa PNG, JPG, WebP o GIF")
            width, height = img.size
            if width * height > MAX_PIXELS:
                raise InvalidIconError("La imagen tiene demasiados píxeles")
            img.seek(0)  # GIF animado: el primer fotograma
            upright = ImageOps.exif_transpose(img)  # fotos del celular giradas
            rgba = upright.convert("RGBA")
    except InvalidIconError:
        raise
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
        raise InvalidIconError("El archivo no es una imagen válida") from exc

    rgba.thumbnail((size, size), Image.Resampling.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(rgba, ((size - rgba.width) // 2, (size - rgba.height) // 2))
    buf = io.BytesIO()
    canvas.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


class IconStore:
    """Carpeta ``icons/`` de la carpeta de datos."""

    def __init__(self, folder: Path) -> None:
        self.folder = folder

    def path(self, icon_id: str) -> Path | None:
        if not _ID_RE.match(icon_id):
            return None
        return self.folder / f"{icon_id}.png"

    def save(self, data: bytes) -> str:
        """Normaliza y guarda la imagen. Devuelve su id (``InvalidIconError`` si no vale)."""
        png = normalize(data)
        icon_id = hashlib.sha256(png).hexdigest()[:16]
        target = self.folder / f"{icon_id}.png"
        if not target.exists():
            self.folder.mkdir(parents=True, exist_ok=True)
            atomic_write_bytes(target, png)
            log.info("Icono personalizado guardado: %s (%d bytes)", icon_id, len(png))
        else:
            target.touch()  # vuelve a contar el margen de limpieza
        return icon_id

    def read(self, icon_id: str) -> bytes | None:
        path = self.path(icon_id)
        if path is None:
            return None
        try:
            return path.read_bytes()
        except OSError:
            return None

    def prune(self, used: Iterable[str], grace_s: float = PRUNE_GRACE_S) -> list[str]:
        """Borra las imágenes que ningún botón usa (salvo las subidas hace menos de ``grace_s``)."""
        keep = set(used)
        removed: list[str] = []
        if not self.folder.is_dir():
            return removed
        now = time.time()
        for path in self.folder.glob("*.png"):
            if path.stem in keep or not _ID_RE.match(path.stem):
                continue
            try:
                if now - path.stat().st_mtime < grace_s:
                    continue
                path.unlink()
                removed.append(path.stem)
            except OSError:
                log.debug("No se pudo borrar el icono %s", path, exc_info=True)
        if removed:
            log.info("Iconos personalizados sin usar borrados: %d", len(removed))
        return removed
