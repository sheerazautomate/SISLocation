# SISLocation

Two-phase extractor for Punjab SIS (`sis.pesrp.edu.pk`) school data:

1. **Phase 1 — IDs.** For each EMIS code, resolve district / tehsil / markaz / school ID.
2. **Phase 2 — Coordinates.** For each school ID, follow the Google Maps redirect and pull lat/lon.
3. **Merge.** Combine everything into `JSON`, `CSV` and `GeoJSON`.

## The endpoints

```
GET /dashboard/get_dtms_by_emis_code?s_id_emis_code=32230183
-> {"s_district_idFk":"18","s_tehsil_idFk":"70","s_markaz_idFk":"2605","s_id":"10669","s_emis_code":"32230183"}
-> `null` when the EMIS code is unknown

GET /transfer/show_google_map_school/10669
-> 302 Location: https://www.google.com/maps?saddr=30.84132237,71.2246089
```

## Run it on GitHub Actions (no local machine needed)

**Phase 1 is fully automated.** Commit your base file, then run the workflow.

0. **Install the workflow** (one time, from your machine). The Arena GitHub App
   isn't granted the `workflows` permission — both `git push` and the REST API
   return 403 for `.github/workflows/*` — so the file ships at
   `ci/fetch-dtms.yml.txt` and you install it with your own credentials:
   ```bash
   ./ci/install-workflow.sh
   ```
   It installs onto the **default branch** (`main`), because GitHub only shows the
   "Run workflow" button for workflows present there. It then returns you to your
   current branch. Use `--here` to install onto the current branch instead.

1. Commit the base file to the branch you'll run from, so the runner can read it:
   ```bash
   git add -f "data/Base Schools.json"
   git commit -m "data: add base schools file"
   git push
   ```
2. GitHub → **Actions** → **Phase 1 - Fetch DTMS IDs** → **Run workflow**.
3. Leave the defaults (10 shards × 6 workers) or tune them, then start it.

Or from the CLI:

```bash
gh workflow run fetch-dtms.yml -f limit=20    # smoke test first
gh workflow run fetch-dtms.yml                # full 38K run
gh run watch $(gh run list --workflow=fetch-dtms.yml -L1 --json databaseId -q '.[0].databaseId')
```

**Smoke-test first:** set `limit` to `20` — each shard does 20 codes, finishing in
under a minute, so you can confirm everything works before the full 38K run.

### What it does

`prep` validates the base file and builds the shard matrix → `fetch` runs N runners
in parallel, each on a **disjoint** slice → `combine` merges the shards, dedupes,
writes a summary table, uploads `school_ids.jsonl` as an artifact, and (optionally)
commits it back to the branch.

38K codes across 10 shards is ~3,800 lookups each — roughly 10–20 minutes per shard
rather than hours in a single job.

### Inputs

| Input | Default | Notes |
| --- | --- | --- |
| `base_file` | `data/Base Schools.json` | Must be committed to the repo |
| `shards` | `10` | Parallel runners, 1–20 (auto-clamped to the code count) |
| `workers` | `6` | Concurrency **per runner** — total load is `shards × workers` |
| `delay` | `0.15` | Random jitter per request |
| `emis_field` | *(blank)* | Auto-detected if blank |
| `limit` | `0` | Cap rows per shard — use for smoke tests |
| `retry_errors` | off | Re-attempt previously errored rows |
| `commit_results` | on | Commit `data/school_ids.jsonl` back to the branch |

> **Be careful with total load.** `shards × workers` is what the SIS server actually
> sees. The defaults mean ~60 concurrent requests. If you see errors climbing in the
> job logs, lower `workers` before raising `shards`.

### Resuming and retrying

Every shard caches its partial JSONL and results are committed, so **re-running the
workflow picks up where it left off** — already-fetched codes are skipped. If a shard
times out or a runner dies, just run it again. To repair errored rows, re-run with
`retry_errors` enabled.

Download the result from the run's **Artifacts** section (`school_ids`), or pull the
branch if `commit_results` was on.

## Phase 2 on GitHub Actions — coordinates and final exports

Once Phase 1 has produced `data/school_ids.jsonl`, Phase 2 requests
`/transfer/show_google_map_school/<school_id>` and extracts the coordinates from
its Maps redirect **without visiting Google**. Only successful, unique school IDs
are fetched; missing/error Phase-1 records remain visible in the merged exports.

### Install and smoke-test

The workflow is shipped as **`ci/fetch-coords.yml.txt`** because the Arena GitHub App
cannot push workflow files. On your own machine, with your usual GitHub credentials:

```bash
./ci/install-workflow.sh --phase2
```

Alternatively, use GitHub's file editor on `main`: create
`.github/workflows/fetch-coords.yml`, paste the template, and commit it. The workflow
must exist on the default branch for the **Run workflow** button to appear. The
selected run branch must also contain the workflow and the updated `sis/` code;
merge the Phase-2 implementation before running on `main`.

