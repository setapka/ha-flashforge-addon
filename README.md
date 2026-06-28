# Flashforge Adventurer 5M Control Panel

Home Assistant Addon для управления 3D-принтером Flashforge Adventurer 5M с прошивкой Klipper/Moonraker.

## Возможности

- 🖨️ **Мониторинг принтера** - отслеживание температуры экструдера и стола, прогресса печати
- 🎮 **Управление печатью** - пауза, возобновление, отмена печати
- 📷 **Веб-камера** - просмотр потока с камеры принтера
- 🔍 **Автообнаружение** - поиск принтеров в локальной сети через mDNS
- 🏠 **Home Assistant интеграция** - MQTT автообнаружение датчиков и кнопок
- 🌓 **Тёмная тема** - автоматическое переключение по системным настройкам

## Установка

### Вариант 1: Добавление репозитория

1. Откройте Home Assistant
2. Перейдите в **Настройки** → **Дополнения** → **Магазин дополнений**
3. Нажмите на три точки в правом верхнем углу → **Репозитории**
4. Добавьте URL репозитория: `https://github.com/username/ha-flashforge-addon`
5. Найдите "Flashforge Adventurer 5M Control Panel" и установите

### Вариант 2: Ручная установка

1. Скопируйте папку `flashforge_adv5m_addon` в `/addons` на вашем Home Assistant
2. В Home Assistant перейдите в **Настройки** → **Дополнения** → **Магазин дополнений**
3. Нажмите на три точки → **Проверить наличие обновлений**
4. Найдите аддон и установите его

## Настройка

### Конфигурация аддона

| Параметр | Описание | По умолчанию |
|----------|----------|--------------|
| `printer_ip` | IP адрес принтера в локальной сети | `` |
| `moonraker_port` | Порт Moonraker API | `7125` |
| `log_level` | Уровень логирования | `info` |

### Поиск принтера

1. Откройте веб-интерфейс аддона
2. Нажмите кнопку **🔍 Автопоиск** для сканирования сети
3. Выберите найденный принтер или введите IP вручную
4. Нажмите **Подключить**

## Home Assistant интеграция

Аддон автоматически создаёт следующие сущности:

### Датчики
- `sensor.extruder_temperature` - Температура экструдера
- `sensor.bed_temperature` - Температура стола
- `sensor.print_progress` - Прогресс печати (%)
- `sensor.print_status` - Статус печати

### Бинарные датчики
- `binary_sensor.filament_detected` - Наличие филамента

### Кнопки
- `button.pause_print` - Пауза печати
- `button.resume_print` - Возобновление печати
- `button.cancel_print` - Отмена печати

## API

### REST API

| Endpoint | Метод | Описание |
|----------|-------|----------|
| `/api/printer/info` | GET | Информация о принтере |
| `/api/printer/data` | GET | Текущие данные принтера |
| `/api/discovery` | GET | Обнаружение принтеров |
| `/api/print/pause` | POST | Пауза печати |
| `/api/print/resume` | POST | Возобновление печати |
| `/api/print/cancel` | POST | Отмена печати |
| `/api/configure` | POST | Настройка подключения |

### WebSocket

Подключение к WebSocket: `ws://[host]:8099/websocket`

Формат сообщений:
```json
{
    "params": {
        "objects": {
            "extruder": {"temperature": 200, "target": 210},
            "heater_bed": {"temperature": 60, "target": 60},
            "display_status": {"progress": 0.5},
            "print_stats": {"state": "printing", "filename": "test.gcode"}
        }
    }
}
```

## Структура проекта

```
flashforge_adv5m_addon/
├── config.yaml          # Конфигурация аддона
├── Dockerfile           # Docker образ
├── requirements.txt     # Python зависимости
├── .dockerignore        # Исключения Docker
├── README.md            # Документация
└── rootfs/
    ├── run.sh           # Скрипт запуска
    └── app/
        ├── main.py      # Backend приложение
        └── static/
            └── index.html  # Frontend интерфейс
```

## Технические требования

- Home Assistant OS 9.0+
- Moonraker на принтере (стандартный порт 7125)
- Сетевой доступ к принтеру

## Лицензия

MIT License

## Поддержка

При возникновении проблем создайте issue в репозитории GitHub.
