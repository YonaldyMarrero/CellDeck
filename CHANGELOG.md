# Registro de cambios

Todos los cambios visibles de CellDeck se anotan aquí. El formato sigue
[Keep a Changelog](https://keepachangelog.com/es-ES/1.1.0/) y las versiones siguen
[Versionado Semántico](https://semver.org/lang/es/): `MAYOR.MENOR.PARCHE`.

## [Sin publicar]

### Añadido

- **Soporte para Linux** (X11 y Wayland; GNOME, KDE, XFCE, Cinnamon…). Ver `docs/linux.md`:
  - Volumen y Volume Mixer con PulseAudio o PipeWire.
  - "Ahora suena", portada y controles multimedia con MPRIS (Spotify, navegadores, VLC…).
  - Atajos de teclado y texto con XTEST en X11, o `ydotool` en Wayland.
  - Apps instaladas por sus archivos `.desktop` (paquetes, Flatpak y Snap), con su logotipo.
  - Bloquear, suspender, hibernar, apagar, reiniciar y cerrar sesión.
  - Icono en la bandeja (AppIndicator), diálogos con zenity/kdialog e inicio automático XDG.
- Una configuración hecha en Windows funciona en Linux: `explorer.exe`, `notepad.exe`,
  `calc.exe`, `cmd.exe`, `taskmgr.exe` y `ms-settings:` abren la app equivalente.
- Integración continua también en Ubuntu.
- **Paquete `.deb`** para Ubuntu, Debian, Zorin, Mint y Pop!_OS (`scripts/build_deb.sh`). Se
  publica en cada Release con su huella en `SHA256SUMS.txt` y su atestación de compilación.

### Seguridad

- **El celular conectado por USB ya no recibe permisos de administración sin PIN.** Con
  `adb reverse` el celular (y cualquier app instalada en él) llega a la PC como `127.0.0.1`, y
  antes eso bastaba para obtener un token con permiso de edición: una app maliciosa del celular
  podía crear un botón que abriera cualquier programa en la PC. Ahora:
  - Todos los dispositivos, también por USB, se emparejan una vez con el PIN.
  - La propia PC entra al panel con un enlace de un solo uso que abre la bandeja
    (`#admin=…`, caduca en 2 minutos). En modo `--no-tray` el enlace aparece en el log.
  - Editar botones desde el celular requiere activar **Permitir edición desde el celular**.
    Ver el PIN y configurar OBS o Spotify solo es posible desde la propia PC.
  - Los tokens emitidos por versiones anteriores dejan de valer: cada celular vuelve a
    introducir el PIN una vez.

## [0.1.1] - 2026-11-15

### Cambiado

- iPhone y iPad: el menú ya no ofrece "Vibrar al pulsar" ni "Pantalla completa al tocar", que
  iOS no admite, y el consejo para que no se apague la pantalla indica el ajuste de iOS.
- Los iPhone y iPad se emparejan con el nombre "iPhone" o "iPad" en lugar de un texto como
  "CPU iPhone OS 18_7 like Mac OS X".
- `bun run lint` también comprueba la compatibilidad con Safari de iOS 15.

## [0.1.0] - 2026-11-09

Primera versión pública. Durante el desarrollo el proyecto se llamó **PhoneDeck**: al abrir
CellDeck por primera vez se copian los datos de `%APPDATA%\PhoneDeck`, el celular sigue
emparejado y el inicio con Windows pasa a CellDeck.

### Añadido

- Servidor en la PC (FastAPI y WebSocket) que se usa desde el navegador del celular, sin
  instalar apps. Funciona por cable USB (`adb reverse`) o por WiFi con PIN.
- Interfaz del celular compatible desde Chrome 60 (Android antiguos), celulares nuevos,
  tablets, iPhone y iPad. Las columnas se adaptan a cada pantalla.
- Botones para multimedia, volumen, abrir programas y carpetas, enlaces, atajos de teclado,
  macros paso a paso y acciones del sistema (bloquear, apagar la pantalla, suspender).
- Botones de aplicaciones instaladas (Steam, Epic Games, Visual Studio, VS Code, Brave) que las
  encuentran sin rutas fijas y muestran su logotipo oficial.
- Widgets en vivo: Ahora suena con portada, volumen, CPU y RAM, y reloj.
- Volume Mixer por aplicación, temporizador Pomodoro y cambio de escena en OBS (opcional).
- Integración opcional con Spotify. Cada usuario usa su propio Client ID.
- Editor visual desde el celular o la PC: botones, widgets, páginas, iconos, colores y macros.
- `CellDeck.exe` con icono en la bandeja: QR y PIN, conexión USB, modo WiFi, desconectar
  dispositivos e iniciar con Windows.
- Releases automáticas en GitHub: `CellDeck.exe`, `CellDeck-win64.zip`, SHA-256 y atestación
  de compilación.

[Sin publicar]: ../../compare/v0.1.1...HEAD
[0.1.1]: ../../compare/v0.1.0...v0.1.1
[0.1.0]: ../../releases/tag/v0.1.0