In **Actions → Phase 2 - Fetch School Coordinates → Run workflow**, select the
branch containing your Phase-1 output and start with **limit = 20**. With ten shards
this fetches up to 200 pending schools, not the entire dataset.

```bash
# After installation / merging the Phase-2 code:
gh workflow run fetch-coords.yml --ref main -f limit=20
# Inspect the summary and school_locations artifact; then run all remaining IDs:
gh workflow run fetch-coords.yml --ref main
```

### Inputs and results

| Input | Default | Meaning |
| --- | --- | --- |
| `ids_file` | `data/school_ids.jsonl` | Committed Phase-1 output |
| `base_file` | `data/Base Schools.json` | Base rows carried into final exports |
| `shards` | `10` | 1–20 shards; at most 10 execute concurrently |
| `workers` | `4` | 1–16 workers per runner |
| `delay` | `0.25` | Random request jitter in seconds |
| `limit` | `0` | Pending rows per shard; `20` for a smoke test |
| `emis_field` | *(blank)* | Auto-detect the EMIS column for merging |
| `retry_errors` | off | Retry failed HTTP/network/invalid-coordinate lookups |
| `retry_missing` | off | Retry schools without stored coordinates |
| `commit_results` | on | Commit only `data/school_coords.jsonl` to the run branch |

Defaults allow up to **40 concurrent requests**. Lower workers or shards if SIS
starts returning rate-limit/server errors. TLS verification stays enabled.

The **`school_locations` artifact** (90-day retention) contains:

- `school_coords.jsonl` — resumable coordinate checkpoint;
- `schools_final.json`, `schools_final.csv`, `schools_final.geojson` — merged base
  rows, IDs, coordinates, and final statuses. GeoJSON excludes missing, zero and
  globally invalid coordinates; valid locations outside Punjab are kept and flagged.

The summary reports processed/pending IDs, status counts, out-of-range locations,
and merged-school coverage. Smoke tests and failed shards intentionally produce
**partial exports** with `coords_pending` rows, not fabricated locations.

### Resume safely

Each shard seeds from **committed coordinates plus its cached partial results**.
Caches are isolated by branch, school-ID set and shard layout. Re-running skips
recorded IDs unless the relevant retry switch is enabled. The final combine includes
the committed baseline, so changing shard count or losing a shard cannot erase it.
Successful records beat missing/error records, and all run-attempt artifacts are
kept separately to prevent a shorter retry from overwriting earlier progress.

Fetch steps leave time for best-effort cache/artifact saves even on failure. A hard
runner loss/cancellation can still lose progress not yet uploaded or committed;
re-run to recover. Caches may be evicted. If `commit_results` is off, download the
checkpoint before artifact expiry and commit it (or use it locally) for durable resume.
Final exports remain artifacts rather than large generated files in Git.

## Quick start (local)

```bash
pip install -r requirements.txt

# put your base file at data/Base Schools.json, then:
python run.py all -b "data/Base Schools.json" -w 8
```

Or run the phases individually (recommended for 38K rows, so you can inspect between steps):

```bash
python run.py ids    -i "data/Base Schools.json" -o data/school_ids.jsonl    -w 8
python run.py coords -i data/school_ids.jsonl    -o data/school_coords.jsonl -w 8
python run.py merge  -b "data/Base Schools.json" -o data/schools_final
```

Always dry-run a small slice first:

```bash
python run.py ids -i "data/Base Schools.json" -o data/sample_ids.jsonl --limit 50 -w 4
```

## Input format

The base file may be `.json` (array of objects, array of codes, or `{"data": [...]}`),
`.jsonl`, `.csv`, or a `.txt` of one code per line. The EMIS column is auto-detected
(`emis`, `emis_code`, `s_emis_code`, `EMIS Code`, …); override with `--emis-field "EMIS Code"`.
Codes are de-duplicated and `32230183.0`-style spreadsheet artifacts are normalised.

## Resumability

Both phases append to JSONL and **skip work already recorded**, so a run interrupted at
row 20,000 picks up where it left off — just re-run the same command. A torn final line
from a hard kill is repaired before appending new results.

```bash
python run.py ids    -i "data/Base Schools.json" -o data/school_ids.jsonl --retry-errors
python run.py coords -i data/school_ids.jsonl -o data/school_coords.jsonl --retry-errors
python run.py coords -i data/school_ids.jsonl -o data/school_coords.jsonl --retry-missing
```

## Useful flags

| Flag | Meaning |
| --- | --- |
| `-w, --workers` | Concurrent requests (default 8). Be kind to a government server. |
| `--delay N` | Random 0–N s jitter before each request. |
| `--limit N` | Only process the first N pending rows. |
| `--timeout` / `--retries` | Per-request timeout and retry budget (backoff + jitter). |
| `--retry-errors` | Re-attempt rows previously recorded as errors. |
| `--retry-missing` | (coords) Re-attempt schools with no stored location. |
| `--insecure` | Skip TLS verification if the server's chain misbehaves. |

