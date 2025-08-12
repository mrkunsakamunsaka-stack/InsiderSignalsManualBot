# ---------- Build stage ----------
FROM python:3.11-slim AS builder

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential ca-certificates curl && \
    rm -rf /var/lib/apt/lists/*

# Copy reqs first for better caching
COPY requirements.txt ./
RUN pip install --upgrade pip && \
    pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---------- Runtime stage ----------
FROM python:3.11-slim AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata && \
    rm -rf /var/lib/apt/lists/*

# Bring in wheels + requirements.txt
COPY --from=builder /wheels /wheels
COPY --from=builder /app/requirements.txt ./requirements.txt

# Install from prebuilt wheels
RUN pip install --no-cache-dir --find-links=/wheels -r requirements.txt && \
    rm -rf /wheels

# Copy the app code
COPY . /app

# Prepare writable state files
RUN useradd -m appuser \
 && chown -R appuser:appuser /app \
 && touch /app/paper.json /app/config.json /app/journal.jsonl \
 && chown appuser:appuser /app/paper.json /app/config.json /app/journal.jsonl

USER appuser

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import os,sys; sys.exit(0 if os.path.exists('/app/manual_bot.py') else 1)"

CMD ["python", "manual_bot.py"]
