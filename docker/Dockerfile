# Образ приложения VELOR — используется и веб-сервером (FastAPI), и Telegram-ботом.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Europe/Moscow

# Системные пакеты:
#  - fonts-dejavu-core — кириллица в PDF-экспорте (reportlab ищет DejaVuSans)
#  - tzdata — корректные метки времени в логах
RUN apt-get update \
 && apt-get install -y --no-install-recommends fonts-dejavu-core tzdata \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Сначала зависимости — чтобы слой кэшировался и пересобирался редко.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Затем код приложения.
COPY . .

# Каталоги под данные/логи/загрузки (наполняются томами в docker-compose).
RUN mkdir -p /app/data /app/logs /app/uploads \
 && chmod +x docker/entrypoint.sh

ENTRYPOINT ["sh", "docker/entrypoint.sh"]
# По умолчанию — веб-сервер. Бот переопределяет команду в docker-compose.
CMD ["uvicorn", "server:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips=*"]
