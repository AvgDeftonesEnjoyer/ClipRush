FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tesseract-ocr libchromaprint-tools ca-certificates && rm -rf /var/lib/apt/lists/*
COPY requirements.txt requirements-media.txt ./
RUN pip install --no-cache-dir -r requirements.txt -r requirements-media.txt && useradd -m -u 10001 worker
COPY cliprush ./cliprush
COPY alembic.ini ./alembic.ini
COPY migrations ./migrations
RUN mkdir /data && chown 10001:10001 /data
USER worker
EXPOSE 8080
CMD ["uvicorn", "cliprush.api:app", "--host", "0.0.0.0", "--port", "8080"]
