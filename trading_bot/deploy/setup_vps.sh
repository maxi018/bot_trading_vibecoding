#!/usr/bin/env bash
# Provisión básica de un VPS Ubuntu 22.04/24.04 para quantbot.
# Revisar ANTES de ejecutar. Correr como root:  bash deploy/setup_vps.sh
set -euo pipefail

APP_DIR=/opt/quantbot
STATE_DIR=/var/lib/quantbot

echo "==> Paquetes base + reloj sincronizado (timestamps correctos = firmas válidas)"
apt-get update
apt-get install -y python3 python3-venv python3-pip git ufw fail2ban chrony unattended-upgrades
systemctl enable --now chrony
timedatectl set-timezone UTC

echo "==> Usuario de servicio sin shell"
id quantbot &>/dev/null || useradd --system --home "$APP_DIR" --shell /usr/sbin/nologin quantbot

echo "==> Firewall: solo SSH entrante (el bot solo hace conexiones salientes)"
ufw default deny incoming
ufw default allow outgoing
ufw allow OpenSSH
ufw --force enable

echo "==> SSH: sin password, sin root"
sed -i 's/^#\?PasswordAuthentication .*/PasswordAuthentication no/' /etc/ssh/sshd_config
sed -i 's/^#\?PermitRootLogin .*/PermitRootLogin no/' /etc/ssh/sshd_config
systemctl reload ssh || systemctl reload sshd

echo "==> Código + venv"
mkdir -p "$APP_DIR" "$STATE_DIR" /etc/quantbot
if [ ! -d "$APP_DIR/quantbot" ]; then
  echo "Copiar el directorio trading_bot/ a $APP_DIR (rsync/git) y re-ejecutar."; exit 1
fi
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade pip
"$APP_DIR/.venv/bin/pip" install "$APP_DIR[live]"
mkdir -p "$APP_DIR/state"
chown -R quantbot:quantbot "$APP_DIR" "$STATE_DIR"

echo "==> Secretos (solo root y quantbot pueden leer)"
if [ ! -f /etc/quantbot/quantbot.env ]; then
  cp "$APP_DIR/.env.example" /etc/quantbot/quantbot.env
fi
chown root:quantbot /etc/quantbot/quantbot.env
chmod 640 /etc/quantbot/quantbot.env

echo "==> systemd"
cp "$APP_DIR/deploy/systemd/quantbot.service" /etc/systemd/system/
systemctl daemon-reload
systemctl enable quantbot

cat <<'MSG'
Listo. Próximos pasos:
  1. Editar /etc/quantbot/quantbot.env (API key SIN permiso de retiro + whitelist de IP).
  2. Correr tests:      sudo -u quantbot /opt/quantbot/.venv/bin/python -m pytest /opt/quantbot/tests
  3. Arrancar (paper):  systemctl start quantbot && journalctl -u quantbot -f
MSG
