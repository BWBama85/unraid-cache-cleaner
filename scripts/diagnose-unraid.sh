#!/usr/bin/env bash
#
# Diagnose whether unraid-cache-cleaner is about to delete files that still
# belong to live qBittorrent torrents.
#
# The qBittorrent URL is usually a Docker DNS name (e.g. http://qbittorrent:8080)
# that only resolves inside the cleaner container's network, so this script runs
# the check INSIDE the cleaner container. That also means it sees exactly what
# the cleaner sees: same env, same /data mount, same /config/last-run.json.
#
# Nothing is deleted. This is read-only.
#
# Usage (on the Unraid server):
#   bash diagnose-unraid.sh
#
# Override the container name if yours differs:
#   CONTAINER=<name> bash diagnose-unraid.sh

set -euo pipefail

CONTAINER="${CONTAINER:-unraid-cache-cleaner}"

echo "== unraid-cache-cleaner diagnosis =="
echo "container: ${CONTAINER}"
echo

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
  echo "ERROR: container '${CONTAINER}' not found."
  echo "Run 'docker ps' to find the name, then re-run as:"
  echo "  CONTAINER=<name> bash ${0##*/}"
  exit 1
fi

# Everything below runs inside the cleaner container via 'docker exec':
# it already has the qBittorrent credentials, network access, and the report.
docker exec -i "${CONTAINER}" python3 - <<'PY'
import json
import os
import ssl
import urllib.parse
import urllib.request
from http.cookiejar import CookieJar
from pathlib import PurePosixPath

url = os.environ.get("QBITTORRENT_URL", "").rstrip("/")
user = os.environ.get("QBITTORRENT_USERNAME", "")
pw = os.environ.get("QBITTORRENT_PASSWORD", "")
verify = os.environ.get("QBITTORRENT_VERIFY_TLS", "true").strip().lower()
report_path = os.environ.get("REPORT_PATH", "/config/last-run.json")

if not url:
    raise SystemExit("ERROR: QBITTORRENT_URL is not set inside the container.")

print(f"qBittorrent URL (from inside container): {url}")
print(f"report:                                  {report_path}")
print()

# --- talk to qBittorrent ---------------------------------------------------
ctx = ssl.create_default_context()
if verify in ("0", "false", "no", "off"):
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE


class RedirectRefused(Exception):
    """A redirect off the configured host was refused before it was followed."""


class HostBoundRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Fail-closed redirect guard mirroring
    unraid_cache_cleaner.http_redirect.HostBoundRedirectHandler.

    Inlined rather than imported: this diagnostic is a standalone snippet piped to
    'python3 -' inside the container (it deliberately reimplements the qBittorrent
    calls instead of importing the package), so it must not depend on the package
    being importable on that interpreter's path. urllib re-applies the opener's
    addheaders — here the 'Referer' base URL — and the session cookie as
    unredirected headers when it follows a 3xx, so a qBittorrent endpoint or an
    interposing reverse proxy that 301/302s to a different host (or downgrades
    https -> http) would otherwise leak the internal base URL and cookie to the
    redirect target. A same-host redirect — including a port change or an
    http -> https upgrade — is still followed.
    """

    def __init__(self, allowed_host, require_tls):
        super().__init__()
        self._allowed_host = (allowed_host or "").lower()
        self._require_tls = require_tls

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        target_host = (target.hostname or "").lower()
        if target_host != self._allowed_host or (self._require_tls and target.scheme != "https"):
            raise RedirectRefused(
                "refusing to follow a cross-host or TLS-downgrading redirect to "
                f"'{target.scheme}://{target.hostname or 'unknown'}' so credentials stay on-box"
            )
        return super().redirect_request(req, fp, code, msg, headers, newurl)


parsed = urllib.parse.urlparse(url)
opener = urllib.request.build_opener(
    urllib.request.HTTPCookieProcessor(CookieJar()),
    HostBoundRedirectHandler(parsed.hostname, parsed.scheme == "https"),
    urllib.request.HTTPSHandler(context=ctx),
)
opener.addheaders = [("Referer", url), ("User-Agent", "uccc-diagnose/1.0")]


def qb_open(*args, **kwargs):
    """opener.open() that turns a refused cross-host redirect into a clean exit."""
    try:
        return opener.open(*args, **kwargs)
    except RedirectRefused as exc:
        raise SystemExit(f"ERROR: qBittorrent {exc}")


login = qb_open(
    url + "/api/v2/auth/login",
    data=urllib.parse.urlencode({"username": user, "password": pw}).encode(),
    timeout=20,
).read().decode().strip()
if login != "Ok.":
    raise SystemExit(f"ERROR: qBittorrent login failed (response: {login!r})")

torrents = json.load(qb_open(url + "/api/v2/torrents/info?filter=all", timeout=30))

# --- load what the cleaner wants to delete ---------------------------------
report = json.load(open(report_path))
watch_roots = [r.rstrip("/") for r in report.get("watch_roots", [])] or ["/data"]
deletes = [a for a in report.get("actions", []) if a.get("action") == "delete"]


def release_under_roots(path):
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
def root_of(p):
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
