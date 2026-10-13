FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 CONFIG=/config/config.yml STATE_DIR=/config
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN apt-get update && apt-get install -y --no-install-recommends p7zip-full && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir .
VOLUME /config
ENTRYPOINT ["plex-organizer"]
CMD ["plan"]
