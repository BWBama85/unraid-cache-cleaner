#!/usr/bin/env bash
#
# Remove empty directories under the downloads path whose last-modified date
# falls in calendar year 2025.
#
# "Empty" means the directory's ENTIRE SUBTREE contains no regular files --
# a folder holding only other empty folders counts as empty and is pruned.
#
# SAFETY: every candidate is cross-checked against qBittorrent's live torrent
# list first. A queued-but-not-yet-started torrent legitimately owns an EMPTY
# folder, so a naive `find -type d -empty -delete` would destroy it. Anything
# that is, contains, or sits inside a live torrent path is refused.
#
# REPORT-ONLY BY DEFAULT. Nothing is removed unless you pass --delete.
#
# Usage (on the Unraid server):
#   bash prune-empty-2025-dirs.sh              # report what would go
#   bash prune-empty-2025-dirs.sh --delete     # actually remove them
#
# Override the container name if yours differs:
#   CONTAINER=<name> bash prune-empty-2025-dirs.sh

set -uo pipefail
CONTAINER="${CONTAINER:-unraid-cache-cleaner}"
MODE="report"
[ "${1:-}" = "--delete" ] && MODE="delete"

echo "== prune empty 2025 directories =="
echo "container: ${CONTAINER}   mode: ${MODE}   date: $(date)"
echo

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
  echo "ERROR: container '${CONTAINER}' not found."
  exit 1
fi

docker exec -i -e PRUNE_MODE="${MODE}" "${CONTAINER}" python3 - <<'PY'
import errno, json, os, ssl, sys, time, urllib.parse, urllib.request
from http.cookiejar import CookieJar

mode = os.environ.get("PRUNE_MODE", "report")
roots = [r.strip() for r in os.environ.get("WATCH_PATHS", "/data").split(",") if r.strip()]
YEAR = 2025

# ---- live torrent paths (the safety guard) --------------------------------
url = os.environ.get("QBITTORRENT_URL", "").rstrip("/")
user = os.environ.get("QBITTORRENT_USERNAME", "")
pw = os.environ.get("QBITTORRENT_PASSWORD", "")
verify = os.environ.get("QBITTORRENT_VERIFY_TLS", "true").strip().lower()

if not url:
    sys.exit("ABORT: QBITTORRENT_URL unset; refusing to delete without the live-torrent guard.")
try:
    ctx = ssl.create_default_context()
    if verify in ("0", "false", "no", "off"):
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ctx),
        urllib.request.HTTPCookieProcessor(CookieJar()))
    if user or pw:
        data = urllib.parse.urlencode({"username": user, "password": pw}).encode()
        opener.open(urllib.request.Request(
            f"{url}/api/v2/auth/login", data=data, headers={"Referer": url}), timeout=20).read()
    torrents = json.loads(opener.open(f"{url}/api/v2/torrents/info", timeout=60).read())
except Exception as exc:
    sys.exit(f"ABORT: could not reach qBittorrent ({exc}); refusing to delete blind.")

# NOTE: guard on content_path ONLY. save_path is the shared root (/data) for
# every torrent, so including it would match every directory in the tree and
# refuse the entire candidate set.
live = set()
for t in torrents:
    v = t.get("content_path")
    if v:
        live.add(os.path.normpath(v))
if not live:
    sys.exit("ABORT: qBittorrent returned no content paths; refusing to delete without a usable guard.")
print(f"live torrents: {len(torrents)}  ({len(live)} distinct content paths) -- guard active")
print()

def touches_live(p):
    p = os.path.normpath(p)
    for lp in live:
        if p == lp or p.startswith(lp + os.sep) or lp.startswith(p + os.sep):
            return lp
    return None

# ---- find directories whose whole subtree holds no regular files ----------
candidates = []   # (depth, path, mtime)
skipped_live = []
for root in roots:
    rp = os.path.normpath(root)
    if not os.path.isdir(rp):
        print(f"!! {rp} is not a directory inside the container -- skipping")
        continue
    has_file = {}
    for dirpath, dirnames, filenames in os.walk(rp, topdown=False, followlinks=False):
        # os.walk puts EVERY non-directory entry in filenames -- regular files,
        # symlinks, sockets. Any of them means this subtree is not empty.
        own = bool(filenames)
        kids = any(has_file.get(os.path.join(dirpath, d), False) for d in dirnames)
        has_file[dirpath] = own or kids
        if dirpath == rp:
            continue
        if own or kids:
            continue
        try:
            st = os.lstat(dirpath)
        except OSError:
            continue
        if time.localtime(st.st_mtime).tm_year != YEAR:
            continue
        hit = touches_live(dirpath)
        if hit:
            skipped_live.append((dirpath, hit))
            continue
        candidates.append((dirpath.count(os.sep), dirpath, st.st_mtime))

candidates.sort(key=lambda x: (-x[0], x[1]))

print(f"=== EMPTY DIRECTORIES WITH {YEAR} MTIME: {len(candidates)} ===")
print()
for _d, p, mt in sorted(candidates, key=lambda x: x[1]):
    print(f"  {time.strftime('%Y-%m-%d %H:%M', time.localtime(mt))}  {p}")

if skipped_live:
    print()
    print(f"=== REFUSED (belong to live torrents): {len(skipped_live)} ===")
    for p, hit in skipped_live:
        print(f"  {p}\n      overlaps live: {hit}")

print()
if mode != "delete":
    print("REPORT ONLY -- nothing removed. Re-run with --delete to remove the above.")
    raise SystemExit(0)

removed = failed = 0
for _depth, p, _mt in candidates:      # deepest first so parents empty out
    try:
        os.rmdir(p)
        removed += 1
    except OSError as exc:
        failed += 1
        note = ""
        if exc.errno == errno.ENOTEMPTY:
            note = ("  (still holds a subdirectory whose own mtime is not "
                    f"{YEAR}, so it was never a candidate -- left alone on purpose)")
        print(f"  FAILED {p}: {exc.strerror}{note}")

print(f"=== RESULT: removed {removed}, failed {failed} ===")
leftover = [p for _d, p, _m in candidates if os.path.isdir(p)]
print(f"verification: {len(leftover)} of {len(candidates)} still present"
      + (" -- all gone" if not leftover else ""))
PY

echo
echo "== done =="
