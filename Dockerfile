FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app/ app/

# Непривилегированный пользователь: контейнер держит read-only доступ к
# БД двух чужих production-ботов, root внутри не нужен. ВАЖНО: ./data
# монтируется с хоста (docker-compose.yml) — если каталог на хосте
# принадлежит root, приложению не хватит прав писать broadcast.db и
# картинки кампаний. Перед первым запуском на хосте:
#   sudo chown -R 1000:1000 /opt/broadcast-admin/data
RUN useradd --uid 1000 --create-home --shell /usr/sbin/nologin appuser \
    && mkdir -p /app/data \
    && chown -R appuser:appuser /app
USER appuser

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8080"]
