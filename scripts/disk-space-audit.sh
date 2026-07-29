#!/usr/bin/env bash
#
# Audit WHY the download drive is full and what unraid-cache-cleaner is (or is
# not) reclaiming.
#
# Answers, in order:
#   1. How full is the pool, and what else lives on it besides downloads?
#   2. What is the cleaner actually configured to do (DRY_RUN? grace? globs?)
#   3. What did its last run report?
#   4. What is in its state DB, and why are candidates not eligible yet?
#   5. How much of the download dir is live torrent data vs. orphaned junk?
#   6. Are download files hardlinked into the media library, or full copies?
#
# READ-ONLY. Nothing is deleted, no config is changed.
#
# Usage (on the Unraid server):
#   bash disk-space-audit.sh
#
# Override the container name if yours differs:
#   CONTAINER=<name> bash disk-space-audit.sh

set -uo pipefail

CONTAINER="${CONTAINER:-unraid-cache-cleaner}"

echo "=============================================="
echo " unraid-cache-cleaner :: disk space audit"
echo " container: ${CONTAINER}"
echo " date:      $(date)"
echo "=============================================="
echo

if ! docker inspect "${CONTAINER}" >/dev/null 2>&1; then
  echo "ERROR: container '${CONTAINER}' not found."
  echo "Run 'docker ps --format \"{{.Names}}\"' to find it, then re-run as:"
  echo "  CONTAINER=<name> bash ${0##*/}"
  exit 1
fi

# ---------------------------------------------------------------------------
# Resolve the host path behind the container's /data mount.
# ---------------------------------------------------------------------------
DL_HOST="$(docker inspect -f \
  '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' \
  "${CONTAINER}")"
CFG_HOST="$(docker inspect -f \
  '{{range .Mounts}}{{if eq .Destination "/config"}}{{.Source}}{{end}}{{end}}' \
  "${CONTAINER}")"

echo "### 1. MOUNTS AND FREE SPACE"
echo
echo "cleaner /data   -> ${DL_HOST:-<NOT MOUNTED>}"
echo "cleaner /config -> ${CFG_HOST:-<NOT MOUNTED>}"
echo
echo "-- all mounts on ${CONTAINER} --"
docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{if .RW}}rw{{else}}ro{{end}}){{"\n"}}{{end}}' "${CONTAINER}"
echo
echo "-- qBittorrent's mounts (must match /data byte-for-byte) --"
for qb in binhex-qbittorrent qbittorrent qBittorrent; do
  if docker inspect "${qb}" >/dev/null 2>&1; then
    echo "container: ${qb}"
    docker inspect -f '{{range .Mounts}}{{.Source}} -> {{.Destination}} ({{if .RW}}rw{{else}}ro{{end}}){{"\n"}}{{end}}' "${qb}"
    break
  fi
done
echo
echo "-- df --"
df -h | head -1
df -h | grep -E '/mnt/(cache|disk|user)' || true
echo
if [ -n "${DL_HOST}" ]; then
  echo "-- df for the download path specifically --"
  df -h "${DL_HOST}"
  echo
  POOL_ROOT="$(dirname "${DL_HOST}")"
  echo "-- what else is on ${POOL_ROOT} (top-level, may take a minute) --"
  du -h --max-depth=1 --one-file-system "${POOL_ROOT}" 2>/dev/null | sort -h | tail -25
  echo
fi

# ---------------------------------------------------------------------------
echo "### 2. CLEANER CONFIGURATION (secrets redacted)"
echo
docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "${CONTAINER}" \
  | grep -vE '^(PATH|LANG|GPG_KEY|PYTHON_|HOME|HOSTNAME)' \
  | sed -E 's/^(.*(PASS|TOKEN|SECRET|KEY|APIKEY|API_KEY)[^=]*)=.+$/\1=<redacted>/I' \
  | sort
echo
echo "-- restart policy / uptime (frequent restarts reset the grace window) --"
docker inspect -f 'restarts={{.RestartCount}} started={{.State.StartedAt}} status={{.State.Status}}' "${CONTAINER}"
echo

# ---------------------------------------------------------------------------
echo "### 3-6. INSIDE-CONTAINER ANALYSIS"
echo
docker exec -i "${CONTAINER}" python3 - <<'PY'
import json, os, sqlite3, ssl, stat, sys, time, urllib.parse, urllib.request
from collections import defaultdict
from http.cookiejar import CookieJar
from pathlib import Path

