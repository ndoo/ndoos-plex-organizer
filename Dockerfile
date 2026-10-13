FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 CONFIG=/config/config.yml STATE_DIR=/config
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
# unrar is in Debian's non-free section; it is only fetched at build time and used by `unpack`
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends p7zip-full unrar \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir .
VOLUME /config
ENTRYPOINT ["plex-organizer"]
CMD ["plan"]
