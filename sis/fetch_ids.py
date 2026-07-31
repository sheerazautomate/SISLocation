"""Phase 1 - resolve district / tehsil / markaz / school IDs for every EMIS code.

Endpoint:
    GET /dashboard/get_dtms_by_emis_code?s_id_emis_code=<EMIS>
    -> {"s_district_idFk":"18","s_tehsil_idFk":"70","s_markaz_idFk":"2605",
        "s_id":"10669","s_emis_code":"32230183"}
    -> literal `null` when the EMIS code is unknown.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Any

from .common import (
    BASE_URL,
    JsonlStore,
    Progress,
    add_shard_args,
    extract_emis_codes,
    fetch,
    run_pool,
    select_shard,
)

ENDPOINT = BASE_URL + "/dashboard/get_dtms_by_emis_code?s_id_emis_code={emis}"

FIELDS = ("s_district_idFk", "s_tehsil_idFk", "s_markaz_idFk", "s_id", "s_emis_code")


def resolve_ids(emis: str, *, timeout: float, retries: int, insecure: bool) -> dict[str, Any]:
    url = ENDPOINT.format(emis=emis)
    rec: dict[str, Any] = {"emis_code": emis}
    try:
        resp = fetch(url, timeout=timeout, retries=retries, insecure=insecure)
    except Exception as exc:  # noqa: BLE001
        rec["status"] = "error"
        rec["error"] = f"{type(exc).__name__}: {exc}"
        return rec

    body = (resp.text or "").strip()
    if resp.status_code != 200:
        rec["status"] = "error"
        rec["error"] = f"HTTP {resp.status_code}"
        return rec
    if not body or body.lower() == "null":
        rec["status"] = "missing"
        return rec

    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        rec["status"] = "error"
        rec["error"] = "invalid JSON: " + body[:120].replace("\n", " ")
        return rec

    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        rec["status"] = "missing"
        return rec

    rec.update({
        "district_id": data.get("s_district_idFk"),
        "tehsil_id": data.get("s_tehsil_idFk"),
        "markaz_id": data.get("s_markaz_idFk"),
        "school_id": data.get("s_id"),
        "emis_code_api": data.get("s_emis_code"),
    })
    rec["status"] = "ok" if rec.get("school_id") else "missing"
    return rec


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="fetch_ids",
        description="Resolve district/tehsil/markaz/school IDs for each EMIS code.",
    )
    ap.add_argument("-i", "--input", default="data/Base Schools.json",
                    help="Base schools file (.json/.jsonl/.csv/.txt)")
    ap.add_argument("-o", "--output", default="data/school_ids.jsonl",
                    help="Append-only JSONL results (resumable)")
    ap.add_argument("--emis-field", default=None,
                    help="Explicit EMIS key name (auto-detected by default)")
    ap.add_argument("-w", "--workers", type=int, default=8, help="Concurrent requests")
    ap.add_argument("--timeout", type=float, default=30.0)
    ap.add_argument("--retries", type=int, default=4)
    ap.add_argument("--delay", type=float, default=0.0,
                    help="Random 0..N s jitter before each request (be polite)")
    ap.add_argument("--limit", type=int, default=0, help="Only process first N pending")
    ap.add_argument("--retry-errors", action="store_true",
                    help="Re-attempt rows previously recorded as errors")
    ap.add_argument("--insecure", action="store_true",
                    help="Skip TLS verification (server has a flaky chain)")
    add_shard_args(ap)
    args = ap.parse_args(argv)

    if args.insecure:
        import urllib3

        urllib3.disable_warnings()

    if not os.path.exists(args.input):
        sys.stderr.write(f"Input not found: {args.input}\n")
        return 2

    codes, field = extract_emis_codes(args.input, args.emis_field)
    sys.stderr.write(f"Loaded {len(codes)} unique EMIS codes (field: {field})\n")

    if args.shards > 1:
        codes = select_shard(codes, args.shard, args.shards)
        sys.stderr.write(
            f"Shard {args.shard + 1}/{args.shards}: {len(codes)} codes in this slice\n"
        )

    store = JsonlStore(args.output, key="emis_code")
    done = store.load_done()
    if args.retry_errors:
        done = {k: v for k, v in done.items() if v.get("status") != "error"}
    pending = [c for c in codes if c not in done]
    if args.limit:
        pending = pending[: args.limit]

    sys.stderr.write(
        f"Already done: {len(done)} | pending this run: {len(pending)}\n"
    )
    if not pending:
        sys.stderr.write("Nothing to do.\n")
        return 0

    progress = Progress(len(pending), "IDs", already=len(done))
    with store:
        run_pool(
            pending,
            lambda e: resolve_ids(
                e, timeout=args.timeout, retries=args.retries, insecure=args.insecure
            ),
            store=store,
            progress=progress,
            workers=args.workers,
            delay=args.delay,
        )
    progress.finish()
    sys.stderr.write(f"Wrote {args.output}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
