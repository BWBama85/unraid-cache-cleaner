#!/usr/bin/env bash
#
# Diagnose whether unraid-cache-cleaner is about to delete files that still
# belong to live qBittorrent torrents.
#
# It runs on the Unraid host, reads the qBittorrent credentials out of the
# running cleaner container, queries the qBittorrent API directly, and compares
# what qBittorrent actually reports against the orphans flagged in last-run.json.
#
# Nothing is deleted. This is read-only.
#
# Usage (on the Unraid server):
#   bash diagnose-unraid.sh
#
# Override defaults with environment variables if needed:
#   CONTAINER   cleaner container name (default: unraid-cache-cleaner)
#   REPORT      path to last-run.json  (default: /mnt/user/appdata/<CONTAINER>/last-run.json)
#
# Only requirements: docker and python3 (both already present on Unraid).

set -euo pipefail

CONTAINER="${CONTAINER:-unraid-cache-cleaner}"
REPORT="${REPORT:-/mnt/user/appdata/${CONTAINER}/last-run.json}"

echo "== unraid-cache-cleaner diagnosis =="
echo "container: ${CONTAINER}"
echo "report:    ${REPORT}"
echo

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
  echo "ERROR: container '${CONTAINER}' not found."
  echo "Run 'docker ps' to find the name, then re-run as:"
  echo "  CONTAINER=<name> bash ${0##*/}"
  exit 1
fi

if [[ ! -f "${REPORT}" ]]; then
  echo "ERROR: report not found at ${REPORT}"
  echo "Re-run with the correct path:"
  echo "  REPORT=/path/to/last-run.json bash ${0##*/}"
  exit 1
fi

Q_URL="$(docker exec "${CONTAINER}" printenv QBITTORRENT_URL 2>/dev/null || true)"
Q_USER="$(docker exec "${CONTAINER}" printenv QBITTORRENT_USERNAME 2>/dev/null || true)"
Q_PASS="$(docker exec "${CONTAINER}" printenv QBITTORRENT_PASSWORD 2>/dev/null || true)"
Q_VERIFY="$(docker exec "${CONTAINER}" printenv QBITTORRENT_VERIFY_TLS 2>/dev/null || echo true)"

if [[ -z "${Q_URL}" ]]; then
  echo "ERROR: could not read QBITTORRENT_URL from the container environment."
  exit 1
fi

echo "qBittorrent URL: ${Q_URL}"
echo

export Q_URL Q_USER Q_PASS Q_VERIFY REPORT

python3 - <<'PY'
import json
import os
import ssl
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import PurePosixPath

url = os.environ["Q_URL"].rstrip("/")

# --- talk to qBittorrent ---------------------------------------------------
ctx = ssl.create_default_context()
if os.environ.get("Q_VERIFY", "true").strip().lower() in ("0", "false", "no", "off"):
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE

opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(CookieJar()),
    urllib.request.HTTPSHandler(context=ctx),
)
opener.addheaders = [("Referer", url), ("User-Agent", "uccc-diagnose/1.0")]

login = opener.open(
    url + "/api/v2/auth/login",
    data=urllib.parse.urlencode(
        {"username": os.environ["Q_USER"], "password": os.environ["Q_PASS"]}
    ).encode(),
    timeout=20,
).read().decode().strip()
if login != "Ok.":
    raise SystemExit(f"ERROR: qBittorrent login failed (response: {login!r})")

torrents = json.load(
    opener.open(url + "/api/v2/torrents/info?filter=all", timeout=30)
)

# --- load what the cleaner wants to delete ---------------------------------
report = json.load(open(os.environ["REPORT"]))
watch_roots = [r.rstrip("/") for r in report.get("watch_roots", [])] or ["/data"]
deletes = [a for a in report.get("actions", []) if a.get("action") == "delete"]


def release_under_roots(path: str):
    """First path segment beneath any watch root (e.g. /data/Foo/bar -> Foo)."""
    for root in watch_roots:
        if path == root:
            return None
        if path.startswith(root + "/"):
            return path[len(root) + 1:].split("/", 1)[0]
    return None


flagged = {}
for action in deletes:
    rel = release_under_roots(action["path"])
    if rel:
        flagged[rel] = flagged.get(rel, 0) + 1


# --- summarize what qBittorrent reports ------------------------------------
def root_of(p: str) -> str:
    if p.startswith("/") and "/" in p[1:]:
        return "/" + p.split("/")[1]
    return p or "(empty)"


root_hist = {}
torrent_names = set()
torrent_basenames = set()
ident_to_cp = {}
for t in torrents:
    cp = t.get("content_path") or ""
    name = t.get("name") or ""
    root_hist[root_of(cp)] = root_hist.get(root_of(cp), 0) + 1
    if name:
        torrent_names.add(name)
        ident_to_cp[name] = cp
    if cp:
        base = PurePosixPath(cp).name
        torrent_basenames.add(base)
        ident_to_cp.setdefault(base, cp)

print(f"torrents reported by qBittorrent API: {len(torrents)}")
print(f"watch roots (from last-run.json):     {watch_roots}")
print()
print("content_path root as qBittorrent reports it:")
for r, c in sorted(root_hist.items(), key=lambda kv: -kv[1]):
    flag = "   <-- matches watch root" if r in watch_roots else "   <-- NOT in watch roots"
    print(f"  {c:5d}  {r}{flag}")
print()

matches = sorted(r for r in flagged if r in torrent_names or r in torrent_basenames)

print(f"distinct folders/files the cleaner flagged: {len(flagged)}")
print(f"  ...that are STILL LIVE torrents in qBit:  {len(matches)}")
print()

if matches:
    print("!!! THE CLEANER IS ABOUT TO DELETE LIVE TORRENT CONTENT !!!")
    print()
    print("Examples (flagged on disk  <-  what qBittorrent reports):")
    for rel in matches[:12]:
        print(f"  /data/{rel}")
        print(f"      qB content_path: {ident_to_cp.get(rel, '?')}")
    print()
    print("VERDICT: BUG CONFIRMED.")
    print("qBittorrent reports these torrents under a path the cleaner can't")
    print("see, so it skips them and flags their files as orphans.")
    print("DO NOT set DRY_RUN=false. The matching logic needs the fix.")
else:
    print("VERDICT: none of the flagged items are current torrents.")
    print("They look like genuine leftovers removed from qBittorrent.")
    print("The matching is working; still eyeball the list before deleting.")
PY
