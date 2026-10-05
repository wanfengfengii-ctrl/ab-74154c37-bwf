# Docker CLI (static) so the verify step can re-check the app image build.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && arch="$(uname -m)" \
 && curl -fsSL "https://download.docker.com/linux/static/stable/${arch}/docker-27.3.1.tgz" \
      -o /tmp/docker.tgz \
 && tar -xzf /tmp/docker.tgz -C /usr/local/bin --strip-components=1 docker/docker \
 && rm /tmp/docker.tgz \
 && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements-dev.txt /tmp/requirements/
RUN pip install --no-cache-dir \
      -r /tmp/requirements/requirements.txt \
      -r /tmp/requirements/requirements-dev.txt

# The repository is bind-mounted here by docker compose at runtime.
WORKDIR /workspace
CMD ["python", "verify/run.py"]
