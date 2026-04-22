#!/usr/bin/env bash
set -euo pipefail

REPO_OWNER="${REPO_OWNER:-BWBama85}"
REPO_NAME="${REPO_NAME:-unraid-cache-cleaner}"
REPO_REF="${REPO_REF:-main}"
TEMPLATE_URL="${TEMPLATE_URL:-https://raw.githubusercontent.com/${REPO_OWNER}/${REPO_NAME}/${REPO_REF}/contrib/unraid-cache-cleaner.xml}"
TARGET_DIR="${TARGET_DIR:-/boot/config/plugins/dockerMan/templates-user}"
TARGET_FILE="${TARGET_FILE:-${TARGET_DIR}/unraid-cache-cleaner.xml}"

if [[ ! -d /boot/config ]]; then
  echo "This installer is intended to run on an Unraid server."
  echo "Expected to find /boot/config but it does not exist."
  exit 1
fi

if command -v curl >/dev/null 2>&1; then
  downloader=(curl -fsSL)
elif command -v wget >/dev/null 2>&1; then
  downloader=(wget -qO-)
else
  echo "Neither curl nor wget is available. Install one of them and retry."
  exit 1
fi

mkdir -p "${TARGET_DIR}"

tmp_file="$(mktemp)"
cleanup() {
  rm -f "${tmp_file}"
}
trap cleanup EXIT

"${downloader[@]}" "${TEMPLATE_URL}" > "${tmp_file}"
install -m 0644 "${tmp_file}" "${TARGET_FILE}"

cat <<EOF
Installed template:
  ${TARGET_FILE}

Next steps in Unraid:
  1. Open the Docker tab.
  2. Click Add Container.
  3. Select the 'unraid-cache-cleaner' template.
  4. Set your qBittorrent URL, username, password, and download path mount.
  5. Leave DRY_RUN=true for the first start.

Published image:
  ghcr.io/bwbama85/unraid-cache-cleaner:latest
EOF
