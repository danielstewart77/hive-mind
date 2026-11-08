FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY comms/requirements.txt /app/comms/requirements.txt
RUN pip install --no-cache-dir -r /app/comms/requirements.txt

# The hive's data-access layer: comms reads and writes the one database
# through it and opens no file of its own. From a build context rather than
# git because the repository is private; `--no-deps` because its one
# dependency, asyncpg, is in comms/requirements.txt beside everything else.
COPY --from=hive_data . /tmp/hive-data
RUN pip install --no-cache-dir --no-deps /tmp/hive-data && rm -rf /tmp/hive-data

COPY comms/ /app/comms/
COPY hive_logging.py /app/hive_logging.py

EXPOSE 8424

CMD ["python", "-m", "comms.server"]
