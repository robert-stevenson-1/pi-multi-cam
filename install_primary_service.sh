#!/bin/bash

# Ensure the script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script as root: sudo ./install_service.sh"
  exit 1
fi

# Get the directory where the script is currently located
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_SERVICE_FILE="$SCRIPT_DIR/picam_primary.service"
SYSTEMD_TARGET="/etc/systemd/system/picam_primary.service"

# Check if the service file exists in the repo
if [ ! -f "$REPO_SERVICE_FILE" ]; then
  echo "Error: Cannot find picam_primary.service in $SCRIPT_DIR"
  echo "Make sure picam_primary.service is in the same directory as this script."
  exit 1
fi

echo "Copying picam_primary.service to /etc/systemd/system/..."
cp "$REPO_SERVICE_FILE" "$SYSTEMD_TARGET"
chmod 644 "$SYSTEMD_TARGET"

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Enabling picam_primary.service to run on boot..."
systemctl enable picam_primary.service

echo "Starting picam_primary.service now..."
systemctl start picam_primary.service

echo "================================================="
echo "Setup complete! Current service status:"
echo "================================================="
systemctl status picam_primary.service --no-pager
