#!/usr/bin/env bash
# Construye dist/celldeck_<versión>_amd64.deb (Ubuntu, Debian, Zorin, Mint, Pop!_OS...).
#
# El paquete lleva el código como wheel y TODAS sus dependencias como wheels para Python
# 3.11, 3.12 y 3.13 (x86_64). Al instalarlo, postinst crea /opt/celldeck/venv sin conexión,
# con las versiones fijadas en requirements/linux.txt. PyGObject se toma del sistema.
#
# Uso:
#   bash scripts/build_deb.sh           # construye el paquete
#   bash scripts/build_deb.sh --lock    # regenera requirements/linux.txt desde linux.in
#
# Requisitos: python3 (>= 3.11) con pip y dpkg-deb. Necesita Internet para descargar wheels.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
PYTHON="${CELLDECK_PYTHON:-python3}"
PY_VERSIONS=(3.11 3.12 3.13)
PLATFORMS=(manylinux_2_17_x86_64 manylinux2014_x86_64 manylinux_2_28_x86_64 linux_x86_64)

cd "$ROOT"

if [[ "${1:-}" == "--lock" ]]; then
    report="$(mktemp)"
    trap 'rm -f "$report"' EXIT
    "$PYTHON" -m pip install --dry-run --ignore-installed --quiet --disable-pip-version-check \
        --report "$report" -r requirements/linux.in
    {
        echo "# Generado por: bash scripts/build_deb.sh --lock   (no editar a mano)"
        "$PYTHON" -c '
import json, sys
report = json.load(open(sys.argv[1]))
for name, version in sorted((i["metadata"]["name"].lower(), i["metadata"]["version"])
                            for i in report["install"]):
    print(f"{name}=={version}")
' "$report"
    } > requirements/linux.txt
    echo "requirements/linux.txt actualizado"
    exit 0
fi

VERSION="$("$PYTHON" -c "import sys; sys.path.insert(0, 'src'); import celldeck; print(celldeck.__version__)")"
# Debian no admite "-" en la versión de origen sin revisión: 0.2.0-beta.1 → 0.2.0~beta.1
DEB_VERSION="${VERSION//-/\~}"
PKG="celldeck_${DEB_VERSION}_amd64"
BUILD="$(mktemp -d)"
trap 'rm -rf "$BUILD"' EXIT
STAGE="$BUILD/$PKG"
OPT="$STAGE/opt/celldeck"

echo "==> CellDeck $VERSION → dist/$PKG.deb"
mkdir -p "$OPT/wheels" "$STAGE/DEBIAN" "$STAGE/usr/bin" "$STAGE/usr/share/applications" \
    "$STAGE/usr/share/icons/hicolor/256x256/apps" "$STAGE/usr/share/doc/celldeck"

echo "==> Wheel de CellDeck"
"$PYTHON" -m pip wheel --quiet --disable-pip-version-check --no-deps -w "$OPT/wheels" .

echo "==> Wheels de las dependencias (Python ${PY_VERSIONS[*]})"
platform_args=()
for p in "${PLATFORMS[@]}"; do platform_args+=(--platform "$p"); done
for v in "${PY_VERSIONS[@]}"; do
    "$PYTHON" -m pip download --quiet --disable-pip-version-check --only-binary=:all: \
        --implementation cp --python-version "$v" "${platform_args[@]}" \
        -d "$OPT/wheels" -r requirements/linux.txt
done

echo "==> Recursos"
grep -v '^#' requirements/linux.txt > "$OPT/requirements.txt"
cp -r web "$OPT/web"
mkdir -p "$OPT/assets"
cp assets/celldeck.png "$OPT/assets/"
cp assets/celldeck.png "$STAGE/usr/share/icons/hicolor/256x256/apps/celldeck.png"
install -m 0755 packaging/linux/celldeck.sh "$STAGE/usr/bin/celldeck"
install -m 0644 packaging/linux/celldeck.desktop "$STAGE/usr/share/applications/"
install -m 0644 LICENSE "$STAGE/usr/share/doc/celldeck/copyright"
install -m 0755 packaging/linux/postinst packaging/linux/prerm "$STAGE/DEBIAN/"

SIZE_KB="$(du -sk "$STAGE" | cut -f1)"
cat > "$STAGE/DEBIAN/control" <<EOF
Package: celldeck
Version: $DEB_VERSION
Section: utils
Priority: optional
Architecture: amd64
Maintainer: CellDeck contributors <noreply@users.noreply.github.com>
Installed-Size: $SIZE_KB
Depends: python3 (>= 3.11), python3 (<< 3.14), python3-venv, python3-gi, gir1.2-ayatanaappindicator3-0.1, libpulse0
Recommends: zenity | kdialog, xdg-utils, libglib2.0-bin
Suggests: adb, ydotool
Homepage: https://github.com/YonaldyMarrero/CellDeck
Description: Tu celular viejo, convertido en un Stream Deck para tu PC
 CellDeck sirve una página web que se abre en el navegador del celular (por cable USB
 con adb o por WiFi con PIN) y la convierte en un panel de botones: multimedia,
 volumen por aplicación, abrir programas, atajos de teclado, macros, Pomodoro, OBS
 y Spotify. En el celular no se instala nada.
EOF

# Permisos normales de paquete aunque el umask local sea 002 (nada escribible por el grupo).
chmod -R u+rwX,go+rX,go-w "$STAGE"

mkdir -p dist
dpkg-deb --root-owner-group --build "$STAGE" "dist/$PKG.deb" >/dev/null
echo "==> Listo: dist/$PKG.deb ($(du -h "dist/$PKG.deb" | cut -f1))"
