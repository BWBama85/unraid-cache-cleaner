#!/usr/bin/env bash
#
# Force a fresh dry-run of unraid-cache-cleaner against the CURRENT qBittorrent
# torrent list, show what it would delete, and check hardlink counts so you can
# see whether deleting each orphan is safe.
#
# It refuses to run unless DRY_RUN is true in the container. Nothing is deleted.
#
# Usage (on the Unraid server):
#   bash fresh-check.sh
#
# Override the container name if yours differs:
#   CONTAINER=<name> bash fresh-check.sh

CONTAINER="${CONTAINER:-unraid-cache-cleaner}"

echo "== fresh dry-run check =="
echo "container: ${CONTAINER}"
echo

state="$(docker inspect -f '{{.State.Running}}' "${CONTAINER}" 2>/dev/null || echo missing)"
if [ "${state}" != "true" ]; then
  echo "ERROR: container '${CONTAINER}' is not running (state: ${state})."
  echo "Start it on the Docker tab, then re-run this script."
  echo "If the name differs:  CONTAINER=<name> bash ${0##*/}"
  exit 1
fi

# --- safety guard: only proceed in dry-run mode ----------------------------
dr="$(docker exec "${CONTAINER}" printenv DRY_RUN 2>/dev/null || echo "")"
drl="$(printf '%s' "${dr}" | tr '[:upper:]' '[:lower:]')"
case "${drl}" in
  ""|true|1|yes|on)
    echo "DRY_RUN is effectively TRUE (value: '${dr:-unset}') — safe, no deletions."
    ;;
  *)
    echo "REFUSING TO RUN: DRY_RUN='${dr}' means deletions are enabled."
    echo "Set DRY_RUN=true on the container first, then re-run this check."
    exit 1
    ;;
esac

report="$(docker exec "${CONTAINER}" printenv REPORT_PATH 2>/dev/null || echo /config/last-run.json)"
[ -z "${report}" ] && report=/config/last-run.json

echo
echo "== running a fresh scan (dry-run) =="
docker exec "${CONTAINER}" unraid-cache-cleaner scan 2>&1 | tail -8 || \
  echo "(scan command returned an error; see above)"

echo
echo "== fresh report: ${report} =="
if ! docker exec "${CONTAINER}" cat "${report}" > /tmp/uccc-report.json 2>/dev/null; then
  echo "ERROR: could not read ${report} from the container."
  exit 1
fi

python3 - <<'PY'
import json
from collections import Counter

r = json.load(open("/tmp/uccc-report.json"))
print(f"dry_run:         {r.get('dry_run')}")
print(f"torrent_count:   {r.get('torrent_count')}")
print(f"scanned_files:   {r.get('scanned_file_count')}")
print(f"orphan_cands:    {r.get('orphan_candidate_count')}")
print(f"eligible(delete):{r.get('eligible_count')}")
print(f"watch_roots:     {r.get('watch_roots')}")

roots = [x.rstrip("/") for x in r.get("watch_roots", [])] or ["/data"]
dels = [a for a in r.get("actions", []) if a.get("action") == "delete"]


def rel(p):
    for root in roots:
        if p.startswith(root + "/"):
            return p[len(root) + 1:].split("/", 1)[0]
    return None


folders = Counter()
total = 0
for a in dels:
    f = rel(a["path"])
    if f:
        folders[f] += 1
    total += a.get("size") or 0

print(f"\nwould delete: {len(dels)} files across {len(folders)} folders, "
      f"{total / 1e9:.2f} GB")
print("top folders:")
for f, n in folders.most_common(25):
    print(f"  {n:4d}  /data/{f}")

with open("/tmp/uccc-sample.txt", "w") as fh:
    fh.write("\n".join(a["path"] for a in dels[:10]))
PY

echo
echo "== hardlink check on a sample =="
echo "(links column: >1 means the same data is also linked elsewhere, e.g. your"
echo " media library, so deleting this copy is safe. 1 means this is the only copy.)"
echo
while IFS= read -r f; do
  [ -z "${f}" ] && continue
  docker exec "${CONTAINER}" stat -c '  %h links   %s bytes   %n' "${f}" 2>/dev/null || \
    echo "  (missing)  ${f}"
done < /tmp/uccc-sample.txt
