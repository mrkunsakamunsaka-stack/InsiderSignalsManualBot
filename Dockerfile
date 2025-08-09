FROM python:3.11-slim

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY manual_bot.py ./

# Expose the tiny keep-alive HTTP server
EXPOSE 10000

CMD ["python", "manual_bot.py"]
