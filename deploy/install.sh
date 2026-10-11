#!/usr/bin/env bash
# Downlink Desk installer for Raspberry Pi OS (Lite) and other Debian-based Linux.
# Run from the repository folder:   ./deploy/install.sh
# Safe to run again; it keeps your config.toml, API key and data.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_NAME="$(id -un)"
cd "$ROOT"

say() { printf '\n\033[1m%s\033[0m\n' "$*"; }

say "1/5  Installing system packages (needs your password once)"
sudo apt-get update -qq
sudo apt-get install -y -qq python3 python3-venv python3-pip sqlite3 >/dev/null

say "2/5  Creating a Python environment and installing decoders (slow on a Pi Zero, be patient)"
python3 -m venv .venv
.venv/bin/pip install -q --upgrade pip
.venv/bin/pip install -q -r requirements.txt

say "3/5  Configuration"
mkdir -p data secrets
chmod 700 secrets
if [ ! -f config.toml ]; then
  cp config.example.toml config.toml
  read -r -p "  Your latitude in decimal degrees (e.g. 51.48, or Enter to skip): " LAT || true
  read -r -p "  Your longitude in decimal degrees (e.g. -0.01, or Enter to skip): " LON || true
  if [ -n "${LAT:-}" ] && [ -n "${LON:-}" ]; then
    sed -i "s/^lat = .*/lat = ${LAT}/; s/^lon = .*/lon = ${LON}/" config.toml
  fi
  echo "  Wrote config.toml (edit it any time, then: sudo systemctl restart downlink-desk-web)"
else
  echo "  Keeping your existing config.toml"
fi
if [ ! -s secrets/satnogs_token ]; then
  echo "  Your SatNOGS DB API key: log in at https://db.satnogs.org, click your avatar,"
  echo "  then Settings / API Token. (Press Enter to skip and add it later.)"
  read -r -s -p "  API key: " TOKEN || true; echo
  if [ -n "${TOKEN:-}" ]; then
    printf '%s\n' "$TOKEN" > secrets/satnogs_token
    chmod 600 secrets/satnogs_token
    echo "  Saved to secrets/satnogs_token"
  else
    echo "  Skipped. Later: echo YOUR_KEY > $ROOT/secrets/satnogs_token && chmod 600 $ROOT/secrets/satnogs_token"
  fi
fi

say "4/5  Installing background services"
for unit in downlink-desk-web.service downlink-desk-fetch.service downlink-desk-fetch.timer; do
  sed -e "s#@ROOT@#${ROOT}#g" -e "s#@USER@#${USER_NAME}#g" "deploy/${unit}" | sudo tee "/etc/systemd/system/${unit}" >/dev/null
done
cfgval() { awk -F= -v k="$1" '$1 ~ "^"k"[ ]*$" {sub(/#.*/,"",$2); gsub(/[ "]/,"",$2); print $2; exit}' config.toml; }
POLL=$(cfgval poll_minutes); POLL=${POLL:-30}
[ "$POLL" -lt 20 ] && POLL=20
sudo sed -i "s#^OnUnitActiveSec=.*#OnUnitActiveSec=${POLL}min#" /etc/systemd/system/downlink-desk-fetch.timer
sudo systemctl daemon-reload
sudo systemctl enable --now downlink-desk-web.service downlink-desk-fetch.timer >/dev/null

say "5/5  Done"
PORT=$(cfgval port); PORT=${PORT:-8080}
HOST=$(hostname)
echo "  Console:  http://${HOST}.local:${PORT}   (or http://$(hostname -I | awk '{print $1}'):${PORT})"
echo "  First fetch runs within two minutes. To fetch now:  $ROOT/.venv/bin/python -m downlink_desk.fetch --force"
echo "  Logs:     journalctl -u downlink-desk-web -u downlink-desk-fetch -f"
