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

0. **Install the workflow** (one time). The Arena GitHub App isn't granted the
   `workflows` permission, so the workflow file ships at `ci/fetch-dtms.yml.txt`
   and you install it with your own credentials:
   ```bash
   ./ci/install-workflow.sh
   ```
1. Commit the base file so the runner can read it:
   ```bash
   git add -f "data/Base Schools.json"
   git commit -m "data: add base schools file"
   git push
   ```
2. GitHub → **Actions** → **Phase 1 - Fetch DTMS IDs** → **Run workflow**.
3. Leave the defaults (10 shards × 6 workers) or tune them, then start it.

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
from a hard kill is tolerated.

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

## Tests

```bash
python run.py test     # 22 offline tests, no network needed
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
