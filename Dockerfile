# syntax=docker/dockerfile:1.7
FROM node:20-slim AS frontend
WORKDIR /app/frontend
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm ci || npm install
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# tesseract + libs for QR decode
RUN apt-get update && apt-get install -y --no-install-recommends \
    tesseract-ocr libzbar0 libgl1 libglib2.0-0 \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/requirements.txt ./backend/requirements.txt
RUN pip install --no-cache-dir -r backend/requirements.txt

COPY backend ./backend
COPY sync ./sync
COPY scripts ./scripts
COPY --from=frontend /app/frontend/dist ./frontend/dist

# data dirs (manifest lives on persistent disk, evidence under /data if mounted)
RUN mkdir -p /data/s3-live-monitor /app/intelligence/audit
ENV S3LM_HOST=0.0.0.0 S3LM_PORT=8787
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --retries=3 CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8787/api/healthz',timeout=4).read() else 1)"

CMD ["python", "-m", "uvicorn", "backend.app:app", "--host", "0.0.0.0", "--port", "8787"]
