"""API HTTP y WebSocket de punta a punta (con FakeBackend)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from celldeck.platform.fake import FakeBackend
from celldeck.server import create_app
from celldeck.state import AppState
from tests.conftest import LAN_HOST, auth, local_token, make_state, paired_token

# El TestClient usa Host 'testserver' en WebSocket salvo que se pase la URL completa.
WS_URL = "ws://127.0.0.1:8765/ws"


@pytest.fixture
def wifi_state(tmp_path: Path, backend: FakeBackend) -> AppState:
    return make_state(tmp_path, backend, mode="wifi")


@pytest.fixture
def remote(wifi_state: AppState) -> Iterator[TestClient]:
    """Celular por WiFi: IP de la LAN, Host = IP de la PC."""
    app = create_app(wifi_state)
    with TestClient(app, base_url=f"http://{LAN_HOST}:8765", client=("192.168.1.50", 40000)) as c:
        yield c


# ------------------------------------------------------------------- USB/local
def test_health_and_security_headers(client: TestClient) -> None:
    res = client.get("/api/health")
    assert res.status_code == 200 and res.json()["ok"] is True
    assert res.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in res.headers["content-security-policy"]


def test_host_header_rejected(state: AppState) -> None:
    app = create_app(state)
    with TestClient(app, base_url="http://evil.example:8765", client=("127.0.0.1", 1)) as c:
        assert c.get("/api/health").status_code == 421


def test_local_session_without_token_requires_pairing(client: TestClient) -> None:
    """Llegar como 127.0.0.1 ya no da un token: el celular por adb reverse también llega así."""
    res = client.get("/api/session")
    assert res.status_code == 401
    assert "token" not in res.text


def test_admin_link_gives_admin_session(client: TestClient) -> None:
    token = local_token(client)
    data = client.get("/api/session", headers=auth(token)).json()
    assert data["loopback"] is True and data["admin"] is True and data["can_edit"] is True


def test_admin_link_is_single_use(client: TestClient, state: AppState) -> None:
    code = state.admin_links.issue()
    assert client.post("/api/session/admin", json={"code": code}).status_code == 200
    assert client.post("/api/session/admin", json={"code": code}).status_code == 403
    assert client.post("/api/session/admin", json={"code": "inventado"}).status_code == 403


def test_admin_link_only_from_loopback(remote: TestClient, wifi_state: AppState) -> None:
    code = wifi_state.admin_links.issue()
    assert remote.post("/api/session/admin", json={"code": code}).status_code == 403


def test_usb_phone_cannot_edit_or_read_pin(client: TestClient, state: AppState) -> None:
    """Una app del celular por adb reverse (127.0.0.1, sin enlace de la bandeja)."""
    token = paired_token(client)
    h = auth(token)
    data = client.get("/api/session", headers=h).json()
    assert data["admin"] is False and data["can_edit"] is False
    payload = client.get("/api/config", headers=h).json()
    assert client.put("/api/config", headers=h, json=payload).status_code == 403
    assert client.get("/api/pairing", headers=h).status_code == 403
    assert client.get("/api/obs/settings", headers=h).status_code == 403
    assert client.get("/api/spotify/settings", headers=h).status_code == 403
    # Ejecutar los botones ya configurados sí puede, como cualquier celular emparejado.
    res = client.post("/api/run", headers=h, json={"page": "media", "item": "play-pause"})
    assert res.status_code == 200


def test_usb_phone_can_edit_when_allowed(client: TestClient, state: AppState) -> None:
    state.settings_store.update(allow_remote_edit=True)
    h = auth(paired_token(client))
    payload = client.get("/api/config", headers=h).json()
    assert client.put("/api/config", headers=h, json=payload).status_code == 200
    assert client.get("/api/pairing", headers=h).status_code == 403  # el PIN, nunca


def test_admin_token_is_not_admin_from_the_network(
    remote: TestClient, wifi_state: AppState
) -> None:
    """Si un token de la PC se filtrara, desde la red vale como el de un celular más."""
    token = wifi_state.tokens.issue("pc", 1, admin=True)
    data = remote.get("/api/session", headers=auth(token)).json()
    assert data["admin"] is False and data["can_edit"] is False
    assert remote.get("/api/pairing", headers=auth(token)).status_code == 403


def test_websocket_hello_reports_edit_permission(client: TestClient) -> None:
    with client.websocket_connect(f"{WS_URL}?token={paired_token(client)}") as ws:
        hello = ws.receive_json()
    assert hello["type"] == "hello" and hello["data"]["can_edit"] is False
    with client.websocket_connect(f"{WS_URL}?token={local_token(client)}") as ws:
        assert ws.receive_json()["data"]["can_edit"] is True


def test_run_requires_token(client: TestClient, backend: FakeBackend) -> None:
    res = client.post("/api/run", json={"page": "media", "item": "play-pause"})
    assert res.status_code == 401
    assert backend.calls == []


def test_run_button(client: TestClient, backend: FakeBackend) -> None:
    token = local_token(client)
    res = client.post("/api/run", json={"page": "media", "item": "play-pause"}, headers=auth(token))
    assert res.status_code == 200 and res.json()["ok"] is True
    assert backend.names() == ["send_media_key"]


def test_run_unknown_or_widget(client: TestClient) -> None:
    h = auth(local_token(client))
    assert (
        client.post("/api/run", json={"page": "media", "item": "nope"}, headers=h).status_code
        == 404
    )
    assert (
        client.post(
            "/api/run", json={"page": "media", "item": "now-playing"}, headers=h
        ).status_code
        == 404
    )


def test_confirm_required(client: TestClient, backend: FakeBackend) -> None:
    h = auth(local_token(client))
    res = client.post("/api/run", json={"page": "pc", "item": "sleep"}, headers=h)
    assert res.status_code == 428 and res.json()["detail"]["confirm_required"] is True
    assert backend.calls == []
    res = client.post(
        "/api/run", json={"page": "pc", "item": "sleep", "confirmed": True}, headers=h
    )
    assert res.status_code == 200 and backend.names() == ["system_command"]


def test_cannot_send_arbitrary_actions(client: TestClient, backend: FakeBackend) -> None:
    """El endpoint ignora cualquier 'action' enviada: solo vale lo que está en la config."""
    h = auth(local_token(client))
    body = {"page": "media", "item": "next", "action": {"type": "launch", "path": "cmd.exe"}}
    res = client.post("/api/run", json=body, headers=h)
    assert res.status_code == 200
    assert backend.names() == ["send_media_key"]


def test_get_and_put_config(client: TestClient, state: AppState) -> None:
    h = auth(local_token(client))
    current = client.get("/api/config", headers=h).json()
    config = current["config"]
    config["pages"][0]["name"] = "Música"
    res = client.put(
        "/api/config", json={"revision": current["revision"], "config": config}, headers=h
    )
    assert res.status_code == 200
    assert state.config_store.config.pages[0].name == "Música"

    # Revisión vieja → conflicto
    res = client.put(
        "/api/config", json={"revision": current["revision"], "config": config}, headers=h
    )
    assert res.status_code == 409

    # Config inválida → 422 con detalle
    config["pages"][0]["items"][1]["action"] = {"type": "keys", "combo": "ctrl+xyz"}
    res = client.put("/api/config", json={"config": config}, headers=h)
    assert res.status_code == 422
    assert "Tecla desconocida" in res.json()["detail"]["message"]


def test_plugins_endpoint(client: TestClient) -> None:
    data = client.get("/api/plugins", headers=auth(local_token(client))).json()
    assert {p["type"] for p in data["plugins"]} >= {"media", "macro"}
    assert "ctrl" in data["keys"]


def test_websocket_requires_token_and_receives_config(client: TestClient) -> None:
    with pytest.raises(WebSocketDisconnect), client.websocket_connect(WS_URL) as ws:
        ws.receive_text()

    token = local_token(client)
    with client.websocket_connect(f"{WS_URL}?token={token}") as ws:
        assert ws.receive_json()["type"] == "hello"
        ws.send_json({"type": "ping"})
        assert ws.receive_json() == {"type": "pong"}
        h = auth(token)
        current = client.get("/api/config", headers=h).json()
        current["config"]["pages"][0]["name"] = "Por WS"
        client.put("/api/config", json=current, headers=h)
        msg = ws.receive_json()
        assert msg["type"] == "config"
        assert msg["data"]["config"]["pages"][0]["name"] == "Por WS"


# ----------------------------------------------------------------------- WiFi
def test_remote_session_requires_pairing(remote: TestClient) -> None:
    assert remote.get("/api/session").status_code == 401


def test_remote_pairing_flow(
    remote: TestClient, wifi_state: AppState, backend: FakeBackend
) -> None:
    assert (
        remote.post(
            "/api/pair", json={"pin": "000000" if wifi_state.settings.pin != "000000" else "111111"}
        ).status_code
        == 403
    )
    res = remote.post("/api/pair", json={"pin": wifi_state.settings.pin, "device": "Redmi"})
    assert res.status_code == 200
    data = res.json()
    assert data["loopback"] is False and data["can_edit"] is False
    h = auth(data["token"])
    assert remote.get("/api/session", headers=h).status_code == 200
    assert remote.post("/api/run", json={"page": "media", "item": "next"}, headers=h).json()["ok"]
    assert backend.names() == ["send_media_key"]


def test_remote_cannot_edit_by_default(remote: TestClient, wifi_state: AppState) -> None:
    token = remote.post("/api/pair", json={"pin": wifi_state.settings.pin}).json()["token"]
    current = remote.get("/api/config", headers=auth(token)).json()
    res = remote.put("/api/config", json=current, headers=auth(token))
    assert res.status_code == 403

    wifi_state.settings_store.update(allow_remote_edit=True)
    current = remote.get("/api/config", headers=auth(token)).json()
    assert remote.put("/api/config", json=current, headers=auth(token)).status_code == 200


def test_pin_bruteforce_locks(remote: TestClient, wifi_state: AppState) -> None:
    wrong = "000000" if wifi_state.settings.pin != "000000" else "111111"
    codes = [remote.post("/api/pair", json={"pin": wrong}).status_code for _ in range(6)]
    assert codes[:5] == [403] * 5 and codes[5] == 429
    # Ni siquiera el PIN correcto funciona durante el bloqueo
    assert remote.post("/api/pair", json={"pin": wifi_state.settings.pin}).status_code == 429


def test_revoke_devices(remote: TestClient, wifi_state: AppState) -> None:
    token = remote.post("/api/pair", json={"pin": wifi_state.settings.pin}).json()["token"]
    wifi_state.settings_store.revoke_all_devices()
    assert remote.get("/api/config", headers=auth(token)).status_code == 401


def test_validate_config_endpoint(client: TestClient, state: AppState) -> None:
    h = auth(local_token(client))
    config = client.get("/api/config", headers=h).json()["config"]
    assert client.post("/api/config/validate", json={"config": config}, headers=h).json() == {
        "ok": True
    }
    config["pages"][0]["items"][1]["action"] = {"type": "keys", "combo": "ctrl+nope"}
    res = client.post("/api/config/validate", json={"config": config}, headers=h)
    assert res.status_code == 422
    assert res.json()["detail"]["errors"][0]["loc"].startswith("pages.0.items.1")
    assert not res.json()["detail"]["errors"][0]["msg"].startswith("Value error")
    # Validar no guarda nada
    assert state.config_store.config.pages[0].items[1].action.type == "media"


def test_remote_cannot_validate_without_edit_permission(
    remote: TestClient, wifi_state: AppState
) -> None:
    token = remote.post("/api/pair", json={"pin": wifi_state.settings.pin}).json()["token"]
    config = remote.get("/api/config", headers=auth(token)).json()["config"]
    res = remote.post("/api/config/validate", json={"config": config}, headers=auth(token))
    assert res.status_code == 403
