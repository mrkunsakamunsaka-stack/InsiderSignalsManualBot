FROM python:3.10-slim

WORKDIR /app
COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY manual_bot.py /app/

CMD ["python", "-u", "manual_bot.py"]

