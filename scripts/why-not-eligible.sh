#!/usr/bin/env bash
#
# The cleaner tracks orphan candidates but reports eligible=0 forever.
# This proves WHICH eligibility predicate is failing, using the stored values.
#
# Eligibility (state.py get_eligible_candidates) requires BOTH:
#   (now - first_seen) >= ORPHAN_GRACE_SECONDS
#   (now - mtime)      >= MIN_FILE_AGE_SECONDS
#
# It also compares the DB's recorded mtime against the file's LIVE mtime, so a
# value that is being rewritten every scan shows up as drift.
#
# READ-ONLY. Nothing is deleted, no config is changed.
#
# Usage (on the Unraid server):
#   bash why-not-eligible.sh

set -uo pipefail
CONTAINER="${CONTAINER:-unraid-cache-cleaner}"

echo "== why are candidates never eligible? =="
echo "container: ${CONTAINER}   date: $(date)"
echo

docker exec -i "${CONTAINER}" python3 - <<'PY'
import json, os, sqlite3, time
from pathlib import Path

state_path = os.environ.get("STATE_DB_PATH", "/config/state.sqlite3")
report_path = os.environ.get("REPORT_PATH", "/config/last-run.json")
grace = int(os.environ.get("ORPHAN_GRACE_SECONDS", "21600"))
min_age = int(os.environ.get("MIN_FILE_AGE_SECONDS", "0") or 0)
now = time.time()

def human(n):
    n = float(n)
    for u in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1000.0:
            return f"{n:.2f} {u}"
        n /= 1000.0
    return f"{n:.2f} PB"

def ts(v):
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v))
    except (ValueError, OSError, OverflowError):
        return f"<uninterpretable: {v!r}>"

print(f"now = {now:.3f} ({ts(now)})")
print(f"ORPHAN_GRACE_SECONDS = {grace}   MIN_FILE_AGE_SECONDS = {min_age}")
print()

conn = sqlite3.connect(f"file:{state_path}?mode=ro", uri=True)
conn.row_factory = sqlite3.Row

print("=== PER-CANDIDATE PREDICATE EVALUATION ===")
print()
rows = conn.execute(
    "SELECT path, size, mtime, first_seen, last_seen FROM candidates "
    "ORDER BY size DESC").fetchall()
print(f"{len(rows)} candidates\n")

fail_grace = fail_age = both_ok = 0
for r in rows:
    fs, mt = r["first_seen"], r["mtime"]
    d_fs, d_mt = now - fs, now - mt
    ok_fs, ok_mt = d_fs >= grace, d_mt >= min_age
    if ok_fs and ok_mt:
        both_ok += 1
    if not ok_fs:
        fail_grace += 1
    if not ok_mt:
        fail_age += 1
    # live mtime for comparison
    try:
        live = os.lstat(r["path"]).st_mtime
        drift = f"live_mtime={live:.3f} ({ts(live)}) delta_vs_db={live - mt:+.1f}s"
    except OSError as exc:
        live = None
        drift = f"live stat failed: {exc.__class__.__name__} {exc.strerror}"
    verdict = "ELIGIBLE" if (ok_fs and ok_mt) else "blocked"
    print(f"[{verdict}] {human(r['size']):>10}  {Path(r['path']).name}")
    print(f"    first_seen = {fs!r} -> {ts(fs)}")
    print(f"      (now-first_seen) = {d_fs:,.1f}s  >= {grace}? {'YES' if ok_fs else 'NO'}")
    print(f"    db mtime   = {mt!r} -> {ts(mt)}")
    print(f"      (now-mtime)      = {d_mt:,.1f}s  >= {min_age}? {'YES' if ok_mt else 'NO'}")
    print(f"    {drift}")
    print()

print("=== SUMMARY ===")
print(f"  candidates:                 {len(rows)}")
print(f"  blocked by grace window:    {fail_grace}")
print(f"  blocked by min-file-age:    {fail_age}")
print(f"  passing BOTH (eligible):    {both_ok}")
print()

# Exactly the query the service runs.
q = conn.execute(
    "SELECT COUNT(*) FROM candidates WHERE (?-first_seen) >= ? AND (?-mtime) >= ?",
    (now, grace, now, min_age)).fetchone()[0]
print(f"  service's own SQL returns:  {q}")
print()

print("=== COLUMN TYPES ACTUALLY STORED (should all be 'real'/'integer') ===")
for r in conn.execute(
        "SELECT typeof(mtime) tm, typeof(first_seen) tf, typeof(size) tsz, "
        "COUNT(*) n FROM candidates GROUP BY tm, tf, tsz"):
    print(f"  mtime={r['tm']:>8}  first_seen={r['tf']:>8}  size={r['tsz']:>8}  rows={r['n']}")
print()

print("=== ACTIONS HISTORY (has it ever actually deleted?) ===")
for r in conn.execute(
        "SELECT action, status, COUNT(*) n, COALESCE(SUM(size),0) b, "
        "MAX(occurred_at) last FROM actions GROUP BY action, status "
        "ORDER BY n DESC"):
    print(f"  {r['action']:<12} {r['status']:<22} {r['n']:>7} rows  "
          f"{human(r['b']):>10}  last={ts(r['last'])}")
print()

print("=== LAST-RUN REPORT (full) ===")
try:
    with open(report_path) as fh:
        rep = json.load(fh)
    for k, v in sorted(rep.items()):
        if isinstance(v, list) and len(v) > 8:
            print(f"  {k}: <{len(v)} entries>")
        else:
            print(f"  {k}: {v}")
except Exception as exc:
    print(f"  !! {exc}")
conn.close()
PY

echo
echo "== done -- nothing was modified =="
