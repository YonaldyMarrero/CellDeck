"""Dependencias de FastAPI: estado y autenticación."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status

from celldeck.security import TOKEN_HEADER, is_loopback
from celldeck.state import AppState


def get_state(request: Request) -> AppState:
    state: AppState = request.app.state.pd
    return state


StateDep = Annotated[AppState, Depends(get_state)]


@dataclass(frozen=True, slots=True)
class Principal:
    """Quién hace la petición."""

    device: str
    loopback: bool
    # La propia PC: token de administración canjeado con un enlace de la bandeja.
    # La IP no basta (el celular por ``adb reverse`` también llega como 127.0.0.1).
    admin: bool
    can_edit: bool


def client_ip(request: Request) -> str:
    return request.client.host if request.client else ""


def principal_for(state: AppState, token: str | None, ip: str | None) -> Principal | None:
    """Quién es el dueño de ``token`` conectado desde ``ip`` (``None`` si no es válido).

    Un token de administración solo cuenta como tal desde loopback: si se filtrara, desde
    la red valdría como el de un dispositivo más.
    """
    payload = state.tokens.verify(token)
    if payload is None:
        return None
    loopback = is_loopback(ip)
    admin = payload.admin and loopback
    return Principal(
        device=payload.device,
        loopback=loopback,
        admin=admin,
        can_edit=admin or state.settings.allow_remote_edit,
    )


def get_principal(request: Request, state: StateDep) -> Principal:
    """Exige un token válido (incluso desde loopback, como protección CSRF)."""
    principal = principal_for(state, request.headers.get(TOKEN_HEADER), client_ip(request))
    if principal is None:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Token inválido o ausente")
    return principal


PrincipalDep = Annotated[Principal, Depends(get_principal)]


def require_editor(principal: PrincipalDep) -> Principal:
    if not principal.can_edit:
        raise HTTPException(
            status.HTTP_403_FORBIDDEN,
            "La edición remota está desactivada. Actívala desde la bandeja de la PC.",
        )
    return principal


EditorDep = Annotated[Principal, Depends(require_editor)]


def require_admin(principal: PrincipalDep) -> Principal:
    """Solo la propia PC (con un token canjeado desde la bandeja)."""
    if not principal.admin:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Solo disponible desde la propia PC")
    return principal


AdminDep = Annotated[Principal, Depends(require_admin)]
