FROM python:3.12-slim

LABEL org.opencontainers.image.title="Unraid Cache Cleaner"
LABEL org.opencontainers.image.description="A safe Unraid-side qBittorrent orphan cleanup service."
LABEL org.opencontainers.image.source="https://github.com/BWBama85/unraid-cache-cleaner"
LABEL org.opencontainers.image.url="https://github.com/BWBama85/unraid-cache-cleaner"
LABEL org.opencontainers.image.documentation="https://github.com/BWBama85/unraid-cache-cleaner/blob/main/docs/unraid.md"
LABEL org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

# `unar`/`lsar` (Debian `main`, free-licensed) power the opt-in RAR extraction
# feature; they read RAR v5 and multi-volume sets. The Python stdlib has no RAR
# support, so extraction shells out to this binary. Kept off unless
# EXTRACT_ENABLED=true, but installed so the image is ready.
RUN apt-get update \
    && apt-get install -y --no-install-recommends unar \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY pyproject.toml README.md /app/
COPY src /app/src

RUN pip install --no-cache-dir .

VOLUME ["/config", "/data"]

# Plex duplicate report web UI (#34): read-only viewer by default, plus an opt-in
# action layer (WEB_ENABLE_ACTIONS=true) to reclaim duplicates. Served by the `web`
# subcommand, or by `service` when WEB_ENABLED=true. Matches the WEB_PORT default;
# the port is only bound when the UI is actually run. A filesystem delete of an
# untracked copy also needs the Plex media volume mounted (WEB_MEDIA_PATH_MAP);
# that mount is operator-configured, so no fixed media VOLUME is declared here.
EXPOSE 8080

ENTRYPOINT ["unraid-cache-cleaner"]
CMD ["service"]
