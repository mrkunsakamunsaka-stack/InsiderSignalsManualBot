FROM python:3.11-slim

WORKDIR /app

# System deps to make numpy wheels happy + tzdata for pytz
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential curl ca-certificates tzdata \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY manual_bot.py ./

# Render listens on $PORT; our tiny HTTP server binds to it for keep-alive.
ENV PORT=10000

CMD ["python", "manual_bot.py"]
