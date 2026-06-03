#!/usr/bin/env bash
#
# Show how the cleaner and qBittorrent containers are wired: their networks,
# their /data mounts, and the cleaner's QBITTORRENT_URL. Read-only.
#
# Usage (on the Unraid server):
#   bash inspect-mounts.sh
#
# Override names if needed:
#   CLEANER=<name> QBIT=<name> bash inspect-mounts.sh

CLEANER="${CLEANER:-unraid-cache-cleaner}"
QBIT="${QBIT:-qbittorrent}"

data_source() {
  docker inspect -f \
    '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' \
    "$1" 2>/dev/null
}

show() {
  local c="$1"
  echo "=== ${c} ==="
  if ! docker inspect "${c}" >/dev/null 2>&1; then
    echo "  (container not found)"
    return
  fi
  echo "  running:  $(docker inspect -f '{{.State.Running}}' "${c}")"
  echo "  networks: $(docker inspect -f '{{range $k,$v := .NetworkSettings.Networks}}{{$k}} {{end}}' "${c}")"
  echo "  /data  ->  $(data_source "${c}")"
  echo "  QBITTORRENT_URL: $(docker exec "${c}" printenv QBITTORRENT_URL 2>/dev/null || echo n/a)"
  echo "  all mounts:"
  docker inspect -f '{{range .Mounts}}    {{.Source}}  ->  {{.Destination}}{{println}}{{end}}' "${c}"
}

echo "running containers:"
docker ps --format '  {{.Names}}   ({{.Image}})'
echo

show "${CLEANER}"
echo
show "${QBIT}"
echo

src="$(data_source "${CLEANER}")"
if [ -n "${src}" ]; then
  echo "cleaner's /data source on the host: ${src}"
  echo "first entries there:"
  ls -1 "${src}" 2>/dev/null | head -10 || echo "  (cannot list it / empty)"
else
  echo "cleaner has NO /data mount configured."
fi

qsrc="$(data_source "${QBIT}")"
if [ -n "${qsrc}" ]; then
  echo
  echo "qBittorrent's /data source on the host: ${qsrc}"
  echo "first entries there:"
  ls -1 "${qsrc}" 2>/dev/null | head -10 || echo "  (cannot list it / empty)"
fi
