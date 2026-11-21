"""Iconos personalizados: validación de la imagen, almacén, API y limpieza."""

from __future__ import annotations

import io
import os
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from celldeck.custom_icons import (
    ICON_SIZE,
    MAX_UPLOAD_BYTES,
    IconStore,
    InvalidIconError,
    icon_id_of,
    normalize,
)
from celldeck.state import AppState
from tests.conftest import auth, local_token, paired_token


def image_bytes(fmt: str = "PNG", size: tuple[int, int] = (300, 150), **save: Any) -> bytes:
    img = Image.new("RGBA" if fmt == "PNG" else "RGB", size, (200, 30, 30))
    buf = io.BytesIO()
    img.save(buf, format=fmt, **save)
    return buf.getvalue()


# ------------------------------------------------------------------ imagen
def test_normalize_makes_a_square_png_keeping_aspect() -> None:
    png = normalize(image_bytes("PNG", (300, 150)))
    with Image.open(io.BytesIO(png)) as img:
        assert img.format == "PNG" and img.size == (ICON_SIZE, ICON_SIZE)
        assert img.getpixel((0, 0))[3] == 0  # bandas transparentes arriba y abajo
        assert img.getpixel((ICON_SIZE // 2, ICON_SIZE // 2))[:3] == (200, 30, 30)


@pytest.mark.parametrize("fmt", ["JPEG", "WEBP", "GIF"])
def test_other_formats_are_accepted(fmt: str) -> None:
    assert normalize(image_bytes(fmt)).startswith(b"\x89PNG")


def test_jpeg_metadata_is_dropped_and_orientation_applied() -> None:
    img = Image.new("RGB", (200, 100), (0, 0, 255))
    exif = img.getexif()
    exif[0x0112] = 6  # Orientation: girar 90° (foto hecha con el celular en vertical)
    exif[0x010F] = "Fabricante secreto"
    buf = io.BytesIO()
    img.save(buf, format="JPEG", exif=exif)
    png = normalize(buf.getvalue())
    with Image.open(io.BytesIO(png)) as out:
        assert not out.getexif()
        assert out.info.get("exif") is None
        # Tras girarla queda vertical: las bandas transparentes están a los lados.
        assert out.getpixel((0, ICON_SIZE // 2))[3] == 0
        assert out.getpixel((ICON_SIZE // 2, 0))[3] == 255


@pytest.mark.parametrize(
    ("data", "message"),
    [
        (b"", "vacía"),
        (b"no soy una imagen", "no es una imagen"),
        (b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>', "imagen"),
        (b"0" * (MAX_UPLOAD_BYTES + 1), "demasiado grande"),
    ],
)
def test_invalid_images_are_rejected(data: bytes, message: str) -> None:
    with pytest.raises(InvalidIconError, match=message):
        normalize(data)


def test_bmp_is_not_accepted() -> None:
    with pytest.raises(InvalidIconError, match="Formato no admitido"):
        normalize(image_bytes("BMP"))


def test_too_many_pixels(monkeypatch: pytest.MonkeyPatch) -> None:
    from celldeck import custom_icons

    monkeypatch.setattr(custom_icons, "MAX_PIXELS", 100)
    with pytest.raises(InvalidIconError, match="píxeles"):
        normalize(image_bytes("PNG", (20, 20)))


def test_icon_id_of() -> None:
    assert icon_id_of("img:0123456789abcdef") == "0123456789abcdef"
    assert icon_id_of("img:../../etc/passwd") is None
    assert icon_id_of("gamepad") is None
    assert icon_id_of(None) is None


# ------------------------------------------------------------------ almacén
def test_store_saves_by_content_hash(tmp_path: Path) -> None:
    store = IconStore(tmp_path / "icons")
    first = store.save(image_bytes())
    again = store.save(image_bytes())
    assert first == again and len(first) == 16
    assert store.read(first) is not None
    assert len(list((tmp_path / "icons").glob("*.png"))) == 1
    assert store.read("../settings") is None and store.path("../x") is None


def test_prune_keeps_used_and_recent_icons(tmp_path: Path) -> None:
    store = IconStore(tmp_path / "icons")
    used = store.save(image_bytes(size=(10, 10)))
    old = store.save(image_bytes(size=(20, 10)))
    recent = store.save(image_bytes(size=(30, 10)))
    long_ago = time.time() - 7200
    for icon_id in (used, old):
        os.utime(tmp_path / "icons" / f"{icon_id}.png", (long_ago, long_ago))
    assert store.prune({used}) == [old]
    assert store.read(used) and store.read(recent) and store.read(old) is None


# ---------------------------------------------------------------------- API
def upload(client: TestClient, token: str, data: bytes, content_type: str = "image/png") -> Any:
    headers = {**auth(token), "Content-Type": content_type}
    return client.post("/api/custom-icons", content=data, headers=headers)


def button_with_icon(client: TestClient, token: str, icon: str) -> Any:
    payload = client.get("/api/config", headers=auth(token)).json()
    config = payload["config"]
    button = next(i for p in config["pages"] for i in p["items"] if i["kind"] == "button")
    button["icon"] = icon
    return client.put(
        "/api/config",
        headers=auth(token),
        json={"revision": payload["revision"], "config": config},
    )


def test_upload_and_use_a_custom_icon(client: TestClient, state: AppState) -> None:
    token = local_token(client)
    res = upload(client, token, image_bytes("JPEG"), "image/jpeg")
    assert res.status_code == 200
    value = res.json()["icon"]
    assert value.startswith("img:")

    assert button_with_icon(client, token, value).status_code == 200
    assert value[4:] in state.config_store.config.custom_icon_ids()

    # Cualquier dispositivo emparejado la ve (la muestra en su panel), con el token en la URL.
    phone = paired_token(client)
    got = client.get(f"/api/custom-icons/{value[4:]}?token={phone}")
    assert got.status_code == 200 and got.headers["content-type"] == "image/png"
    assert "immutable" in got.headers["cache-control"]
    assert client.get(f"/api/custom-icons/{value[4:]}").status_code == 401
    assert client.get("/api/custom-icons/0123456789abcdef", headers=auth(token)).status_code == 404
    assert client.get("/api/custom-icons/..%2Fsettings", headers=auth(token)).status_code in (
        404,
        422,
    )


def test_only_editors_can_upload(client: TestClient) -> None:
    phone = paired_token(client)  # celular por USB sin edición permitida
    assert upload(client, phone, image_bytes()).status_code == 403
    assert client.post("/api/custom-icons", content=image_bytes()).status_code == 401


def test_upload_rejects_svg_garbage_and_huge_files(client: TestClient) -> None:
    token = local_token(client)
    svg = b'<svg xmlns="http://www.w3.org/2000/svg"></svg>'
    assert upload(client, token, svg, "image/svg+xml").status_code == 415
    assert upload(client, token, b"{}", "application/json").status_code == 415
    assert upload(client, token, b"no es una imagen").status_code == 422
    big = b"0" * (MAX_UPLOAD_BYTES + 1)
    assert upload(client, token, big).status_code == 413


def test_config_rejects_malformed_image_icons(client: TestClient) -> None:
    token = local_token(client)
    assert button_with_icon(client, token, "img:../../settings").status_code == 422
    assert button_with_icon(client, token, "img:ABCDEF0123456789").status_code == 422


def test_saving_the_config_prunes_unused_old_icons(client: TestClient, state: AppState) -> None:
    token = local_token(client)
    value = upload(client, token, image_bytes(size=(40, 40))).json()["icon"]
    orphan = upload(client, token, image_bytes(size=(50, 40))).json()["icon"]
    long_ago = time.time() - 7200
    for v in (value, orphan):
        path = state.icons.path(v[4:])
        assert path is not None
        os.utime(path, (long_ago, long_ago))
    assert button_with_icon(client, token, value).status_code == 200
    assert state.icons.read(value[4:]) is not None
    assert state.icons.read(orphan[4:]) is None
