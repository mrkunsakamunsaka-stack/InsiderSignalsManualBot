# Smaller, safer Python image
FROM python:3.11-slim

# System deps (build tools not needed for these libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
      curl ca-certificates tzdata tini \
    && rm -rf /var/lib/apt/lists/*

# Working dir
WORKDIR /app

# Copy only what we need first (better layer caching)
COPY requirements.txt /app/requirements.txt

# Install Python deps (no cache -> smaller image)
RUN pip install --no-cache-dir -r /app/requirements.txt

# Copy app
COPY manual_bot.py /app/manual_bot.py

# Non-root user
RUN useradd -m botuser
USER botuser

# Environment for Python
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

# Use tini as init to reap zombies
ENTRYPOINT ["/usr/bin/tini", "--"]

# Start the worker
CMD ["python", "manual_bot.py"]
