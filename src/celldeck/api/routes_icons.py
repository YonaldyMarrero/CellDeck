"""Iconos personalizados de los botones: subir (editor) y servir (cualquier dispositivo).

La subida va en el cuerpo de la petición tal cual (``Content-Type: image/...``), sin
``multipart``: así no hace falta otra dependencia y el tamaño se limita mientras se lee.
"""

from __future__ import annotations

import logging
from typing import Annotated

from fastapi import APIRouter, HTTPException, Path, Request, Response, status

from celldeck.api.deps import EditorDep, StateDep
from celldeck.custom_icons import ICON_ID_PATTERN, ICON_PREFIX, MAX_UPLOAD_BYTES, InvalidIconError
from celldeck.security import TOKEN_HEADER

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api/custom-icons", tags=["icons"])

IconId = Annotated[str, Path(pattern=ICON_ID_PATTERN)]


async def _read_limited(request: Request, limit: int) -> bytes:
    """Lee el cuerpo y corta en cuanto supera ``limit`` (no se fía de Content-Length)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > limit:
        raise HTTPException(413, "Imagen demasiado grande")
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            raise HTTPException(413, "Imagen demasiado grande (máx. 3 MB)")
        chunks.append(chunk)
    return b"".join(chunks)


@router.post("")
async def upload_icon(request: Request, state: StateDep, who: EditorDep) -> dict[str, str]:
    """Guarda la imagen normalizada y devuelve el valor para el campo ``icon`` del botón."""
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    if not content_type.startswith("image/") or content_type == "image/svg+xml":
        raise HTTPException(
            status.HTTP_415_UNSUPPORTED_MEDIA_TYPE, "Sube una imagen PNG, JPG, WebP o GIF"
        )
    data = await _read_limited(request, MAX_UPLOAD_BYTES)
    try:
        icon_id = state.icons.save(data)
    except InvalidIconError as exc:
        raise HTTPException(422, str(exc)) from None
    log.info("Icono personalizado subido por %s", who.device)
    return {"icon": f"{ICON_PREFIX}{icon_id}"}


@router.get("/{icon_id}")
def get_icon(icon_id: IconId, request: Request, state: StateDep, token: str = "") -> Response:
    # <img> no puede enviar cabeceras: el token también se acepta por query.
    if state.tokens.verify(token or request.headers.get(TOKEN_HEADER)) is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o ausente")
    png = state.icons.read(icon_id)
    if png is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Icono no encontrado")
    # El id es el hash del contenido: la misma URL nunca cambia de imagen.
    return Response(
        png,
        media_type="image/png",
        headers={"Cache-Control": "private, max-age=31536000, immutable"},
    )
