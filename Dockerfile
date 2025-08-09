FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY manual_bot.py /app/

# Render provides $PORT; our tiny HTTP server binds to it
CMD ["python", "-u", "manual_bot.py"]
