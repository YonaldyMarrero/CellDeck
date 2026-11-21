"""Backend de Linux: lógica que no necesita un escritorio real (dobles de pulse, D-Bus, X11)."""

from __future__ import annotations

import asyncio
import os
import struct
import sys
import zlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from celldeck import autostart, winui
from celldeck.apps import APPS
from celldeck.platform.audio import SYSTEM_KEY
from celldeck.platform.base import PlatformError, SystemCommand
from celldeck.platform.keymap import _NAMED_KEYS, parse_combo

# Los módulos de Linux no se pueden importar en otros sistemas: el archivo entero se omite.
if sys.platform != "linux":
    pytest.skip("Solo Linux", allow_module_level=True)

from celldeck.platform import linux_apps, linux_input
from celldeck.platform.linux import LinuxBackend
from celldeck.platform.linux_audio import GONE_MESSAGE, PulseAudio, identify_stream
from celldeck.platform.linux_mpris import (
    MAX_ARTWORK_BYTES,
    PlayerState,
    parse_media,
    pick_player,
    read_artwork,
    unwrap,
)


# ---------------------------------------------------------------------- teclado
def test_every_key_name_maps_to_x11_and_evdev() -> None:
    """Cualquier tecla que acepta el editor se puede pulsar en X11 y en Wayland."""
    for name, vk in _NAMED_KEYS.items():
        assert vk in linux_input.VK_TO_KEYSYM, name
        assert vk in linux_input.VK_TO_EVDEV, name


def test_evdev_codes_of_common_keys() -> None:
    codes = [linux_input.VK_TO_EVDEV[vk] for vk in parse_combo("ctrl+shift+t").keys]
    assert codes == [29, 42, 20]  # KEY_LEFTCTRL, KEY_LEFTSHIFT, KEY_T
    assert linux_input.VK_TO_EVDEV[parse_combo("f12").keys[0]] == 88
    assert linux_input.VK_TO_EVDEV[parse_combo("f13").keys[0]] == 183


def test_char_keysym() -> None:
    assert linux_input.char_keysym("a") == ord("a")
    assert linux_input.char_keysym("ñ") == 0xF1  # Latin-1: el keysym es el código
    assert linux_input.char_keysym("€") == 0x01000000 | 0x20AC


