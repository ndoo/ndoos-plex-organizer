FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 CONFIG=/config/config.yml STATE_DIR=/config
WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .
VOLUME /config
ENTRYPOINT ["plex-organizer"]
CMD ["plan"]
