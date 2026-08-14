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
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    CHROME_USE_EXISTING=true \
    CHROME_CDP_URL=http://127.0.0.1:9222

WORKDIR /app

# System libs for Pillow / ddddocr; Playwright will install Chromium deps next
RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        gnupg \
        wget \
        libglib2.0-0 \
        libgomp1 \
        libgl1 \
        libsm6 \
        libxext6 \
        libxrender1 \
    && rm -rf /var/lib/apt/lists/*

# Google Chrome Stable（ICRIS 门户 TLS 指纹绕过，比 Playwright Chromium 更强）
RUN wget -q -O - https://dl.google.com/linux/linux_signing_key.pub | gpg --dearmor -o /usr/share/keyrings/google-chrome.gpg \
    && echo "deb [arch=amd64 signed-by=/usr/share/keyrings/google-chrome.gpg] http://dl.google.com/linux/chrome/deb/ stable main" > /etc/apt/sources.list.d/google-chrome.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends google-chrome-stable \
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
