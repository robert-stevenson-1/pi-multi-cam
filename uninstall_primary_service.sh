#!/bin/bash

# Ensure the script is run with sudo
if [ "$EUID" -ne 0 ]; then
  echo "Please run this script as root: sudo ./uninstall_service.sh"
  exit 1
fi

SERVICE_NAME="picam_primary.service"
SYSTEMD_TARGET="/etc/systemd/system/$SERVICE_NAME"

echo "Stopping $SERVICE_NAME..."
systemctl stop "$SERVICE_NAME" 2>/dev/null

echo "Disabling $SERVICE_NAME..."
systemctl disable "$SERVICE_NAME" 2>/dev/null

# Remove the service file if it exists
if [ -f "$SYSTEMD_TARGET" ]; then
  echo "Removing service file $SYSTEMD_TARGET..."
  rm "$SYSTEMD_TARGET"
else
  echo "Service file not found in /etc/systemd/system/, moving on."
fi

echo "Reloading systemd daemon..."
systemctl daemon-reload

echo "Resetting failed systemd states..."
systemctl reset-failed

echo "================================================="
echo "Uninstall complete! $SERVICE_NAME has been cleanly removed."
echo "================================================="
