#!/bin/bash
set -e

# Загрузка конфигурации из options.json
if [ -f "/data/options.json" ]; then
    PRINTER_IP=$(jq -r '.printer_ip // ""' /data/options.json)
    PRINTER_PORT=$(jq -r '.printer_port // 8899' /data/options.json)
    SCAN_PORTS=$(jq -r '.scan_ports // "8899"' /data/options.json)
    SCAN_SUBNET=$(jq -r '.scan_subnet // "192.168.1.0/24"' /data/options.json)
    LOG_LEVEL=$(jq -r '.log_level // "info"' /data/options.json)
    
    export PRINTER_IP
    export PRINTER_PORT
    export SCAN_PORTS
    export SCAN_SUBNET
    export LOG_LEVEL
fi

echo "Starting Flashforge Adventurer 5M Addon..."
echo "Printer IP: ${PRINTER_IP:-Not configured}"
echo "Printer Port: ${PRINTER_PORT:-8899}"
echo "Scan Ports: ${SCAN_PORTS:-8899}"
echo "Scan Subnet: ${SCAN_SUBNET:-192.168.1.0/24}"
echo "Log Level: ${LOG_LEVEL:-info}"

# Запуск приложения
cd /app
exec python3 main.py
