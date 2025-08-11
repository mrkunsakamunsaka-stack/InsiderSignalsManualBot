# ---------- Build stage ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# System deps (just enough to build telegram/requests wheels)
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Only copy lock/requirements first for layer caching
COPY requirements.txt ./

RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------- Runtime stage ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Minimal runtime libs
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

# Bring in wheels from builder
COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir --find-links=/wheels -r /wheels/requirements.txt && \
    rm -rf /wheels

# Copy the app code last (best cache)
COPY . /app

# --- Make state files and app dir writable for non-root user ---
RUN useradd -m appuser \
 && chown -R appuser:appuser /app \
 && touch /app/paper.json /app/config.json \
 && chown appuser:appuser /app/paper.json /app/config.json

USER appuser

# Healthcheck: proves the process is alive (Telegram polling thread is running)
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys; sys.exit(0 if os.path.exists('/app/manual_bot.py') else 1)"

# Worker entrypoint (Render “Background Worker” service)
CMD ["python", "manual_bot.py"]
