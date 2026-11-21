"""Sesión y emparejamiento por PIN."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from celldeck.api.deps import Principal, StateDep, client_ip, principal_for
from celldeck.security import TOKEN_HEADER, PairingLocked, is_loopback
from celldeck.state import AppState

log = logging.getLogger(__name__)
router = APIRouter(prefix="/api", tags=["auth"])


def _session_info(state: AppState, principal: Principal | None, ip: str) -> dict[str, Any]:
    admin = bool(principal and principal.admin)
    return {
        "version": state.version,
        "mode": state.mode,
        # Conexión local (PC o celular por USB): solo para mensajes de la UI, no da permisos.
        "loopback": is_loopback(ip),
        "admin": admin,
        "can_edit": admin or state.settings.allow_remote_edit,
        "backend": state.backend.name,
    }


@router.get("/session")
def session(request: Request, state: StateDep) -> dict[str, Any]:
    """Confirma el token del cliente; si no hay o caducó, 401 y la UI pide el PIN.

    Ya no se entrega un token sin PIN a los clientes locales: con ``adb reverse`` el
    celular (y cualquier app instalada en él) llega como 127.0.0.1. La propia PC entra
    con un enlace de un solo uso de la bandeja (``POST /api/session/admin``).
    """
    ip = client_ip(request)
    principal = principal_for(state, request.headers.get(TOKEN_HEADER), ip)
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Emparejamiento requerido")
    return _session_info(state, principal, ip)


class AdminRequest(BaseModel):
    code: str = Field(min_length=1, max_length=64)


@router.post("/session/admin")
def admin_session(body: AdminRequest, request: Request, state: StateDep) -> dict[str, Any]:
    """Canjea un enlace de la bandeja (``#admin=código``) por un token de administración."""
    ip = client_ip(request)
    if not is_loopback(ip) or not state.admin_links.consume(body.code):
        log.warning("Enlace de administración inválido o caducado desde %s", ip)
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "Enlace caducado. Abre el panel otra vez desde el icono de la bandeja.",
        )
    token = state.tokens.issue("pc", state.settings.token_ttl_days, admin=True)
    principal = principal_for(state, token, ip)
    return {"token": token, **_session_info(state, principal, ip)}


class PairRequest(BaseModel):
    pin: str = Field(min_length=1, max_length=12)
    device: str = Field(default="celular", max_length=64)


@router.post("/pair")
def pair(body: PairRequest, request: Request, state: StateDep) -> dict[str, Any]:
    """Canjea el PIN por un token (con límite de intentos)."""
    ip = client_ip(request)
    try:
        ok = state.pin_guard.verify(ip, body.pin.strip(), state.settings.pin)
    except PairingLocked as exc:
        raise HTTPException(
            status.HTTP_429_TOO_MANY_REQUESTS,
            str(exc),
            headers={"Retry-After": str(exc.retry_after)},
        ) from None
    if not ok:
        log.warning("PIN incorrecto desde %s", ip)
        raise HTTPException(status.HTTP_403_FORBIDDEN, "PIN incorrecto")
    log.info("Dispositivo emparejado desde %s (%s)", ip, body.device)
    token = state.tokens.issue(body.device, state.settings.token_ttl_days)
    return {"token": token, **_session_info(state, principal_for(state, token, ip), ip)}
