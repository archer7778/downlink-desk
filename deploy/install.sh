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
  num='^-?[0-9]+([.][0-9]+)?$'
  if [ -n "${LAT:-}${LON:-}" ] && ! [[ "${LAT:-}" =~ $num && "${LON:-}" =~ $num ]]; then
    echo "  Those don't look like decimal degrees; skipped. Edit lat and lon in config.toml later."
    LAT=""; LON=""
  fi
  if [ -n "${LAT:-}" ] && [ -n "${LON:-}" ]; then
    sed -i "s/^lat = .*/lat = ${LAT}/; s/^lon = .*/lon = ${LON}/" config.toml
  fi
  echo "  Wrote config.toml (edit it any time, then: sudo systemctl restart downlink-desk-web)"
else
  echo "  Keeping your existing config.toml"
fi
chmod 600 config.toml   # it can hold the operator passcode and your private alert topic
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
for unit in downlink-desk-web.service downlink-desk-fetch.service downlink-desk-fetch.timer \
            downlink-desk-update.service downlink-desk-update.timer; do
  sed -e "s#@ROOT@#${ROOT}#g" -e "s#@USER@#${USER_NAME}#g" "deploy/${unit}" | sudo tee "/etc/systemd/system/${unit}" >/dev/null
done
cfgval() { awk -F= -v k="$1" '$1 ~ "^"k"[ ]*$" {sub(/#.*/,"",$2); gsub(/[ "]/,"",$2); print $2; exit}' config.toml; }
POLL=$(cfgval poll_minutes); POLL=${POLL:-30}
[ "$POLL" -lt 20 ] && POLL=20
sudo sed -i "s#^OnUnitActiveSec=.*#OnUnitActiveSec=${POLL}min#" /etc/systemd/system/downlink-desk-fetch.timer
sudo systemctl daemon-reload
sudo systemctl enable --now downlink-desk-web.service downlink-desk-fetch.timer downlink-desk-update.timer >/dev/null
sudo systemctl restart downlink-desk-web.service

say "5/5  Done"
PORT=$(cfgval port); PORT=${PORT:-8080}
HOST=$(hostname)
LANIP=$(ip -4 -o addr show wlan0 2>/dev/null | awk '{print $4}' | cut -d/ -f1); [ -z "$LANIP" ] && LANIP=$(ip -4 -o addr show eth0 2>/dev/null | awk '{print $4}' | cut -d/ -f1)
echo "  Console:  http://${HOST}.local:${PORT}${LANIP:+   (or http://${LANIP}:${PORT})}"
echo "  First fetch runs within two minutes. To fetch now:  $ROOT/.venv/bin/python -m downlink_desk.fetch --force"
echo "  Starts by itself at every boot, and checks GitHub for updates 5 minutes after boot and nightly."
echo "  Update now: sudo $ROOT/deploy/update.sh    (turn off: auto_update = false under [updates] in config.toml)"
echo "  Logs:     journalctl -u downlink-desk-web -u downlink-desk-fetch -u downlink-desk-update -f"
