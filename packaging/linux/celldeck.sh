#!/bin/sh
# Lanzador del paquete .deb: recursos en /opt/celldeck y venv propio (ver postinst).
export CELLDECK_RESOURCES=/opt/celldeck
export CELLDECK_LAUNCHER=/usr/bin/celldeck
exec /opt/celldeck/venv/bin/python -m celldeck "$@"
