FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY comms/requirements.txt /app/comms/requirements.txt
RUN pip install --no-cache-dir -r /app/comms/requirements.txt

COPY comms/ /app/comms/
COPY hive_logging.py /app/hive_logging.py

EXPOSE 8424

CMD ["python", "-m", "comms.server"]
