FROM python:3.11-slim

WORKDIR /app

# ffmpeg — опционально, для инструмента video_edit (p18_video.py).
# Убери эту строку, если он не нужен — сократит образ.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY code/ ./code/
COPY config/ ./config/

# Данные (ключи/чаты/промты/логи) — во внешний volume, чтобы не терялись
# при пересоздании контейнера. Смонтируй сюда volume при `docker run`.
ENV FLUXERAI_DATA_DIR=/data
VOLUME ["/data"]

# Headless-режим: без заставки, без попытки открыть браузер, без debug-автоперезагрузчика.
# Всё это включаемо через те же переменные, что и при обычном локальном запуске —
# скрипт их прекрасно понимает, дефолты в config/settings.py просто рассчитаны
# на запуск разработчиком на своей машине с браузером под рукой.
ENV HOST=0.0.0.0
ENV PORT=5000
ENV DEBUG=0
ENV OPEN_BROWSER=0
ENV SPLASH=0

EXPOSE 5000

CMD ["python", "code/main.py"]