def human(n):
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000.0:
            return f"{n:.2f} {unit}"
        n /= 1000.0
    return f"{n:.2f} PB"

report_path = os.environ.get("REPORT_PATH", "/config/last-run.json")
state_path = os.environ.get("STATE_DB_PATH", "/config/state.sqlite3")
watch = [w.strip() for w in os.environ.get("WATCH_PATHS", "/data").split(",") if w.strip()]
grace = int(os.environ.get("ORPHAN_GRACE_SECONDS", "21600"))
min_age = int(os.environ.get("MIN_FILE_AGE_SECONDS", "0") or 0)
now = time.time()

# --- 3. last run report ----------------------------------------------------
print("### 3. LAST RUN REPORT")
print()
try:
    with open(report_path) as fh:
        rep = json.load(fh)
    gen = rep.get("generated_at") or rep.get("started_at")
    if isinstance(gen, (int, float)):
        age_h = (now - gen) / 3600.0
        print(f"generated_at: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(gen))} "
              f"({age_h:.1f} hours ago)")
    for k in ("dry_run", "torrent_count", "scanned_file_count", "candidate_count",
              "eligible_count", "action_count", "deleted_count", "deleted_bytes",
              "protected_dir_count", "errors"):
        if k in rep:
            v = rep[k]
            if k.endswith("bytes") and isinstance(v, (int, float)):
                v = f"{v} ({human(v)})"
            print(f"  {k:22} {v}")
    extra = {k: v for k, v in rep.items()
             if k not in ("actions", "candidates", "files", "groups") and k not in (
                 "generated_at", "started_at", "dry_run", "torrent_count",
                 "scanned_file_count", "candidate_count", "eligible_count",
                 "action_count", "deleted_count", "deleted_bytes",
                 "protected_dir_count", "errors")}
    if extra:
        print("  other keys:", ", ".join(sorted(extra)))
except FileNotFoundError:
    print(f"!! no report at {report_path} -- the service may never have completed a run")
except Exception as exc:
    print(f"!! could not read {report_path}: {exc}")
print()

# --- 4. state DB -----------------------------------------------------------
print("### 4. STATE DB (why candidates are / are not eligible)")
print()
print(f"grace window: {grace}s ({grace/3600:.1f}h)   min_file_age: {min_age}s")
try:
    conn = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    n, total = conn.execute(
        "SELECT COUNT(*), COALESCE(SUM(size),0) FROM candidates").fetchone()
    print(f"candidates tracked: {n}  totalling {human(total)}")
    if n:
        elig = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM candidates "
            "WHERE (?-first_seen) >= ? AND (?-mtime) >= ?",
            (now, grace, now, min_age)).fetchone()
        print(f"eligible right now: {elig[0]}  totalling {human(elig[1])}")
        oldest = conn.execute(
            "SELECT path, first_seen, size FROM candidates "
            "ORDER BY first_seen ASC LIMIT 5").fetchall()
        print("\n  oldest tracked candidates:")
        for r in oldest:
            print(f"    {(now-r['first_seen'])/3600:8.1f}h  {human(r['size']):>10}  {r['path']}")
        big = conn.execute(
            "SELECT path, first_seen, size FROM candidates "
            "ORDER BY size DESC LIMIT 10").fetchall()
        print("\n  largest tracked candidates:")
        for r in big:
            print(f"    {(now-r['first_seen'])/3600:8.1f}h  {human(r['size']):>10}  {r['path']}")
    try:
        acts = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(size),0) FROM actions").fetchone()
        print(f"\nactions recorded (lifetime deletions): {acts[0]} totalling {human(acts[1])}")
        recent = conn.execute(
            "SELECT * FROM actions ORDER BY rowid DESC LIMIT 5").fetchall()
        for r in recent:
            print("   ", dict(r))
    except sqlite3.Error:
        pass
    conn.close()
except Exception as exc:
    print(f"!! could not open {state_path}: {exc}")
    print("   (if this file is missing, /config is not persistent and the grace")
    print("    window resets on every restart -- nothing would ever age in)")
print()

# --- 5. live torrents vs. what is on disk ----------------------------------
print("### 5. DISK CONTENTS vs. LIVE TORRENTS")
print()
url = os.environ.get("QBITTORRENT_URL", "").rstrip("/")
user = os.environ.get("QBITTORRENT_USERNAME", "")
pw = os.environ.get("QBITTORRENT_PASSWORD", "")
verify = os.environ.get("QBITTORRENT_VERIFY_TLS", "true").strip().lower()

