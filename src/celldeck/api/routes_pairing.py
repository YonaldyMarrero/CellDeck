"""Emparejamiento: PIN, URLs y código QR para la página /pair.html de la PC.

Estas rutas revelan el PIN, así que **solo responden a la propia PC**: exigen un token
de administración (canjeado con un enlace de la bandeja) usado desde loopback. Ni un
celular por WiFi ni uno por USB (``adb reverse``) pueden consultarlas.
"""

from __future__ import annotations

import io
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response, status

from celldeck.api.deps import AdminDep, StateDep
from celldeck.netinfo import local_ipv4_addresses, primary_ipv4
from celldeck.state import AppState

router = APIRouter(prefix="/api/pairing", tags=["pairing"])


def pairing_urls(state: AppState, port: int) -> list[str]:
    """URLs para abrir desde el celular por WiFi (la principal primero)."""
    pin = state.settings.pin
    primary = primary_ipv4()
    ips = local_ipv4_addresses()
    ordered = ([primary] if primary else []) + [ip for ip in ips if ip != primary]
    return [f"http://{ip}:{port}/#pin={pin}" for ip in ordered]


def _port(request: Request, state: AppState) -> int:
    return request.url.port or state.settings.port


@router.get("")
def pairing_info(request: Request, state: StateDep, _: AdminDep) -> dict[str, Any]:
    port = _port(request, state)
    return {
        "mode": state.mode,
        "pin": state.settings.pin,
        "port": port,
        "urls": pairing_urls(state, port) if state.mode == "wifi" else [],
        "usb_url": f"http://localhost:{port}",
        "allow_remote_edit": state.settings.allow_remote_edit,
    }


@router.get("/qr.png")
def pairing_qr(request: Request, state: StateDep, _: AdminDep, url: str = "") -> Response:
    """QR de una de las URLs de emparejamiento (no se generan QR de texto arbitrario)."""
    if url not in pairing_urls(state, _port(request, state)):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "URL de emparejamiento no válida")
    import qrcode

    img = qrcode.make(url, box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return Response(buf.getvalue(), media_type="image/png")
