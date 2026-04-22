FROM python:3.12-slim

LABEL org.opencontainers.image.title="Unraid Cache Cleaner"
LABEL org.opencontainers.image.description="A safe Unraid-side qBittorrent orphan cleanup service."
LABEL org.opencontainers.image.source="https://github.com/BWBama85/unraid-cache-cleaner"
LABEL org.opencontainers.image.url="https://github.com/BWBama85/unraid-cache-cleaner"
LABEL org.opencontainers.image.documentation="https://github.com/BWBama85/unraid-cache-cleaner/blob/main/docs/unraid.md"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir .

VOLUME ["/config", "/data"]

ENTRYPOINT ["unraid-cache-cleaner"]
CMD ["service"]