live = None
if not url:
    print("!! QBITTORRENT_URL not set inside the container")
else:
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
                f"{url}/api/v2/auth/login", data=data,
                headers={"Referer": url}), timeout=20).read()
        raw = opener.open(f"{url}/api/v2/torrents/info", timeout=60).read()
        torrents = json.loads(raw)
        print(f"qBittorrent reachable from container: {len(torrents)} torrents")
        live = set()
        for t in torrents:
            cp = t.get("content_path") or ""
            if cp:
                live.add(Path(cp).resolve())
        print(f"live content paths: {len(live)}")
    except Exception as exc:
        print(f"!! qBittorrent query failed from inside the container: {exc}")
        print("   (this alone would make the cleaner refuse to delete anything)")

for root in watch:
    rp = Path(root)
    print()
    print(f"-- walking {rp} --")
    if not rp.exists():
        print(f"   !! {rp} does not exist inside the container -- BAD MOUNT")
        continue
    entries = []
    seen_inodes = set()
    total_apparent = 0
    total_unique = 0
    linked_bytes = 0
    linked_files = 0
    try:
        top = sorted(p for p in rp.iterdir())
    except Exception as exc:
        print(f"   !! cannot list: {exc}")
        continue
    for child in top:
        sz = 0
        try:
            cst = os.lstat(child)
            if stat.S_ISREG(cst.st_mode):
                st = cst
                sz = st.st_size
                total_apparent += sz
                if st.st_ino not in seen_inodes:
                    seen_inodes.add(st.st_ino); total_unique += sz
                if st.st_nlink > 1:
                    linked_files += 1; linked_bytes += sz
            else:
                for dirpath, _dirs, files in os.walk(child, followlinks=False):
                    for f in files:
                        fp = os.path.join(dirpath, f)
                        try:
                            st = os.lstat(fp)
                        except OSError:
                            continue
                        sz += st.st_size
                        total_apparent += st.st_size
                        if st.st_ino not in seen_inodes:
                            seen_inodes.add(st.st_ino); total_unique += st.st_size
                        if st.st_nlink > 1:
                            linked_files += 1; linked_bytes += st.st_size
        except Exception:
            pass
        entries.append((sz, child))

    print(f"   top-level entries: {len(entries)}")
    print(f"   apparent total:    {human(total_apparent)}")
    print(f"   unique-inode total:{human(total_unique)}  <- actual disk consumed")
    print(f"   hardlinked files:  {linked_files} ({human(linked_bytes)})")
    if linked_files == 0:
        print("   NOTE: zero hardlinks => *arr is COPYING to the library, not linking.")
        print("         Every release exists twice (download + library copy).")

    if live is not None:
        orphan_sz = 0; orphan_n = 0; keep_sz = 0; keep_n = 0
        orphans = []
        for sz, child in entries:
            rc = child.resolve()
            is_live = rc in live or any(
                str(rc).startswith(str(lp) + "/") or str(lp).startswith(str(rc) + "/")
                for lp in live)
            if is_live:
                keep_sz += sz; keep_n += 1
            else:
                orphan_sz += sz; orphan_n += 1
                orphans.append((sz, child))
        print()
        print(f"   MATCHED to a live torrent: {keep_n} entries, {human(keep_sz)}")
        print(f"   NOT in qBittorrent (orphans): {orphan_n} entries, {human(orphan_sz)}")
        if orphans:
            print("\n   largest orphans (candidates for reclaim):")
            for sz, child in sorted(orphans, reverse=True)[:25]:
                try:
                    age = (now - child.stat().st_mtime) / 86400.0
                except OSError:
                    age = float("nan")
                print(f"     {human(sz):>10}  {age:6.1f}d old  {child.name}")
    else:
        print("\n   largest top-level entries:")
        for sz, child in sorted(entries, reverse=True)[:25]:
            print(f"     {human(sz):>10}  {child.name}")
PY

echo
echo "### 7. RECENT CLEANER LOGS"
echo
docker logs --tail 80 "${CONTAINER}" 2>&1 | sed -E 's/(password|token|apikey|api_key)=[^ ]+/\1=<redacted>/Ig'

echo
echo "=============================================="
echo " audit complete -- nothing was modified"
echo "=============================================="
