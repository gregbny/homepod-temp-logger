# Minimal image: python-slim, no heavy system dependencies.
FROM python:3.12-slim

WORKDIR /app

# Python dependencies first (better Docker layer caching).
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY static ./static

# Defaults, overridable via docker-compose / Unraid env.
ENV PORT=8088 \
    DB_PATH=/data/homepod.db \
    RETENTION_DAYS=0

# The .db lives in a volume mounted at /data (persistence).
VOLUME ["/data"]
EXPOSE 8088

# uvicorn reads $PORT; shell form so the variable is interpolated.
CMD uvicorn app:app --host 0.0.0.0 --port ${PORT}
