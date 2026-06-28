#!/bin/bash
set -e

# Загрузка конфигурации из options.json
if [ -f "/data/options.json" ]; then
    PRINTER_IP=$(jq -r '.printer_ip // ""' /data/options.json)
    MOONRAKER_PORT=$(jq -r '.moonraker_port // 7125' /data/options.json)
    LOG_LEVEL=$(jq -r '.log_level // "info"' /data/options.json)
    
    export PRINTER_IP
    export MOONRAKER_PORT
    export LOG_LEVEL
fi

echo "Starting Flashforge Adventurer 5M Addon..."
echo "Printer IP: ${PRINTER_IP:-Not configured}"
echo "Moonraker Port: ${MOONRAKER_PORT:-7125}"
echo "Log Level: ${LOG_LEVEL:-info}"

# Запуск приложения
cd /app
exec python3 main.py
