# CellDeck en Linux

CellDeck funciona en Linux con las mismas funciones que en Windows. Se ha probado en
**Zorin OS 18 (GNOME sobre X11, PipeWire)** y debería funcionar en cualquier escritorio
moderno: Ubuntu, Debian, Fedora, Mint, Pop!_OS, KDE Plasma, XFCE, Cinnamon…

## Qué usa por debajo

| Función | Cómo lo hace | Requisito |
|---|---|---|
| Volumen general y **Volume Mixer** | PulseAudio o PipeWire (`pipewire-pulse`) | `libpulse0` (viene instalada) |
| Ahora suena, play/pausa, siguiente | MPRIS por D-Bus (Spotify, Firefox, Chrome, Brave, VLC, Rhythmbox…) | Ninguno |
| Atajos de teclado y escribir texto | **X11:** XTEST. **Wayland:** `ydotool` | En Wayland, instalar `ydotool` |
| Abrir programas, carpetas, documentos y URLs | `gio` / `xdg-open` y archivos `.desktop` | Ninguno |
| Apps instaladas (Steam, VS Code, Brave…) | Archivos `.desktop` del sistema, Flatpak y Snap | Ninguno |
| Logotipos de las apps | Tema de iconos (PNG y, con PyGObject, SVG) | `python3-gi` para los SVG |
| Bloquear, suspender, apagar, reiniciar | `loginctl` y `systemctl` (los permisos los da polkit) | Ninguno |
| Apagar la pantalla | `xset dpms force off` | Solo X11 |
| Icono de la bandeja con menú | AppIndicator | `python3-gi` y `gir1.2-ayatanaappindicator3-0.1`; en GNOME, la extensión *AppIndicator* (Ubuntu y Zorin ya la traen) |
| Diálogos (activar WiFi, etc.) | `zenity` o `kdialog` | Viene con el escritorio |
| Iniciar al encender | `~/.config/autostart/celldeck.desktop` | Ninguno |

La configuración se guarda en `~/.config/celldeck/` (o `$XDG_CONFIG_HOME/celldeck`).

## Instalar desde el código

En Ubuntu, Zorin, Debian, Mint o Pop!_OS:

```bash
sudo apt install python3-venv python3-gi gir1.2-ayatanaappindicator3-0.1 git
git clone https://github.com/YonaldyMarrero/CellDeck.git
cd CellDeck
# --system-site-packages: usa el PyGObject del sistema (bandeja e iconos SVG), que no se
# instala bien con pip porque hay que compilarlo.
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -e .
.venv/bin/python -m celldeck
```

En Fedora: `sudo dnf install python3-gobject libayatana-appindicator-gtk3 git` y los mismos
pasos.

Aparece el icono de CellDeck en la barra superior. **Abrir panel** abre el panel en el navegador
de la PC. **Mostrar QR / PIN** muestra cómo conectar el celular.

Sin bandeja (servidor, o un escritorio sin AppIndicator): `.venv/bin/python -m celldeck --no-tray`.
El log muestra un enlace de un solo uso para abrir el panel desde la PC.

## Conectar el celular

Es igual que en Windows: por cable con `adb reverse` (ver [usb.md](usb.md)) o por WiFi con el
PIN. En Linux, `adb` se busca en el `PATH`, en `~/Android/Sdk/platform-tools` y en
`/usr/lib/android-sdk/platform-tools`. Se instala con `sudo apt install adb`.

Por WiFi, si usas un cortafuegos (`ufw`), abre el puerto solo para tu red local:

```bash
sudo ufw allow from 192.168.0.0/16 to any port 8765 proto tcp
```

## Wayland

GNOME y KDE en Wayland no dejan que una aplicación pulse teclas en las ventanas de otras. Por
eso los **atajos de teclado y las macros que escriben texto** necesitan `ydotool`:

```bash
sudo apt install ydotool
sudo systemctl enable --now ydotool   # en algunas distribuciones el servicio es ydotoold
```

Lo demás (volumen, mezclador, multimedia, abrir programas, apagar…) funciona igual en X11 y en
Wayland. Si prefieres no instalar nada, elige **"GNOME en Xorg"** en la pantalla de inicio de
sesión.

## Una configuración, dos sistemas

Los botones de una configuración hecha en Windows funcionan en Linux:

| En Windows | En Linux abre |
|---|---|
| `explorer.exe` | La carpeta personal en el gestor de archivos |
| `notepad.exe` | El editor de texto (GNOME Text Editor, gedit, Kate…) |
| `calc.exe` | La calculadora |
| `cmd.exe`, `powershell.exe`, `wt.exe` | La terminal |
| `taskmgr.exe` | El monitor del sistema |
| `ms-settings:` | La configuración del sistema |
| Atajos con `win` | La tecla Super |

"Epic Games Launcher" no existe en Linux: el botón abre **Heroic Games Launcher** si está
instalado. Visual Studio tampoco existe en Linux.
