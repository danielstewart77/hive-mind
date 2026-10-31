FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt requirements-kerberos.txt ./
# python-gssapi has no Linux wheel: it is compiled here against MIT Kerberos,
# whose build tools are removed again once it is installed.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git libgssapi-krb5-2 gcc libkrb5-dev \
    && pip install --no-cache-dir -r requirements.txt -r requirements-kerberos.txt \
    && apt-get purge -y gcc libkrb5-dev \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*

# The hive's data-access layer, from a build context of its own rather than
# from git: the repository is private, so installing it by URL would need a
# credential at build time. `--no-deps` is deliberate — its dependencies are
# declared in requirements.txt above, where this image's resolver can see
# them beside everything else.
COPY --from=hive_data . /tmp/hive-data
RUN pip install --no-cache-dir --no-deps /tmp/hive-data && rm -rf /tmp/hive-data

COPY src/ ./src/

ENV HIVE_HOST=0.0.0.0

CMD ["python", "src/main.py"]
