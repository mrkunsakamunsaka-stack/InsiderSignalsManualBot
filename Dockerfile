# ---- Build stage ----
FROM python:3.11-slim AS build

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# System deps
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Copy and install deps separately for layer caching
COPY requirements.txt .
RUN pip install --upgrade pip \
    && pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

# ---- Runtime stage ----
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install wheels
COPY --from=build /wheels /wheels
RUN pip install --no-cache /wheels/*

# Copy app
COPY manual_bot.py . 

# Non-root user (optional hardening)
RUN useradd -m botuser
USER botuser

# Worker process (no HTTP port needed)
CMD ["python", "manual_bot.py"]
