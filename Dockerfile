# Slim Python base
FROM python:3.11-slim

# System deps (certs, tzdata for Europe/Dublin correctness)
RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates tzdata curl && \
    rm -rf /var/lib/apt/lists/*

# Workdir
WORKDIR /app

# Copy requirements first (better layer caching)
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app
COPY manual_bot.py /app/manual_bot.py

# Env niceties
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Render sets PORT; our script exposes a tiny health server automatically.
CMD ["python", "-u", "manual_bot.py"]
