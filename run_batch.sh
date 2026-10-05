#!/usr/bin/env bash
# Run ONE batch of the Golden Dome roster, then report where the next batch
# should start. Designed for GitHub Actions: non-interactive, secret-driven,
# and crash-safe (a partial batch never loses work already committed to the
# sheet, because sc1.py appends per accepted source).
#
# Inputs (env):
#   START_ROW     first sheet row for this batch      (default 20)
#   BATCH_SIZE    how many members to process         (default 10)
#   SLEEP_SECONDS cool-down before this batch starts   (default 0)
#   GITHUB_OUTPUT when set, outputs are written to it
#
# Outputs written to $GITHUB_OUTPUT:
#   NEXT_ROW, NO_MORE_ROWS, BATCH_RESULT
set -euo pipefail

START_ROW="${START_ROW:-2}"
BATCH_SIZE="${BATCH_SIZE:-10}"
SLEEP_SECONDS="${SLEEP_SECONDS:-0}"

# github/ sits directly beside sc1.py (EPFD/fernando/github/run_batch.sh).
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT_DIR="$(cd "${HERE}/.." && pwd)"
SC1="${SCRIPT_DIR}/sc1.py"

RESULTS="${HERE}/results.json"
LOG="${HERE}/sc1_batch.log"

log() { printf '[batch] %s\n' "$*"; }
die() { printf '[batch] ERROR: %s\n' "$*" >&2; exit 1; }

emit() {
  local k="$1" v="$2"
  log "${k}=${v}"
  if [ -n "${GITHUB_OUTPUT:-}" ]; then
    printf '%s=%s\n' "${k}" "${v}" >> "${GITHUB_OUTPUT}"
  fi
}

[ -f "${SC1}" ] || die "sc1.py not found at ${SC1} (repo layout changed?)"

# ---------------------------------------------------------------------------
# Fail loudly on missing secrets.
#
# Without this, sc1.py silently falls back to the hard-coded defaults in its
# source. A wrong APPS_SCRIPT_URL is worse than a crash: sheet_existing_urls()
# returns an empty set on failure, so the run would "succeed" while writing
# nothing at all -- a green tick and zero data.
# ---------------------------------------------------------------------------
if [ -z "${APPS_SCRIPT_URL:-}" ]; then
  die "APPS_SCRIPT_URL secret is not set (Settings > Secrets and variables > Actions)."
fi
if [ -z "${GOVINFO_API_KEY:-}" ]; then
  die "GOVINFO_API_KEY secret is not set (Settings > Secrets and variables > Actions)."
fi
case "${APPS_SCRIPT_URL}" in
  *YOUR_DEPLOYMENT_ID*) die "APPS_SCRIPT_URL still contains the placeholder YOUR_DEPLOYMENT_ID." ;;
esac

# ---------------------------------------------------------------------------
# Roster size, so we know when to stop.
#
# This matters more than it looks. sc1.py --start-row treats an out-of-range
# row as "start from the top" (it warns and resets to row 2). Dispatching past
# the end would wrap around and reprocess the roster forever.
# ---------------------------------------------------------------------------
log "reading roster from the sheet..."
TOTAL="$(python3 - "${SC1}" <<'PY'
import importlib.util, sys
spec = importlib.util.spec_from_file_location("sc1", sys.argv[1])
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
print(len(mod.fetch_roster_names()))
PY
)"
[ -n "${TOTAL}" ] || die "could not read the roster from the sheet"
LAST_ROW=$((TOTAL + 1))
log "roster has ${TOTAL} member(s) on sheet rows 2..${LAST_ROW}"

if [ "${START_ROW}" -gt "${LAST_ROW}" ]; then
  log "start row ${START_ROW} is past the last row ${LAST_ROW} - roster complete."
  emit NO_MORE_ROWS "true"
  emit NEXT_ROW "${START_ROW}"
  emit BATCH_RESULT "nothing to do; roster complete through row ${LAST_ROW}"
  exit 0
fi

REMAINING=$((LAST_ROW - START_ROW + 1))
if [ "${BATCH_SIZE}" -gt "${REMAINING}" ]; then
  log "clamping batch size ${BATCH_SIZE} -> ${REMAINING} (end of roster)"
  BATCH_SIZE="${REMAINING}"
fi

if [ "${SLEEP_SECONDS}" -gt 0 ]; then
  log "cooling down ${SLEEP_SECONDS}s before starting (rate-limit relief)"
  sleep "${SLEEP_SECONDS}"
fi
# ---------------------------------------------------------------------------
# Run the batch. --json gives per-member sheet_row so the next start row comes
# from structured data rather than being parsed out of the log text.
# ---------------------------------------------------------------------------
log "running rows ${START_ROW}..$((START_ROW + BATCH_SIZE - 1)) (${BATCH_SIZE} member(s))"
set +e
python3 "${SC1}" \
  --start-row "${START_ROW}" \
  --limit-members "${BATCH_SIZE}" \
  --quiet \
  --json "${RESULTS}" 2>&1 | tee "${LOG}"
SC1_STATUS=${PIPESTATUS[0]}
set -e

# sc1.py exits non-zero on setup failures (no model, no sheet). Treat that as
# fatal so the chain does NOT dispatch onward and skip the untouched members.
if [ "${SC1_STATUS}" -ne 0 ]; then
  die "sc1.py exited ${SC1_STATUS}; not dispatching a follow-up batch so this range can be retried"
fi

if [ ! -f "${RESULTS}" ]; then
  die "no results.json produced; cannot determine the next row"
fi

read -r NEXT_ROW FINISHED < <(python3 - "${RESULTS}" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1]))
except (OSError, ValueError):
    # Keep both fields on ONE line: `read` consumes a single line, so a
    # two-line print would leave FINISHED empty.
    print("0 0")
    raise SystemExit(0)
rows = [m.get("sheet_row") for m in (data.get("members") or [])
        if isinstance(m.get("sheet_row"), int)]
print(f"{max(rows) + 1} {len(rows)}" if rows else "0 0")
PY
)

if [ -z "${NEXT_ROW}" ] || [ "${NEXT_ROW}" -eq 0 ]; then
  die "could not read any completed sheet_row from results.json; not dispatching onward"
fi

if [ "${NEXT_ROW}" -gt "${LAST_ROW}" ]; then
  emit NO_MORE_ROWS "true"
  emit BATCH_RESULT "finished ${FINISHED} member(s) through row $((NEXT_ROW - 1)); roster COMPLETE"
  log "roster complete through row ${LAST_ROW}"
else
  emit NO_MORE_ROWS "false"
  emit BATCH_RESULT "finished ${FINISHED} member(s), rows ${START_ROW}..$((NEXT_ROW - 1)); next batch starts at row ${NEXT_ROW}"
fi
emit NEXT_ROW "${NEXT_ROW}"

log "done: ${FINISHED} member(s) completed; next start row = ${NEXT_ROW}"