"""Offline planning, checkpoint seeding, and reporting for the phase-2 workflow."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import tempfile
from collections import Counter

from .combine import combine
from .common import JsonlStore, select_shard
from .fetch_coords import load_id_rows
from .merge import build_rows, summarize, write_outputs


def plan(ids_file: str, base_file: str, shards: int, workers: int,
         delay: float, limit: int) -> dict[str, str]:
    if not 1 <= shards <= 20:
        raise ValueError("shards must be between 1 and 20")
    if not 1 <= workers <= 16:
        raise ValueError("workers must be between 1 and 16")
    if not math.isfinite(delay) or delay < 0:
        raise ValueError("delay must be finite and non-negative")
    if limit < 0:
        raise ValueError("limit must be non-negative")
    if not os.path.isfile(base_file):
        raise ValueError(f"Base file not found: {base_file}")
    rows = load_id_rows(ids_file)
    if not rows:
        raise ValueError("No successful school IDs found; run phase 1 first")
    shards = min(shards, len(rows))
    # Formatting, input order and duplicate rows must not invalidate checkpoints.
    digest = hashlib.sha256(
        json.dumps(sorted(r["school_id"] for r in rows)).encode()
    ).hexdigest()
    return {"matrix": json.dumps(list(range(shards))), "shards": str(shards),
            "schools": str(len(rows)), "digest": digest}


def checkpoint(sources: list[str], output: str, active: set[str] | None = None) -> None:
    """Rank baseline + partial results and atomically replace a checkpoint.

    Filter only shard seeds. Final datasets retain older IDs as well, so a partial
    phase-1 input cannot silently erase previously fetched coordinates.
    """
    directory = os.path.dirname(os.path.abspath(output))
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=directory, suffix=".jsonl")
    os.close(fd)
    try:
        combine(sources, "school_id", temporary)
        if active is not None:
            records = JsonlStore(temporary, "school_id").load_done()
            with open(temporary, "w", encoding="utf-8") as fh:
                for sid, rec in records.items():
                    if sid in active:
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        os.replace(temporary, output)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def seed(ids_file: str, baseline: str, output: str, shard: int, shards: int) -> None:
    active = {r["school_id"] for r in select_shard(load_id_rows(ids_file), shard, shards)}
    # Read the existing output before replacing it; a successful baseline always
    # beats cached errors, and newer same-status results win.
    checkpoint([baseline, output], output, active)


def finalize(ids_file: str, base_file: str, baseline: str, parts: str,
             output: str, prefix: str, emis_field: str | None = None) -> str:
    checkpoint([baseline, parts], output)
    active = {r["school_id"] for r in load_id_rows(ids_file)}
    all_coords = JsonlStore(output, "school_id").load_done()
    coords = [rec for sid, rec in all_coords.items() if sid in active]
    counts = Counter(r.get("status", "?") for r in coords)
    rows = build_rows(base_file, ids_file, output, emis_field)
    write_outputs(rows, prefix)
    total = len(active)
    pending = total - len(coords)
    lines = ["## Phase 2 - School coordinates", "",
             f"- Eligible school IDs: **{total}**",
             f"- Recorded: **{len(coords)}** ({len(coords) / total:.1%})" if total
             else "- Recorded: **0**",
             f"- Pending: **{pending}**",
             f"- Out of Punjab range: **{sum(bool(r.get('out_of_range')) for r in coords)}**",
             f"- Preserved records outside this input: **{len(all_coords) - len(coords)}**",
             "", "| Coordinate status | Count |", "| --- | ---: |"]
    lines.extend(f"| `{status}` | {count} |" for status, count in sorted(counts.items()))
    lines += ["", "### Merged base schools", "", "| Final status | Count |", "| --- | ---: |"]
    lines.extend(f"| `{status}` | {count} |" for status, count in summarize(rows).items())
    if pending:
        lines += ["", "> Re-run to resume unprocessed schools."]
    if counts["error"]:
        lines += ["", "> Re-run with **retry_errors** to repair failed lookups."]
    if counts["missing"]:
        lines += ["", "> Missing coordinates can be retried with **retry_missing**."]
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    sub = ap.add_subparsers(dest="command", required=True)
    p = sub.add_parser("plan")
    p.add_argument("--ids", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--shards", type=int, required=True)
    p.add_argument("--workers", type=int, required=True)
    p.add_argument("--delay", type=float, required=True)
    p.add_argument("--limit", type=int, required=True)
    p = sub.add_parser("seed")
    p.add_argument("--ids", required=True)
    p.add_argument("--baseline", default="data/school_coords.jsonl")
    p.add_argument("--output", required=True)
    p.add_argument("--shard", type=int, required=True)
    p.add_argument("--shards", type=int, required=True)
    p = sub.add_parser("finalize")
    p.add_argument("--ids", required=True)
    p.add_argument("--base", required=True)
    p.add_argument("--baseline", default="data/school_coords.jsonl")
    p.add_argument("--parts", default="parts/**/coords-*.jsonl")
    p.add_argument("--output", default="data/school_coords.jsonl")
    p.add_argument("--prefix", default="data/schools_final")
    p.add_argument("--emis-field", default=None)
    args = ap.parse_args(argv)
    try:
        if args.command == "plan":
            for key, value in plan(args.ids, args.base, args.shards, args.workers,
                                   args.delay, args.limit).items():
                print(f"{key}={value}")
        elif args.command == "seed":
            seed(args.ids, args.baseline, args.output, args.shard, args.shards)
        else:
            print(finalize(args.ids, args.base, args.baseline, args.parts, args.output,
                           args.prefix, args.emis_field or None), end="")
    except (ValueError, OSError) as exc:
        ap.error(str(exc))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
