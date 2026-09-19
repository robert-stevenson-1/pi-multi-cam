#!/bin/bash

if [ "$EUID" -ne 0 ]; then
  echo "Please run this script as root: sudo ./install_secondary.sh"
  exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SERVICE_FILE="$SCRIPT_DIR/picam_secondary.service"
SYSTEMD_TARGET="/etc/systemd/system/picam_secondary.service"

if [ ! -f "$REPO_SERVICE_FILE" ]; then
  echo "Error: Cannot find picam_secondary.service in $SCRIPT_DIR"
  exit 1
fi

echo "Copying picam_secondary.service to /etc/systemd/system/..."
cp "$REPO_SERVICE_FILE" "$SYSTEMD_TARGET"
chmod 644 "$SYSTEMD_TARGET"

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling picam_secondary.service to run on boot..."
systemctl enable picam_secondary.service

echo "Starting picam_secondary.service (this will pause for 15 seconds)..."
systemctl start picam_secondary.service

echo "================================================="
echo "Setup complete! Current service status:"
echo "================================================="
systemctl status picam_secondary.service --no-pager
