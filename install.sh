#!/usr/bin/env bash
# Instala el mando de TV: crea el entorno virtual e instala las dependencias.
#   ./install.sh            -> solo instala
#   ./install.sh --service  -> instala y configura el arranque automático (sudo)
set -euo pipefail

DIR="$(cd "$(dirname "$(readlink -f "$0")")" && pwd)"
cd "$DIR"

PYTHON="${PYTHON:-python3}"

if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "No se encontró $PYTHON. Instálalo, por ejemplo:" >&2
  echo "  sudo apt install python3 python3-venv" >&2
  exit 1
fi

if ! "$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)'; then
  echo "Se necesita Python 3.10 o superior (tienes $("$PYTHON" -V 2>&1))." >&2
  exit 1
fi

if [ ! -d .venv ]; then
  echo "Creando entorno virtual…"
  "$PYTHON" -m venv .venv
fi

echo "Instalando dependencias…"
./.venv/bin/python -m pip install --quiet --upgrade pip
./.venv/bin/python -m pip install --quiet -r requirements.txt

chmod +x tv tvweb tvapp

if [ "${1:-}" = "--service" ]; then
  RUN_USER="${SUDO_USER:-$(id -un)}"
  RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
  RUN_GROUP="$(id -gn "$RUN_USER")"
  UNIT=/etc/systemd/system/tvremote.service

  echo "Configurando servicio systemd para el usuario $RUN_USER…"
  sudo tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=Mando web Xiaomi TV (Android TV remote)
Documentation=file:$DIR/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_GROUP
WorkingDirectory=$DIR
Environment=HOME=$RUN_HOME
Environment=PYTHONUNBUFFERED=1
ExecStart=$DIR/.venv/bin/python $DIR/webremote.py --port 8765
Restart=always
RestartSec=5
TimeoutStopSec=10
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

  sudo systemctl daemon-reload
  sudo systemctl enable --now tvremote.service
  echo "Servicio activo y habilitado al arranque (systemctl status tvremote)."
fi

cat <<'EOF'

Instalado. Siguiente paso:

  1) Empareja con la TV (muestra un código de 6 dígitos):
       ./tv pair --host <IP de la TV>

  2) Arranca el mando web:
       ./tvweb
     y abre http://<IP-del-PC>:8765/

     O abre la ventana de app de escritorio:
       ./tvapp

Para arrancar solo al encender el PC:
  ./install.sh --service
EOF
