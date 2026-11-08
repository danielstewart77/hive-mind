FROM python:3.12-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /app/
RUN pip install --no-cache-dir -r requirements.txt

# The hive's data-access layer, from a build context of its own rather than
# from git: the repository is private, so installing it by URL would need a
# credential at build time. `--no-deps` is deliberate — its dependencies are
# declared in requirements.txt above, where this image's resolver can see
# them beside everything else.
COPY --from=hive_data . /tmp/hive-data
RUN pip install --no-cache-dir --no-deps /tmp/hive-data && rm -rf /tmp/hive-data

COPY lucent_api/ /app/lucent_api/
COPY server.py /app/
COPY hive_logging.py /app/hive_logging.py

EXPOSE 8424

CMD ["python", "server.py"]
