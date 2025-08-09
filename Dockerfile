FROM python:3.11-slim
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY requirements.txt /app/
RUN pip install -r requirements.txt
COPY manual_bot.py /app/
CMD ["python", "manual_bot.py"]