`SIS_BASE_URL` overrides the base host (used by the test harness).

## Output

`data/school_ids.jsonl`

```json
{"emis_code":"32230183","district_id":"18","tehsil_id":"70","markaz_id":"2605","school_id":"10669","status":"ok"}
```

`data/school_coords.jsonl`

```json
{"school_id":"10669","emis_code":"32230183","latitude":30.84132237,"longitude":71.2246089,"status":"ok"}
```

`data/schools_final.{json,csv,geojson}` — one row per base-file school, with any extra
columns from the base file carried through. Final `status` is one of:

| status | meaning |
| --- | --- |
| `ok` | ID + coordinates resolved |
| `coords_missing` | school has no location stored in SIS (or `0,0`) |
| `coords_error` | network/server error during phase 2 |
| `id_missing` | endpoint returned `null` for that EMIS code |
| `id_error` | network/server error during phase 1 |
| `coords_pending` / `id_pending` | not fetched yet |

Rows outside the Punjab bounding box are kept but flagged `out_of_range: true`.
The `.geojson` contains only rows with real coordinates, ready to drop into QGIS or Kepler.

## Dashboard

A static site (`dashboard/`) for the collected school data. It has three parts:

- **Find a school by EMIS.** The school's card appears, the scope dropdowns jump to its
  district, wing, tehsil and markaz, and its pin is highlighted.
- **Browse by scope.** District → wing → tehsil → markaz. Each dropdown shows only the
  options inside the level above it, with school counts. The table lists the schools in
  the scope, with sorting, a filter box, an "only data issues" toggle, and CSV download.
- **Map.** Clustered pins for the schools in the scope, on OpenStreetMap tiles. Each
  pin has a popup with a Google Maps link.

The page keeps its state in the URL, so a link such as
`#d=<district id>&w=SE&t=<tehsil id>&m=<markaz id>&e=<EMIS>` opens the same view.
District, tehsil and markaz are SIS ids, so the links keep working even if a name is respelled.

```bash
python run.py dashboard              # build dashboard/data/schools.json (not committed)
python run.py dashboard --serve      # ...and serve dashboard/ on http://0.0.0.0:8000
```

It reads the three committed data files (`data/Base Schools.json`,
`data/school_ids.jsonl`, `data/school_coords.jsonl`). Leaflet and markercluster are
vendored in `dashboard/vendor/`, so the page loads no scripts from a CDN.

Things to know:

- The EMIS search and the table use the snapshot in the build. They do not query the live SIS.
- Schools with no coordinates, schools not found in SIS, and schools outside Punjab are
  shown in the table with a status badge. They are not drawn on the map.
- Five or more schools that report the same coordinates are listed under **Data issues**.
  Smaller shared points are not listed, but their pins still spread apart when zoomed in.
- Wing labels: `SE` = Secondary Education (SE), `W-EE` = Women's Elementary Education
  (W-EE), `M-EE` = Male Elementary Education (M-EE).
- Unlabeled numeric columns in the base file are kept in the data but not shown.

### Publish to GitHub Pages

The workflow file cannot be pushed by the Arena app (no `workflows` permission), so it
ships as `ci/dashboard-pages.yml.txt`. One time:

1. On GitHub: **Settings → Pages → Build and deployment → Source: GitHub Actions**.
2. From a checkout of `main`: `./ci/install-workflow.sh --dashboard`.

After that the site rebuilds after each successful **Phase 2 - Fetch School Coordinates**
run, on pushes that change `dashboard/`, `sis/`, `run.py` or the data files, and on demand:

```bash
gh workflow run dashboard-pages.yml --ref main
```

Coordinate commits carry `[skip ci]`, so the rebuild after Phase 2 is triggered by
`workflow_run`, not by a push.

### Dashboard tests

```bash
python run.py test              # includes tests/test_dashboard.py (builder rules and data invariants)
npm --prefix dashboard test     # JavaScript tests for the scope, search, sort and CSV logic (Node 21+)
```

## Tests

```bash
python run.py test     # offline regression suite, no network needed
```

## Combining shards manually

```bash
python -m sis.combine 'parts/**/*.jsonl' -k emis_code -o data/school_ids.jsonl
python -m sis.combine 'parts/**/*.jsonl' -k school_id -o data/school_coords.jsonl
```

Duplicate keys resolve to the best record (`ok` > `missing` > `error`), so a
successful retry always supersedes an earlier failure.

## Notes on scale

38K EMIS codes ≈ 76K requests total. At 8 workers and ~5 req/s sustained that's roughly
2–4 hours per phase depending on server responsiveness. Start conservative, watch the
error counter in the progress line, and raise `-w` only if errors stay at zero.