@pytest.mark.parametrize(
    ("env", "expected"),
    [
        ({"XDG_SESSION_TYPE": "wayland"}, "wayland"),
        ({"XDG_SESSION_TYPE": "x11"}, "x11"),
        ({"WAYLAND_DISPLAY": "wayland-0"}, "wayland"),
        ({"DISPLAY": ":0"}, "x11"),
        ({}, "tty"),
    ],
)
def test_session_type(monkeypatch: pytest.MonkeyPatch, env: dict[str, str], expected: str) -> None:
    for var in ("XDG_SESSION_TYPE", "WAYLAND_DISPLAY", "DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    assert linux_input.session_type() == expected


def test_wayland_without_ydotool_explains_how_to_fix(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    monkeypatch.setattr(linux_input.shutil, "which", lambda _name: None)
    with pytest.raises(PlatformError, match="ydotool"):
        linux_input.create_keyboard()


def test_ydotool_receives_text_by_stdin(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[list[str], str | None]] = []

    def fake_run(args: list[str], **kwargs: Any) -> Any:
        calls.append((args, kwargs.get("input")))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(linux_input.shutil, "which", lambda _name: "/usr/bin/ydotool")
    monkeypatch.setattr(linux_input.subprocess, "run", fake_run)
    kbd = linux_input.YdotoolKeyboard()
    kbd.type_text("--help; rm -rf ~")
    kbd.combo(parse_combo("ctrl+c").keys)
    assert calls[0] == (["/usr/bin/ydotool", "type", "--file", "-"], "--help; rm -rf ~")
    assert calls[1][0] == ["/usr/bin/ydotool", "key", "29:1", "46:1", "46:0", "29:0"]


# ------------------------------------------------------------------------ audio
class FakeVolume:
    def __init__(self, value: float) -> None:
        self.value_flat = value


class FakePulse:
    """Imita la parte de ``pulsectl.Pulse`` que usa CellDeck."""

    def __init__(self) -> None:
        self.sink = SimpleNamespace(volume=FakeVolume(1.03), mute=0)
        self.inputs = [
            self._stream("Firefox", "firefox", 0.5, pid="1"),
            self._stream("Firefox", "firefox", 0.8, mute=True, pid="1"),
            self._stream("Spotify", "spotify", 0.3),
            self._stream("sonido", "canberra-gtk-play", 1.0, role="event"),
            self._stream("CellDeck", "python3", 1.0),  # el propio CellDeck no se muestra
        ]
        self.closed = False

    @staticmethod
    def _stream(
        name: str, binary: str, volume: float, mute: bool = False, role: str = "", pid: str = ""
    ) -> Any:
        props = {"application.name": name, "application.process.binary": binary}
        if role:
            props["media.role"] = role
        if pid:
            props["application.process.id"] = pid
        return SimpleNamespace(proplist=props, volume=FakeVolume(volume), mute=int(mute))

    def server_info(self) -> Any:
        return SimpleNamespace(default_sink_name="alsa_output.test")

    def get_sink_by_name(self, name: str) -> Any:
        assert name == "alsa_output.test"
        return self.sink

    def sink_input_list(self) -> list[Any]:
        return self.inputs

    def volume_set_all_chans(self, obj: Any, value: float) -> None:
        obj.volume = FakeVolume(value)

    def mute(self, obj: Any, muted: bool) -> None:
        obj.mute = int(muted)

    def close(self) -> None:
        self.closed = True


def test_master_volume_is_clamped_to_100() -> None:
    fake = FakePulse()
    audio = PulseAudio(lambda: fake)
    state = audio.get_volume()
    assert state.level == 100 and state.muted is False  # pulse admite 103 %
    audio.set_volume(40)
    audio.set_mute(True)
    assert audio.get_volume().level == 40 and fake.sink.mute == 1


def test_mixer_groups_streams_and_marks_system_sounds() -> None:
    fake = FakePulse()
    audio = PulseAudio(lambda: fake)
    apps = audio.list_apps()
    assert [a.name for a in apps] == ["Firefox", "Spotify", "Sonidos del sistema"]
    firefox = apps[0]
    assert firefox.level == 80 and firefox.muted is False  # una de sus pestañas suena
    assert apps[-1].key == SYSTEM_KEY

    audio.set_level(firefox.key, 20)
    assert [round(s.volume.value_flat * 100) for s in fake.inputs[:2]] == [20, 20]
    audio.set_muted(firefox.key, True)
    assert all(s.mute for s in fake.inputs[:2])
    with pytest.raises(PlatformError, match=GONE_MESSAGE):
        audio.set_level("noexiste", 50)


def test_flatpak_apps_are_keyed_by_their_id() -> None:
    key_a, name, _ = identify_stream(
        {"application.name": "Spotify", "application.flatpak.id": "com.spotify.Client"}
    )
    key_b, _, _ = identify_stream({"application.name": "Otra", "application.process.binary": "x"})
    assert name == "Spotify" and key_a != key_b


def test_audio_reconnects_after_the_server_restarts() -> None:
    attempts = {"n": 0}
    good = FakePulse()

    class Broken(FakePulse):
        def server_info(self) -> Any:
            raise ConnectionError("pipewire reiniciado")

    def factory() -> Any:
        attempts["n"] += 1
        return Broken() if attempts["n"] == 1 else good

    audio = PulseAudio(factory)
    assert audio.get_volume().level == 100
    assert attempts["n"] == 2


def test_audio_without_server_is_a_friendly_error() -> None:
    def factory() -> Any:
        raise OSError("sin servidor")

    with pytest.raises(PlatformError, match="PulseAudio"):
        PulseAudio(factory).get_volume()


# ------------------------------------------------------------------------ MPRIS
def _player(name: str, status: str, meta: dict[str, Any] | None = None) -> Any:
    return PlayerState(f"org.mpris.MediaPlayer2.{name}", status, meta or {}, 30_000_000)


def test_unwrap_variants() -> None:
    assert unwrap(("s", "hola")) == "hola"
    assert unwrap(("v", ("as", ["a", "b"]))) == ["a", "b"]
    assert unwrap(5) == 5


def test_parse_media_metadata() -> None:
    meta = {
        "xesam:title": ("s", "Canción"),
        "xesam:artist": ("as", ["Uno", "Dos"]),
        "xesam:album": ("s", "Disco"),
        "mpris:length": ("x", 180_000_000),
        "mpris:artUrl": ("s", "https://i.scdn.co/image/abc"),
    }
    info, art = parse_media(_player("spotify", "playing", meta))
    assert (info.title, info.artist, info.album) == ("Canción", "Uno, Dos", "Disco")
    assert info.duration_s == 180 and info.position_s == 30 and info.status == "playing"
    assert info.app_id == "spotify" and art == "https://i.scdn.co/image/abc"


def test_pick_player_prefers_the_one_playing() -> None:
    players = [_player("vlc", "paused"), _player("spotify", "playing"), _player("x", "stopped")]
    assert pick_player(players, None).bus_name.endswith("spotify")  # type: ignore[union-attr]
    paused = [_player("a", "paused"), _player("b", "paused")]
    assert pick_player(paused, "org.mpris.MediaPlayer2.b").bus_name.endswith("b")  # type: ignore[union-attr]
    assert pick_player([], None) is None


def test_read_artwork_from_file_and_limits(tmp_path: Path) -> None:
    small = tmp_path / "cover.jpg"
    small.write_bytes(b"jpeg")
    assert read_artwork(small.as_uri()) == b"jpeg"
    big = tmp_path / "big.jpg"
    big.write_bytes(b"0" * (MAX_ARTWORK_BYTES + 1))
    assert read_artwork(big.as_uri()) is None
    assert read_artwork("javascript:alert(1)") is None
    assert read_artwork("ftp://example.com/x.png") is None


def test_read_artwork_over_http_is_size_limited() -> None:
    class Res:
        def __init__(self, data: bytes) -> None:
            self.data = data

        def read(self, n: int) -> bytes:
            return self.data[:n]

        def __enter__(self) -> Res:
            return self

        def __exit__(self, *_: Any) -> None:
            pass

    ok = read_artwork("https://cdn/x", opener=lambda *_a, **_k: Res(b"png"))
    huge = read_artwork(
        "https://cdn/x", opener=lambda *_a, **_k: Res(b"0" * (MAX_ARTWORK_BYTES + 10))
    )
    assert ok == b"png" and huge is None


# ------------------------------------------------------------- apps e iconos
def _png(size: int) -> bytes:
    raw = b"".join(b"\x00" + b"\xff\x00\x00\xff" * size for _ in range(size))

    def chunk(kind: bytes, data: bytes) -> bytes:
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


@pytest.fixture
def xdg(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Carpetas XDG de prueba: ni las apps ni los iconos del sistema real cuentan."""
    home = tmp_path / "home"
    share = home / ".local" / "share"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(share))
    monkeypatch.setenv("XDG_DATA_DIRS", str(tmp_path / "usr" / "share"))
    monkeypatch.setattr(linux_apps, "data_dirs", lambda: [share, tmp_path / "usr" / "share"])
    linux_apps.forget_index()
    (share / "applications").mkdir(parents=True)
    yield share
    linux_apps.forget_index()


def _desktop(folder: Path, desktop_id: str, exec_line: str, icon: str = "") -> Path:
    path = folder / "applications" / f"{desktop_id}.desktop"
    path.write_text(
        f"[Desktop Entry]\nType=Application\nName={desktop_id}\nExec={exec_line}\nIcon={icon}\n",
        encoding="utf-8",
    )
    return path


def test_locate_finds_flatpak_style_desktop_files(xdg: Path) -> None:
    path = _desktop(xdg, "com.visualstudio.code", "/usr/bin/flatpak run com.visualstudio.code")
    location = linux_apps.locate(APPS["vscode"])
    assert location is not None and location.path == str(path) and location.source == "desktop"


def test_locate_falls_back_to_path(xdg: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        linux_apps.shutil, "which", lambda n: "/usr/bin/steam" if n == "steam" else None
    )
    location = linux_apps.locate(APPS["steam"])
    assert location is not None and location.path == "/usr/bin/steam"
    assert linux_apps.locate(APPS["visualstudio"]) is None  # no existe en Linux


def test_windows_programs_are_translated(xdg: Path) -> None:
    calc = _desktop(xdg, "org.gnome.Calculator", "gnome-calculator")
    assert linux_apps.translate_windows_program("calc.exe") == linux_apps.Translated(desktop=calc)
    assert linux_apps.translate_windows_program("EXPLORER.EXE") == linux_apps.Translated(
        folder=Path.home()
    )
    assert linux_apps.translate_windows_program("/usr/bin/calc.exe") is None
    assert linux_apps.translate_windows_program("firefox") is None
    settings = _desktop(xdg, "org.gnome.Settings", "gnome-control-center")
    assert linux_apps.translate_windows_uri("ms-settings:display") == settings
    assert linux_apps.translate_windows_uri("https://example.com") is None


def test_icon_from_desktop_entry_picks_a_large_enough_size(xdg: Path) -> None:
    for size in (48, 256, 512):
        folder = xdg / "icons" / "hicolor" / f"{size}x{size}" / "apps"
        folder.mkdir(parents=True)
        (folder / "steam.png").write_bytes(_png(size))
    _desktop(xdg, "steam", "/usr/games/steam %U", icon="steam")
    assert linux_apps.resolve_icon("steam", 160) == (
        xdg / "icons" / "hicolor" / "256x256" / "apps" / "steam.png"
    )
    assert linux_apps.resolve_icon("steam", 1024).parent.parent.name == "512x512"  # type: ignore[union-attr]

    png = linux_apps.icon_png(str(xdg / "applications" / "steam.desktop"), 160)
    assert png is not None and png.startswith(b"\x89PNG")
    # Un ejecutable también encuentra su icono a través del .desktop que lo lanza.
    assert linux_apps.icon_png("/usr/games/steam", 96) is not None
    assert linux_apps.icon_png("/usr/bin/desconocido", 96) is None


def test_desktop_exe_name_skips_env_prefix(xdg: Path) -> None:
    path = _desktop(xdg, "brave-browser", "env BAMF=1 /usr/bin/brave-browser-stable %U")
    entry = linux_apps.parse_desktop(path)
    assert entry is not None and entry.exe_name == "brave-browser-stable"


# --------------------------------------------------------------------- backend
@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    calls: list[list[str]] = []

    async def fake_spawn(args: Any, cwd: str | None = None, wait: float = 0) -> None:
        calls.append(list(args))

    from celldeck.platform import linux as linux_module

    monkeypatch.setattr(linux_module, "spawn", fake_spawn)
    monkeypatch.setattr(
        linux_module.shutil, "which", lambda name: f"/usr/bin/{name}" if name != "zz" else None
    )
    return calls


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


def test_open_path_variants(spawned: list[list[str]], tmp_path: Path, xdg: Path) -> None:
    backend = LinuxBackend()
    script = tmp_path / "script.sh"
    script.write_text("#!/bin/sh\n")
    script.chmod(0o755)
    doc = tmp_path / "notas.txt"
    doc.write_text("hola")
    desktop = _desktop(xdg, "steam", "steam")

    _run(backend.open_path(str(script), ["--uno", "dos dos"]))
    _run(backend.open_path(str(tmp_path)))
    _run(backend.open_path(str(doc)))
    _run(backend.open_path(str(desktop)))
    _run(backend.open_path("explorer.exe"))
    _run(backend.open_path("firefox", ["https://example.com"]))
    assert spawned == [
        [str(script), "--uno", "dos dos"],
        ["/usr/bin/gio", "open", str(tmp_path)],
        ["/usr/bin/gio", "open", str(doc)],
        ["/usr/bin/gio", "launch", str(desktop)],
        ["/usr/bin/gio", "open", str(Path.home())],
        ["/usr/bin/firefox", "https://example.com"],
    ]
    with pytest.raises(PlatformError, match="No se encontró"):
        _run(backend.open_path(str(tmp_path / "no-existe")))
    _run(backend.close())


def test_open_uri_uses_default_handler(spawned: list[list[str]], xdg: Path) -> None:
    backend = LinuxBackend()
    _run(backend.open_uri("spotify:playlist:123"))
    settings = _desktop(xdg, "org.gnome.Settings", "gnome-control-center")
    _run(backend.open_uri("ms-settings:"))
    assert spawned == [
        ["/usr/bin/gio", "open", "spotify:playlist:123"],
        ["/usr/bin/gio", "launch", str(settings)],
    ]
    _run(backend.close())


@pytest.mark.parametrize(
    ("command", "expected"),
    [
        (SystemCommand.SLEEP, ["/usr/bin/systemctl", "suspend"]),
        (SystemCommand.HIBERNATE, ["/usr/bin/systemctl", "hibernate"]),
        (SystemCommand.SHUTDOWN, ["/usr/bin/systemctl", "poweroff"]),
        (SystemCommand.RESTART, ["/usr/bin/systemctl", "reboot"]),
        (SystemCommand.LOCK, ["/usr/bin/loginctl", "lock-session"]),
        (SystemCommand.LOGOFF, ["/usr/bin/gnome-session-quit", "--logout", "--no-prompt"]),
    ],
)
def test_system_commands(
    spawned: list[list[str]], command: SystemCommand, expected: list[str]
) -> None:
    backend = LinuxBackend()
    _run(backend.system_command(command))
    assert spawned == [expected]
    _run(backend.close())


def test_monitor_off_only_on_x11(spawned: list[list[str]], monkeypatch: pytest.MonkeyPatch) -> None:
    backend = LinuxBackend()
    monkeypatch.setenv("XDG_SESSION_TYPE", "wayland")
    with pytest.raises(PlatformError, match="X11"):
        _run(backend.system_command(SystemCommand.MONITOR_OFF))
    monkeypatch.setenv("XDG_SESSION_TYPE", "x11")
    _run(backend.system_command(SystemCommand.MONITOR_OFF))
    assert spawned == [["/usr/bin/xset", "dpms", "force", "off"]]
    _run(backend.close())


def test_real_spawn_reports_a_failing_launcher(tmp_path: Path) -> None:
    from celldeck.platform.linux import spawn

    failing = tmp_path / "falla.sh"
    failing.write_text("#!/bin/sh\nexit 3\n")
    failing.chmod(0o755)
    with pytest.raises(PlatformError, match="código 3"):
        _run(spawn([str(failing)], wait=5))
    slow = tmp_path / "lento.sh"
    slow.write_text("#!/bin/sh\nsleep 5\n")
    slow.chmod(0o755)
    _run(spawn([str(slow)], wait=0.2))  # sigue en marcha: se da por abierto


def test_create_backend_on_linux() -> None:
    from celldeck.platform import create_backend

    backend = create_backend()
    assert backend.name == "linux"
    _run(backend.close())


# ----------------------------------------------------------- escritorio
def test_autostart_desktop_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("APPIMAGE", "/home/yo/Apps/CellDeck x86_64.AppImage")
    assert autostart.is_supported() and not autostart.is_enabled()
    autostart.set_enabled(True)
    text = (tmp_path / "autostart" / "celldeck.desktop").read_text()
    assert "Exec='/home/yo/Apps/CellDeck x86_64.AppImage'" in text
    assert autostart.is_enabled()
    monkeypatch.setenv("APPIMAGE", "/opt/CellDeck.AppImage")  # se movió: se actualiza
    autostart.refresh_path()
    assert (
        "Exec=/opt/CellDeck.AppImage" in (tmp_path / "autostart" / "celldeck.desktop").read_text()
    )
    autostart.set_enabled(False)
    assert not autostart.is_enabled()


def test_single_instance_lock(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(winui, "_mutex", None)
    assert winui._linux_single_instance() is True
    held = winui._mutex
    assert winui._linux_single_instance() is False  # otra "instancia"
    held.close()
    monkeypatch.setattr(winui, "_mutex", None)
    assert winui._linux_single_instance() is True
    winui._mutex.close()


def test_dialog_without_desktop_answers_no(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("DISPLAY", "WAYLAND_DISPLAY"):
        monkeypatch.delenv(var, raising=False)
    assert winui.ask("¿Seguro?") is False


def test_zenity_question(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[list[str]] = []
    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr(
        winui.shutil, "which", lambda n: "/usr/bin/zenity" if n == "zenity" else None
    )

    def fake_run(args: list[str], **_: Any) -> Any:
        seen.append(args)
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(winui.subprocess, "run", fake_run)
    assert winui.ask("¿Activar?", "CellDeck") is True
    assert seen[0][:2] == ["/usr/bin/zenity", "--question"] and "--text=¿Activar?" in seen[0]
    assert os.environ["DISPLAY"] == ":0"


def test_deb_launcher_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """El paquete .deb arranca con /usr/bin/celldeck y deja los recursos en /opt/celldeck."""
    from celldeck import paths

    monkeypatch.delenv("APPIMAGE", raising=False)
    monkeypatch.setenv("CELLDECK_RESOURCES", "/opt/celldeck")
    monkeypatch.setenv("CELLDECK_LAUNCHER", "/usr/bin/celldeck")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    assert paths.resource_dir() == Path("/opt/celldeck")
    assert paths.web_dir() == Path("/opt/celldeck/web")
    assert autostart.is_supported()
    autostart.set_enabled(True)
    assert "Exec=/usr/bin/celldeck\n" in (tmp_path / "autostart" / "celldeck.desktop").read_text()
