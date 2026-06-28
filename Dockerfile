FROM python:3.11-alpine

# Метаданные
LABEL maintainer="Flashforge Addon Developer"
LABEL description="Flashforge Adventurer 5M Control Panel for Home Assistant"

# Установка системных зависимостей
RUN apk add --no-cache \
    nginx \
    openssl \
    curl \
    jq \
    zeroconf \
    avahi-libs

# Установка Python-зависимостей
COPY requirements.txt /tmp/
RUN pip install --no-cache-dir -r /tmp/requirements.txt

# Копирование приложения
COPY rootfs/ /

# Настройка прав
RUN chmod a+x /run.sh

# Рабочая директория
WORKDIR /app

# Переменные окружения
ENV PYTHONUNBUFFERED=1

EXPOSE 8099

CMD ["/run.sh"]
