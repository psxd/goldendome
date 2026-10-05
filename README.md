# Golden Dome roster on GitHub Actions

Runs `sc1.py` over the sheet roster **10 members at a time**, one batch per
workflow run, chaining itself until the roster is finished.

```
run 1: rows 20..29  → dispatches run 2
run 2: rows 30..39  → dispatches run 3
...
last: rows 530..534 → roster COMPLETE, no dispatch
```

Each batch is a **separate job**, which is the point: one job covering all 533
members would hit GitHub's 6-hour cap, but sequential runs each get a fresh
clock. Batches never overlap — the dispatch is the last step of the job, and a
`concurrency` group serialises any that ever race.

## One-time setup

**1. Push this folder to a GitHub repo.** This directory is currently **not** a
git repository, so nothing will run until it is:

```bash
cd "/Users/pradosh/Documents/Python/SAF Dashboard"
git init
git add .
git commit -m "Golden Dome batch runner"
gh repo create golden-dome --private --source=. --push
```

**2. Install the workflow.** GitHub only reads workflows from `.github/workflows/`
at the repo root:

```bash
mkdir -p .github/workflows
cp "EPFD/fernando/github/sc1-batch.yml" .github/workflows/
git add .github/workflows && git commit -m "Add batch workflow" && git push
```

**3. Add the secrets** — repo → Settings → Secrets and variables → Actions →
*New repository secret*:

| Secret | Required | What it is |
|---|---|---|
| `APPS_SCRIPT_URL` | yes | Deployed Apps Script `/exec` URL for the sheet |
| `GOVINFO_API_KEY` | yes | Free api.data.gov key (Congressional Record search) |
| `OLLAMA_MODEL` | no | Repository **variable**, not secret. Default `gemma3:1b` |

`run_batch.sh` **refuses to start** if either required secret is missing or still
contains the `YOUR_DEPLOYMENT_ID` placeholder. This is deliberate: without the
guard, `sc1.py` silently falls back to its built-in defaults, and a wrong sheet
URL makes `sheet_existing_urls()` return nothing — a green run that wrote zero
data.

**4. Run it.** Actions tab → *Golden Dome roster (batch)* → *Run workflow*.
`start_row` defaults to **20**, so rows 2–19 are treated as already done.

## How resuming works

`run_batch.sh` reads the roster size from the sheet and computes the next start
row from `results.json` (`max(sheet_row) + 1`). It **never** dispatches past the
end: `sc1.py` treats an out-of-range `--start-row` as "start from row 2", so
without this guard the chain would wrap around and reprocess the roster forever.

If a batch dies, no follow-up is dispatched and that range is left for a manual
re-run. Re-dispatching the same `start_row` is safe: sources already on the
sheet are skipped, so nothing is duplicated.

## Notes on the data

**Append-only.** Each accepted source becomes a new 5-column block in the first
empty `H, M, R, …` block on that member's row. Existing cells are never
overwritten, so starting from row 20 cannot damage rows 2–19.

**Congressional Record attribution.** A Record granule is a day of floor
business covering 100+ members, so the member's surname appearing anywhere in the
document proves nothing — it may only be listed as a cosponsor in someone else's
amendment. `sc1.py` therefore segments the Record into speaker turns and only
credits text inside the member's **own** turn where the topic also appears
before the next speaker takes over.

Measured on `CREC-2026-06-23-pt1-PgS3064`: the previous logic would have credited
**37** senators to a single amendment; the current logic credits **1**.

⚠️ **Rows written before this fix may contain mis-attributed Congressional Record
entries.** The append-only design means the fix does not retroactively correct
them. If that matters, clear the affected columns for those members manually.

**Quota.** Ollama runs on the runner itself, so there is no external API key, no
rate limit and no per-request cost. The limits that do apply are DuckDuckGo's
(`DDG_MIN_INTERVAL = 5s`) and the Apps Script quota — use the `sleep_seconds`
input to add a cool-down between batches if you hit either.

## Tuning

| Input | Default | Notes |
|---|---|---|
| `start_row` | 20 | Row 1 is the header, so row 2 = first member |
| `batch_size` | 10 | Lower to 5 if batches approach the 330-minute cap |
| `sleep_seconds` | 0 | Cool-down before the batch starts |

A batch that gets cut off by `timeout-minutes: 330` loses nothing already
written to the sheet, but it will not auto-advance — re-dispatch the same
`start_row` to continue.

## Local dry run

```bash
cd EPFD/fernando
python3 sc1.py --start-row 20 --limit-members 3 --dry-run-sheet --official-only
```

`--dry-run-sheet` writes nothing, so you can confirm the attributions look right
before committing 500+ members to them.