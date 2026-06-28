# Инструкция по установке и запуску через GitHub

## Шаг 1: Создание GitHub репозитория

1. Создайте новый публичный репозиторий на GitHub (например, `ha-flashforge-addon`)
2. Клонируйте репозиторий на свой компьютер:
   ```bash
   git clone https://github.com/ВАШ_USERNAME/ha-flashforge-addon.git
   ```

## Шаг 2: Копирование файлов аддона

Скопируйте все файлы из папки `flashforge_adv5m_addon` в корень вашего GitHub репозитория:

```bash
cp -r flashforge_adv5m_addon/* /путь/к/вашему/репозиторию/
```

Или вручную создайте файлы в репозитории.

## Шаг 3: Настройка repository.json

Откройте файл `repository.json` и замените:
- `username` на ваш GitHub username
- `your.email@example.com` на ваш email

```json
{
  "name": "Flashforge Adventurer 5M Addon Repository",
  "url": "https://github.com/ВАШ_USERNAME/ha-flashforge-addon",
  "maintainer": "Ваше Имя <your.email@example.com>",
  ...
  "image": "ghcr.io/ВАШ_USERNAME/ha-flashforge-addon/{arch}"
}
```

## Шаг 4: Включение GitHub Actions

1. Перейдите в репозиторий на GitHub
2. Нажмите **Settings** → **Actions** → **General**
3. В разделе "Permissions" выберите **Read and write permissions**
4. Включите **Allow GitHub Actions to create and approve pull requests**

## Шаг 5: Публикация репозитория

Закоммитьтьте и отправьте все файлы:

```bash
git add .
git commit -m "Initial commit: Flashforge Adventurer 5M Addon"
git push origin main
```

## Шаг 6: Настройка GitHub Container Registry (GHCR)

1. Перейдите в **Settings** → **Packages**
2. Убедитесь, что пакет `ha-flashforge-addon` создан после первого запуска workflow
3. Установите видимость пакета **Public** (для публичного доступа)

## Шаг 7: Добавление в Home Assistant

### Вариант A: Через GitHub Pages (рекомендуется)

1. В репозитории перейдите в **Settings** → **Pages**
2. В разделе "Source" выберите **GitHub Actions**
3. После первого запуска workflow ваш репозиторий будет доступен как:
   ```
   https://ВАШ_USERNAME.github.io/ha-flashforge-addon/
   ```

4. В Home Assistant:
   - Откройте **Настройки** → **Дополнения** → **Магазин дополнений**
   - Нажмите на три точки → **Репозитории**
   - Добавьте URL: `https://ВАШ_USERNAME.github.io/ha-flashforge-addon/`
   - Нажмите **Добавить**

### Вариант B: Прямая ссылка на raw файл

1. В Home Assistant:
   - Откройте **Настройки** → **Дополнения** → **Магазин дополнений**
   - Нажмите на три точки → **Репозитории**
   - Добавьте URL: `https://raw.githubusercontent.com/ВАШ_USERNAME/ha-flashforge-addon/main/`
   - Нажмите **Добавить**

## Шаг 8: Установка аддона

1. После добавления репозитория найдите **Flashforge Adventurer 5M Control Panel**
2. Нажмите **Установить**
3. Дождитесь завершения установки
4. Перейдите на вкладку **Конфигурация**
5. Введите IP адрес вашего принтера
6. Нажмите **Сохранить**
7. Вернитесь на вкладку **Инфо** и нажмите **Запустить**

## Шаг 9: Проверка работы

1. После запуска откройте веб-интерфейс аддона
2. Проверьте подключение к принтеру
3. Настройте интеграцию с Home Assistant через MQTT

## Автоматическая сборка через GitHub Actions

При каждом пуше в ветку `main` или создании тега версии:

1. **Push в main** → сборка dev-версии
2. **Тег v1.0.0** → сборка релизной версии

### Создание релиза:

```bash
git tag v1.0.0
git push origin v1.0.0
```

Это запустит workflow, который:
- Соберёт Docker-образы для всех архитектур (aarch64, amd64, armv7)
- Опубликует образы в GHCR
- Обновит GitHub Pages

## Troubleshooting

### Ошибка "Repository not valid"
- Убедитесь, что `repository.json` доступен по прямой ссылке
- Проверьте, что файл содержит правильный JSON

### Ошибка сборки Docker
- Проверьте логи GitHub Actions
- Убедитесь, что все файлы на месте (Dockerfile, requirements.txt, rootfs/)

### Аддон не запускается
- Проверьте логи аддона в Home Assistant
- Убедитесь, что IP адрес принтера указан верно
- Проверьте, что порт 7125 открыт на принтере
