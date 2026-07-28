FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

COPY lucent_api/ /app/lucent_api/
COPY server.py /app/
COPY hive_logging.py /app/hive_logging.py

EXPOSE 8424

CMD ["python", "server.py"]
