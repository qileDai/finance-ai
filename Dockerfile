# syntax=docker/dockerfile:1

# ---- Frontend build ----
FROM node:20-bookworm-slim AS frontend
WORKDIR /src/web/admin
COPY web/admin/package.json web/admin/package-lock.json* ./
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi
COPY web/admin/ ./
RUN npm run build

# ---- Runtime ----
FROM python:3.12-slim-bookworm AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    PIP_NO_CACHE_DIR=1 \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

WORKDIR /app

# System libs for Pillow / ddddocr; Playwright will install Chromium deps next
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        libglib2.0-0 \
        libgomp1 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --upgrade pip \
    && pip install -r requirements.txt \
    && playwright install --with-deps chromium

# App source (static/admin filled from frontend stage)
COPY config ./config
COPY src ./src
COPY templates ./templates
COPY main.py ./
COPY docs ./docs
RUN mkdir -p /app/static /app/data/materials
COPY --from=frontend /src/static/admin /app/static/admin

EXPOSE 8081 8082

CMD ["python", "main.py", "wework-external-bot"]
